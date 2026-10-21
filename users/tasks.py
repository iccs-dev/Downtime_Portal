# users/tasks.py
from celery import shared_task
import subprocess
import ntpath
import os
import posixpath
import sys
from django.conf import settings
import pandas as pd
import time 
from django.core.mail import EmailMultiAlternatives, send_mail
from users.models import UploadedFile, User  # Import your model
from django.utils.timezone import now
import logging
import re
import tempfile
from django.db import InterfaceError, OperationalError
from django.db.models import F
from django.utils.html import escape
from django.utils.timezone import localtime
from pytz import timezone as _timezone

from users import mapping
from users.utils import clean_hrms_dump_and_update_mapping

ist = _timezone("Asia/Kolkata")

hrms_logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Daily reminders for files still waiting on someone
# --------------------------------------------------------------------------
# A regular user's upload is not mailed about when it lands. These run at 10:00
# and list what is still outstanding, so a file uploaded today is first reported
# tomorrow morning and keeps being reported until it is actioned.
#
# There is also a dormant `notify_l1_pending` management command that mails each
# L1 user a count. Nothing triggers it; these tasks are the scheduled path.

# Everything not yet finished with. The stage-specific filters are added below.
_PENDING_BASE = dict(
    is_split=True,
    rejected_by_l1=False,
    approved_by_l2=False,
    rejected_by_l2=False,
)


def _pending_rows(queryset):
    """The columns the reminder lists, newest upload first."""
    rows = []
    for record in queryset.select_related('uploaded_by').order_by('-timestamp'):
        rows.append({
            'name': os.path.basename(record.file.name),
            'process': record.process or '-',
            'location': record.location or '-',
            'uploaded_by': record.uploaded_by.username if record.uploaded_by else '-',
            'uploaded': localtime(record.timestamp, ist).strftime('%d-%m-%Y %H:%M'),
        })
    return rows


def _send_pending_reminder(subject, intro, recipient, rows):
    """One mail per recipient listing every outstanding file."""
    if not recipient:
        hrms_logger.warning("No recipient configured for %r; nothing sent.", subject)
        return "Skipped: no recipient configured."

    header = ('File', 'Process', 'Location', 'Uploaded by', 'Uploaded')
    plain = [intro, ""]
    for row in rows:
        plain.append(" | ".join(str(row[k]) for k in
                                ('name', 'process', 'location', 'uploaded_by', 'uploaded')))

    cells = "".join(
        "<tr>" + "".join(
            "<td>%s</td>" % escape(row[k]) for k in
            ('name', 'process', 'location', 'uploaded_by', 'uploaded')
        ) + "</tr>"
        for row in rows
    )
    html = """
    <html><body>
        <p>%s</p>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse;">
            <tr style="background-color: #f2f2f2;">%s</tr>
            %s
        </table>
        <p>Total pending: <strong>%s</strong></p>
    </body></html>
    """ % (
        escape(intro),
        "".join("<th>%s</th>" % escape(h) for h in header),
        cells,
        len(rows),
    )

    send_mail(
        subject=subject,
        message="\n".join(plain),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
        html_message=html,
    )
    hrms_logger.info(
        "Sent %r to %s listing %s pending file(s).", subject, recipient, len(rows)
    )
    return "Sent: %s pending file(s) to %s" % (len(rows), recipient)


@shared_task
def notify_l1_pending_files():
    """Files waiting for an L1 user to press Unlock and Save."""
    pending = UploadedFile.objects.filter(**_PENDING_BASE, l1_status='')
    rows = _pending_rows(pending)

    if not rows:
        hrms_logger.info("No files pending L1 lock/unlock; no mail sent.")
        return "Nothing pending for L1."

    return _send_pending_reminder(
        subject="Pending Files - Lock/Unlock Required",
        intro=(
            "This is a reminder that the following files are pending for "
            "Lock/Unlock action. Please review and update the status."
        ),
        recipient=settings.L1_PENDING_NOTIFY_EMAIL,
        rows=rows,
    )


def cluster_head_email(ats_id):
    """Email of the active L2 account `ats_id`, or None when there is none."""
    if not ats_id:
        return None
    head = User.objects.filter(
        username__iexact=ats_id, role='l2', is_active=True
    ).first()
    return (head.email or None) if head else None


@shared_task
def notify_l2_pending_files():
    """Files L1 has unlocked that are still waiting on an L2 decision.

    Each Cluster Head is mailed only their own processes' files, at the email on
    their user account. A file with no reachable Cluster Head - its process has
    none, or that Cluster Head has no active L2 account with an email - goes to
    L2_PENDING_NOTIFY_EMAIL instead, so nothing pending goes unreported.
    """
    pending = UploadedFile.objects.filter(**_PENDING_BASE, l1_status='unlock')

    if not pending.exists():
        hrms_logger.info("No files pending L2 approval; no mail sent.")
        return "Nothing pending for L2."

    subject = "Pending Files - Approval Required"
    intro = (
        "This is a reminder that the following files are pending for "
        "approval. Please review and approve the pending files."
    )

    # Group the pending files by the address that should hear about them.
    by_recipient = {}
    for record in pending:
        ats_id = mapping.cluster_head_for(record.process, record.location)
        email = cluster_head_email(ats_id)
        if not email:
            if ats_id:
                hrms_logger.warning(
                    "Cluster Head %s has no active L2 account with an email; "
                    "'%s' is reported to the fallback address.",
                    ats_id, record.file.name,
                )
            email = settings.L2_PENDING_NOTIFY_EMAIL
        by_recipient.setdefault(email, []).append(record.id)

    # One failing mail must not stop the others.
    results = []
    for email in sorted(by_recipient, key=lambda e: (e or '').lower()):
        rows = _pending_rows(pending.filter(id__in=by_recipient[email]))
        try:
            results.append(_send_pending_reminder(
                subject=subject, intro=intro, recipient=email, rows=rows,
            ))
        except Exception as exc:
            hrms_logger.error(
                "Could not send the L2 reminder to %s: %s",
                email, exc, exc_info=True,
            )
            results.append("Failed %s: %s" % (email, exc))

    return "; ".join(results)


# --------------------------------------------------------------------------
# HRMS dump -> mapping.csv
# --------------------------------------------------------------------------
# The lock lives beside the dump rather than in memory: the watcher and the
# worker are separate processes, so a threading primitive would not be seen by
# both. O_CREAT|O_EXCL makes the check-and-create one atomic step, which a
# separate exists()-then-create could not guarantee.
HRMS_DUMP_LOCK = "hrms_dump.lock"


@shared_task(bind=True)
def process_hrms_dump(self):
    """Clean the newest HRMS dump and replace media/mapping/mapping.csv.

    Returns a short status string. Failures are logged and returned rather than
    raised: there is nothing to retry automatically, because a dump that fails
    validation will fail identically on a retry, and mapping.csv is left
    untouched either way.
    """
    lock_dir = os.path.join(settings.MEDIA_ROOT, "hrms_dump")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, HRMS_DUMP_LOCK)

    try:
        handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # A run is already under way. Several events can fire for one arriving
        # file, and processing it twice would race on mapping.csv.
        hrms_logger.info("HRMS dump already being processed; skipping.")
        return "Skipped: another HRMS dump run is in progress."

    try:
        os.write(handle, str(self.request.id or "manual").encode())
        os.close(handle)

        hrms_logger.info("HRMS dump processing started.")
        result = clean_hrms_dump_and_update_mapping()
        hrms_logger.info(
            "HRMS dump processed: %s rows -> %s (%s invalid EmpCode(s) dropped, "
            "removed %s)",
            result["rows"], result["mapping_file"],
            result.get("invalid_empcodes", 0),
            ", ".join(result.get("removed_files") or ["nothing"]),
        )
        return "OK: %s rows written to %s" % (result["rows"], result["mapping_file"])

    except FileNotFoundError as exc:
        # Nothing to do - the dump was already processed, or never arrived.
        hrms_logger.info("No HRMS dump to process: %s", exc)
        return "Nothing to process: %s" % exc

    except Exception as exc:
        hrms_logger.exception("HRMS dump processing failed: %s", exc)
        return "Failed: %s" % exc

    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            hrms_logger.warning("Could not remove the HRMS lock at %s", lock_path)


# A database that was briefly unavailable is worth another attempt; a bad file
# or a broken script is not, and would fail the same way on every retry.
TRANSIENT_DB_ERRORS = (OperationalError, InterfaceError)


def _describe_failure(stdout, stderr):
    """The most useful one-line reason out of a failed script run."""
    for stream in (stdout or "", stderr or ""):
        for line in stream.splitlines():
            line = line.strip()
            if line.startswith("Upload failed:"):
                return line[len("Upload failed:"):].strip()
    for stream in (stderr or "", stdout or ""):
        lines = [l.strip() for l in stream.splitlines() if l.strip()]
        if lines:
            return lines[-1][:400]
    return "The HRMS upload script failed without reporting a reason."


def _rejected_report_path(stdout):
    """Where hrms.py saved HRMS's per-row rejection report, if it saved one."""
    match = re.search(r"^Rejected report:\s*(.+)$", stdout or "", re.M)
    if not match:
        return None
    path = match.group(1).strip()
    return path if path and os.path.exists(path) else None


def _build_agent_attachments(source_csv, rejected_report, workdir):
    """Write the uploaded and rejected agent lists for the notification.

    Returns a list of (filename, path). The uploaded list is the approved CSV
    minus every EmpCode HRMS refused - filtered_copy drops all of a rejected
    agent's rows, so the two lists partition the file rather than overlapping.
    Never raises: an attachment problem must not stop the mail going out.
    """
    attachments = []
    stem = os.path.splitext(os.path.basename(source_csv or "upload"))[0]

    rejected_codes = set()
    try:
        if rejected_report:
            frame = pd.read_csv(rejected_report)
            if "EmpCode" in frame.columns:
                rejected_codes = set(
                    frame["EmpCode"].astype(str).str.strip().str.upper()
                )
            out = os.path.join(workdir, "%s_rejected_agents.csv" % stem)
            frame.to_csv(out, index=False)
            attachments.append((os.path.basename(out), out))
    except Exception as exc:
        hrms_logger.warning("Could not prepare the rejected-agent list: %s", exc)

    try:
        if source_csv and os.path.exists(source_csv):
            frame = pd.read_csv(source_csv)
            if "EmpCode" in frame.columns:
                codes = frame["EmpCode"].astype(str).str.strip().str.upper()
                uploaded = frame[~codes.isin(rejected_codes)]
                if not uploaded.empty:
                    out = os.path.join(workdir, "%s_uploaded_agents.csv" % stem)
                    uploaded.to_csv(out, index=False)
                    attachments.append((os.path.basename(out), out))
    except Exception as exc:
        hrms_logger.warning("Could not prepare the uploaded-agent list: %s", exc)

    return attachments


def _agent_counts(stdout):
    """The uploaded/rejected agent counts hrms.py prints after a success.

    Returns (uploaded, rejected) or (None, None) when the line is absent - an
    older script, or a run that failed before it got that far.
    """
    match = re.search(
        r"Uploaded agents:\s*(\d+)\s*\|\s*Rejected agents:\s*(\d+)",
        stdout or "",
    )
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _notify_hrms_outcome(file_record, original_file, success, hrms_status,
                         error="", task_id="", l2_user=None, l2_at=None,
                         uploaded_agents=None, rejected_agents=None,
                         attachments=None):
    """Mail the HRMS outcome to the standing recipients and the file's uploader.

    Failures also go to HRMS_FAILURE_NOTIFY_EMAILS.

    Sent once per outcome per task run. A Celery retry keeps its task id, so a
    retried run does not mail twice; a fresh L2 approval is a new run and does.

    Never raises: a mail problem must not change what happened to the file, nor
    mask the error being reported.
    """
    outcome = "success" if success else "failure"
    key = "%s:%s" % (outcome, task_id or "manual")

    if file_record is not None and file_record.hrms_notified_key == key:
        hrms_logger.info(
            "HRMS %s for %s already notified (%s); not sending again.",
            outcome, original_file, key,
        )
        return False

    # Read defensively: this runs outside the try below, and anything raised
    # here escapes into upload_to_hrms's error handling, which would roll back
    # an approval HRMS has already accepted.
    recipients = list(getattr(settings, "HRMS_NOTIFY_EMAILS", None) or [])
    if not success:
        for address in getattr(settings, "HRMS_FAILURE_NOTIFY_EMAILS", None) or []:
            if address not in recipients:
                recipients.append(address)
    # The MIS user who uploaded it, taken from the record rather than a list.
    uploader = getattr(getattr(file_record, "uploaded_by", None), "email", "") or ""
    uploader = uploader.strip()
    if uploader:
        if uploader not in recipients:
            recipients.append(uploader)
    else:
        # Not an error - the mail still goes to the standing recipients - but
        # the person who uploaded the file will not hear about it, and that is
        # worth seeing in the log rather than discovering later.
        hrms_logger.warning(
            "No email address on the account that uploaded %s (%s); the HRMS "
            "%s notice goes only to the standing recipients.",
            original_file,
            getattr(getattr(file_record, "uploaded_by", None), "username", "unknown"),
            outcome,
        )
    if not recipients:
        hrms_logger.warning("No recipients configured for HRMS notifications.")
        return False

    when = localtime(now(), ist).strftime("%d-%m-%Y %H:%M:%S")
    process = getattr(file_record, "process", "") or "-"

    # The approval stands whatever HRMS does with the file; failures are not
    # retried and the file is not returned to the L2 queue.
    l2_state = "Approved by L2%s%s" % (
        " by %s" % l2_user if l2_user else "",
        " on %s" % localtime(l2_at, ist).strftime("%d-%m-%Y %H:%M") if l2_at else "",
    )

    rows = [
        ("Process", process),
        ("File name", original_file),
        ("L2 approval status", l2_state),
        ("HRMS status", hrms_status),
    ]
    if uploaded_agents is not None:
        rows.append(("Total agents uploaded", uploaded_agents))
    if rejected_agents is not None:
        rows.append(("Total agents rejected", rejected_agents))
    if not success:
        rows.append(("Error", error or "No reason reported."))
    rows.append(("Completion time" if success else "Failure time", when))

    subject = "HRMS Upload %s: %s" % (
        "Successful" if success else "Failed", original_file,
    )
    plain = "\n".join("%s: %s" % (label, value) for label, value in rows)
    html = """
    <html><body>
        <p>%s</p>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse;">
        %s
        </table>
    </body></html>
    """ % (
        escape(
            "The file below was uploaded to HRMS successfully."
            if success else
            "The file below was approved by L2 but HRMS reported the attendance "
            "as already processed. It has been sent back to L1 (Files to mark) "
            "to be marked again."
            if is_hrms_locked(error) else
            "The file below was approved by L2 but could not be delivered to "
            "HRMS. It will not be retried; the error is shown under File Status "
            "on the L2 page."
        ),
        "".join(
            "<tr><th align='left'>%s</th><td>%s</td></tr>" % (escape(l), escape(str(v)))
            for l, v in rows
        ),
    )

    try:
        # EmailMultiAlternatives rather than send_mail: the same HTML-plus-text
        # body, but this one can carry the agent lists as attachments.
        message = EmailMultiAlternatives(
            subject=subject,
            body=plain,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
        )
        message.attach_alternative(html, "text/html")
        for name, path in (attachments or []):
            try:
                with open(path, "rb") as handle:
                    message.attach(name, handle.read(), "text/csv")
            except OSError as exc:
                hrms_logger.warning("Could not attach %s: %s", path, exc)
        message.send(fail_silently=False)
        hrms_logger.info(
            "HRMS %s notice for %s sent to %s with %d attachment(s).",
            outcome, original_file, ", ".join(recipients),
            len(attachments or []),
        )
    except Exception as exc:
        hrms_logger.error(
            "Could not send the HRMS %s notice for %s: %s",
            outcome, original_file, exc,
        )
        return False

    if file_record is not None:
        UploadedFile.objects.filter(pk=file_record.pk).update(hrms_notified_key=key)
    return True


def is_hrms_locked(error):
    """True when HRMS refused the file because attendance is already processed.

    Matched on HRMS's own wording, which spells it "Attendence"; lower-cased so a
    change of capitalisation on their side still hits.
    """
    return "already processed" in (error or "").lower()


# Clears the L2 approval and the L1 mark, which puts the file back in L1's
# Files to mark tab and takes it out of L2 entirely.
SEND_BACK_TO_L1 = dict(
    approved_by_l2=False, approved_by_l2_user=None, approved_at_l2=None,
    l1_status='', l1_status_by=None, l1_status_at=None,
)


def _record_delivery_failure(file_record, reason="", attempts=3):
    """Record why an approved file did not reach HRMS.

    The L2 approval is kept: once approved, a file leaves the pending queue for
    good and is not retried. The error is what the L2 File Status tab shows.

    The one exception is an HRMS locked period ("Attendance already processed"):
    that file goes back to L1 to be marked again, and from there through L2 and
    HRMS once more.

    Never raises. This runs inside error handling, and a write that fails here
    must not mask the original error. The write itself is retried a few times
    because a locked database is one of the failures being recorded.
    """
    if not file_record:
        return False

    fields = dict(
        last_delivery_error=reason or "The HRMS upload failed without a reason.",
        last_delivery_attempt_at=now(),
        delivery_attempts=F('delivery_attempts') + 1,
    )
    if is_hrms_locked(reason):
        fields.update(SEND_BACK_TO_L1)

    for attempt in range(attempts):
        try:
            UploadedFile.objects.filter(pk=file_record.pk).update(**fields)
            return True

        except TRANSIENT_DB_ERRORS as exc:
            if attempt == attempts - 1:
                hrms_logger.error(
                    "Could not record the HRMS failure for %s after %s attempts: %s",
                    file_record.file.name, attempts, exc,
                )
                return False
            time.sleep(2 ** attempt)

        except Exception as exc:
            hrms_logger.exception(
                "Could not record the HRMS failure for %s: %s",
                file_record.file.name, exc,
            )
            return False

    return False


@shared_task(bind=True, max_retries=0)
def upload_to_hrms(self, file_path):
    file_record = None
    # The producer and this worker need not share an OS. A Windows path arriving
    # at a Linux worker contains no separator POSIX recognises, so basename()
    # would hand back the whole string and the record lookup below could never
    # match - leaving a failed upload silently still approved.
    original_file = ntpath.basename(posixpath.basename(str(file_path)))
    try:
        # media/L2_approved/<stem>.csv was produced from L1_approved/<stem>.xlsx,
        # so match on the stem. Matching the full filename would compare a .csv
        # name against the .xlsx path stored on the record and never hit.
        file_stem = os.path.splitext(original_file)[0]
        file_record = UploadedFile.objects.filter(
            file__icontains=file_stem, is_split=True
        ).first()

        # Read the approval before anything can roll it back, so the notices
        # can still say who approved the file and when.
        l2_user = getattr(getattr(file_record, "approved_by_l2_user", None),
                          "username", None)
        l2_at = getattr(file_record, "approved_at_l2", None)
        task_id = getattr(getattr(self, "request", None), "id", "") or ""

        # The file is uploaded exactly as L2 approved it. It is never converted,
        # de-duplicated or rewritten here; approve_file_and_copy already produced
        # the final CSV, and rewriting it would destroy the approved artifact.
        script_path = os.path.join(settings.BASE_DIR, 'users', 'scripts', 'hrms.py')
        command = [sys.executable, script_path, file_path]
        dry_run = os.environ.get(
            'HRMS_UPLOAD_DRY_RUN', ''
        ).strip().lower() in ('1', 'true', 'yes')
        if dry_run:
            command.append('--dry-run')

        # Run once. An approved file is never retried: a failure is recorded on
        # the file and mailed, and the file stays approved.
        result = subprocess.run(
            command,
            capture_output=True,
            text=True
        )
        stdout = result.stdout
        stderr = result.stderr

        # A dry run deliberately stops before Import, so it never prints the
        # success banner. Treat it as terminal.
        if dry_run and result.returncode == 0 and "Dry run complete" in stdout:
            return f"Dry run OK: {original_file}\n{stdout}"

        # Check if subprocess crashed or stderr contains any exception
        if (
            result.returncode != 0
            or "Script failed" in stdout
            or "CRITICAL" in stdout
            or "failed" in stdout.lower()
            or "Traceback" in stderr
            or "Exception" in stderr
            or "NameError" in stderr
        ):

            reason = _describe_failure(stdout, stderr)
            _record_delivery_failure(file_record, reason)
            with tempfile.TemporaryDirectory() as workdir:
                _notify_hrms_outcome(
                    file_record, original_file, success=False,
                    hrms_status="Failed - the upload script reported an error",
                    error=reason, task_id=task_id,
                    l2_user=l2_user, l2_at=l2_at,
                    attachments=_build_agent_attachments(
                        file_path, _rejected_report_path(stdout), workdir,
                    ),
                )
            return f"Script crash or error detected for file: {original_file}"

        if "Record Saved Successfully!" in stdout:
            # Clears an error left from before approvals stopped being
            # released, so File Status does not show a stale failure.
            if file_record is not None:
                UploadedFile.objects.filter(pk=file_record.pk).update(
                    last_delivery_error=''
                )
            uploaded, rejected = _agent_counts(stdout)
            with tempfile.TemporaryDirectory() as workdir:
                _notify_hrms_outcome(
                    file_record, original_file, success=True,
                    hrms_status="Success - HRMS confirmed the import",
                    task_id=task_id, l2_user=l2_user, l2_at=l2_at,
                    uploaded_agents=uploaded, rejected_agents=rejected,
                    attachments=_build_agent_attachments(
                        file_path, _rejected_report_path(stdout), workdir,
                    ),
                )
            return f"Success: {original_file}\n{stdout}"

        reason = (_describe_failure(stdout, stderr)
                  or "HRMS did not confirm the import.")
        _record_delivery_failure(file_record, reason)
        _notify_hrms_outcome(
            file_record, original_file, success=False,
            hrms_status="Failed - HRMS did not confirm the import",
            error=reason, task_id=task_id, l2_user=l2_user, l2_at=l2_at,
        )
        return f"Failed: {original_file}\nSTDOUT: {stdout}\nSTDERR: {stderr}"

    except subprocess.CalledProcessError as e:
        _record_delivery_failure(file_record, _describe_failure(e.stdout, e.stderr))


        _notify_hrms_outcome(
            file_record, original_file, success=False,
            hrms_status="Failed - the upload script could not be run",
            error=_describe_failure(e.stdout, e.stderr),
            task_id=task_id, l2_user=l2_user, l2_at=l2_at,
        )
        return f"Script failed: {original_file}\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}"

    except Exception as e:
        hrms_logger.exception(
            "HRMS upload task failed for %s: %s", original_file, e,
        )

        _record_delivery_failure(file_record, str(e)[:400])

        _notify_hrms_outcome(
            file_record, original_file, success=False,
            hrms_status="Failed - the upload task raised an error",
            error=str(e)[:400],
            task_id=locals().get("task_id", ""),
            l2_user=locals().get("l2_user"), l2_at=locals().get("l2_at"),
        )

        return f"Failed (not retried): {original_file}: {str(e)}"
