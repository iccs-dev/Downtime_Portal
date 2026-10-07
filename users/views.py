from django.conf import settings
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from .forms import CSVUploadForm
from .models import StoredFile, UploadedFile, User
from django.core.files.storage import FileSystemStorage
from django.http import HttpResponse
from django.utils.encoding import smart_str
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.forms import UserCreationForm
from django.db.models import F, Q
import hashlib
import logging
import mimetypes
import os
from io import BytesIO

import pandas as pd
from django.utils.timezone import now
from django.utils.timezone import now, localtime 
from pytz import timezone
import subprocess
import shutil
import paramiko
from scp import SCPClient
import uuid  # Add at the top if not already imported
from django.core.mail import EmailMessage
from datetime import datetime, timedelta
import re
from users.tasks import _notify_hrms_outcome, _record_delivery_failure, upload_to_hrms
from users import mapping

ist = timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)

# An inactive-ATS rejection is held here rather than in the messages
# framework, which clears a message as soon as it is rendered once. The rule is
# that this error stays on screen until the next upload attempt replaces or
# clears it, so a refresh must not lose it.
INACTIVE_ATS_SESSION_KEY = 'inactive_ats_error'

# Upload validation limits.
MAX_UPLOAD_ROWS = 300  # rows per file, excluding the header
MAX_RECORD_AGE_DAYS = 40  # a date this old, or older, is rejected

# Destination for approved files. Credentials come from the environment (.env);
# never hard-code them here.
HRMS_HOST = os.environ.get('HRMS_HOST', '10.140.28.7')
HRMS_USER = os.environ.get('HRMS_USER', 'iccsadmin')
HRMS_PASSWORD = os.environ.get('HRMS_PASSWORD', '')
HRMS_REMOTE_DIR = os.environ.get('HRMS_REMOTE_DIR', '/D:/Revenue/media/Downtime')
HRMS_SSH_TIMEOUT = int(os.environ.get('HRMS_SSH_TIMEOUT', '10'))
# ---------- LOGIN FUNCTION ----------
def user_login(request):
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            login(request, user)

            if user.role.lower() == 'regular':
                return redirect('home')
            elif user.role.lower() == 'l1':
                return redirect('approve_files')
            elif user.role.lower() == 'l2':
                return redirect('approve_files_l2')
            elif user.is_superuser or user.is_staff or user.role.lower() == 'admin':
                messages.warning(request, "Admins must log in via the admin panel.")
                return redirect('/admin/')
        else:
            messages.error(request, "Invalid username or password.")
    
    return render(request, 'users/login.html')

# ---------- LOGOUT FUNCTION ----------
def user_logout(request):
    logout(request)
    return redirect('user_login')



def validate_file_format(uploaded_file):
    """Check if uploaded file matches the reference format."""
    reference_file_path = os.path.join(settings.MEDIA_ROOT, "reference/Upload_Format.xlsx")

    try:
        reference_df = pd.read_excel(reference_file_path, engine="openpyxl")
        file_ext = uploaded_file.name.split('.')[-1].lower()

        if file_ext == "csv":
            uploaded_df = pd.read_csv(uploaded_file)
        elif file_ext in ["xls", "xlsx"]:
            uploaded_df = pd.read_excel(uploaded_file, engine="openpyxl")
        else:
            return False, "Only .csv file is allowed."

        # Size limit, checked before anything else so oversized files fail fast.
        if len(uploaded_df) > MAX_UPLOAD_ROWS:
            return False, (
                f"File contains {len(uploaded_df)} rows. "
                f"A maximum of {MAX_UPLOAD_ROWS} rows is allowed."
            )

        # uploaded_df = pd.read_excel(uploaded_file, engine="openpyxl")

        # Drop empty columns
        reference_df = reference_df.dropna(axis=1, how="all")
        uploaded_df = uploaded_df.dropna(axis=1, how="all")

        # Normalize column names
        reference_columns = [col.strip().lower() for col in reference_df.columns]
        uploaded_columns = [col.strip().lower() for col in uploaded_df.columns]
        # print(f"Reference Columns: {list(reference_df.columns)}")
        # print(f"Uploaded Columns: {list(uploaded_df.columns)}")
        # Compare column names
        if reference_columns == uploaded_columns:
            uploaded_df.columns = [col.strip() for col in uploaded_df.columns]
            # New validation step
            # duplicates_check = uploaded_df.groupby(['EmpCode', 'Date'])['Minutes'].nunique()
            # if (duplicates_check > 1).any():

            # if not uploaded_df["EmpCode"].astype(str).str.match(r"(?i)^ATS\d+$").all():
            #     return False, "Invalid EmpCode format."
            if not uploaded_df["EmpCode"].astype(str).str.strip().str.match(r"(?i)^ATS\d+$").all():
                return False, "Invalid EmpCode format."
            
            # Minutes column numeric validation
            try:
                # Strip spaces and convert to numeric
                uploaded_df["Minutes"] = uploaded_df["Minutes"].astype(str).str.strip()  # remove leading/trailing spaces
                uploaded_df["Minutes"] = pd.to_numeric(uploaded_df["Minutes"], errors="coerce")  # turn non-numeric to NaN
                # uploaded_df["Minutes"] = pd.to_numeric(uploaded_df["Minutes"])
                # Check for any non-numeric (NaN after coercion)
                if uploaded_df["Minutes"].isna().any():
                    return False, "Minutes column contains non-numeric or malformed values."
                if not uploaded_df["Minutes"].apply(lambda x: float(x).is_integer()).all():
                    return False, "Minutes must be integer values only."
            except:
                return False, "Minutes column contains non-numeric values."
            
            # Validate Date formats strictly (no time)
            def is_valid_date_format(date_str):
                # Accept only mm-dd-yyyy exactly
                formats = ["%m-%d-%Y"]
                for fmt in formats:
                    try:
                        # Try parsing, then check if string matches format exactly
                        dt = datetime.strptime(date_str, fmt)
                        # re-format to string and compare to original to avoid partial matches
                        if dt.strftime(fmt) == date_str:
                            return True
                    except:
                        continue
                return False

            # Convert all dates to string first to handle pandas NaNs, datetimes, etc.
            date_strings = uploaded_df["Date"].astype(str)
            for i, d in enumerate(date_strings):
                if not is_valid_date_format(d):
                    return False, f"Invalid date format. Allowed formats: mm-dd-yyyy."

            # Parse strings to datetime
            parsed_dates = date_strings.apply(lambda d: datetime.strptime(d, "%m-%d-%Y"))

            # Compare against midnight today. Parsed dates carry no time, so using
            # the current time of day here would make the boundary drift.
            today = datetime.today().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            oldest_allowed = today - timedelta(days=MAX_RECORD_AGE_DAYS)

            # A date exactly MAX_RECORD_AGE_DAYS old is already too old, hence <=.
            if (parsed_dates <= oldest_allowed).any():
                return False, (
                    f"File contains Date older than {MAX_RECORD_AGE_DAYS} days."
                )

            if (parsed_dates > today).any():
                return False, "File contains Future Date data."
            
            uploaded_df["EmpCode"] = uploaded_df["EmpCode"].str.strip().str.upper()
            if uploaded_df.duplicated(subset=["EmpCode", "Date"]).any():
                return False, "File contains rows with same EmpCode and Date but different Minutes values. Upload aborted."
            return True, "File validated"
        else:
            print(f"Reference Columns: {reference_columns}")
            print(f"Uploaded Columns: {uploaded_columns}")
            return False, "File format is incorrect"
    except Exception as e:
        print(f"Error: {e}")
        return False, "File not validated"


def _read_uploaded_dataframe(uploaded_file):
    """Read an in-memory upload into a DataFrame, leaving the pointer rewound.

    The same handle is later written to disk by Django, so it is rewound both
    before and after reading.
    """
    ext = os.path.splitext(uploaded_file.name)[1].lower()

    uploaded_file.seek(0)
    try:
        if ext == '.csv':
            df = pd.read_csv(uploaded_file)
        else:
            df = pd.read_excel(uploaded_file, engine='openpyxl')
    finally:
        uploaded_file.seek(0)

    df.columns = [str(col).strip() for col in df.columns]
    return df


def validate_process_location(uploaded_file, selected_process, selected_location):
    """Confirm every ATS ID in the upload belongs to the selected Process/Location.

    Runs before the file is saved, so a file that fails here never reaches the
    uploads folder, never creates an UploadedFile row and never reaches L1.
    Returns (is_valid, error_message).
    """
    try:
        uploaded_df = _read_uploaded_dataframe(uploaded_file)
    except Exception as exc:
        logger.error("Could not read '%s' for ATS ID validation: %s",
                     uploaded_file.name, exc, exc_info=True)
        return False, "File could not be read for validation."

    ats_column = mapping.find_ats_column(uploaded_df.columns)
    if ats_column is None:
        logger.warning(
            "Upload rejected - no ATS ID/EmpCode column in '%s'. Columns: %s",
            uploaded_file.name, list(uploaded_df.columns),
        )
        return False, "Uploaded file has no EmpCode (ATS ID) column."

    ats_ids = uploaded_df[ats_column].dropna().astype(str).tolist()
    if not ats_ids:
        logger.warning("Upload rejected - '%s' has no ATS IDs.", uploaded_file.name)
        return False, "Uploaded file contains no ATS IDs."

    try:
        is_valid, error = mapping.validate_ats_ids(
            ats_ids, selected_process, selected_location
        )
    except mapping.MappingUnavailable as exc:
        # Without the mapping there is no way to tell whether the file belongs
        # to the selected process/location, so the upload is blocked.
        logger.error("Process/Location validation unavailable: %s", exc)
        return False, (
            "Process/Location mapping is unavailable. "
            "Please contact support before uploading."
        )

    if is_valid:
        logger.info(
            "Upload accepted - %s ATS ID(s) in '%s' all belong to "
            "Process: %s | Location: %s",
            len(set(ats_ids)), uploaded_file.name,
            selected_process, selected_location,
        )

    return is_valid, error


# ---------- HOME PAGE (Regular Users) ----------
@login_required
def home(request):
    if request.user.role.lower() != 'regular':
        return render(request, 'users/home.html', {
            'message': 'You do not have permission to upload files.'
        })

    files = UploadedFile.objects.filter(uploaded_by=request.user)
    for file in files:
        file.timestamp = localtime(file.timestamp, ist)

    files_waiting_approval = UploadedFile.objects.filter(l1_status='')

    if request.method == 'POST':
        # A new attempt clears the previous rejection, whatever its outcome:
        # the message must not outlive the file it was about.
        request.session.pop(INACTIVE_ATS_SESSION_KEY, None)

        form = CSVUploadForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            file = request.FILES['file']
            selected_process = form.cleaned_data['process']
            selected_location = form.cleaned_data['location']
            ext = file.name.split('.')[-1].lower()
            allowed_extensions = ['csv', 'xls', 'xlsx']

            if ext not in allowed_extensions:
                messages.error(request, "Only CSV and Excel files are allowed.")
                return redirect('home')

            if file:
                is_valid, msg = validate_file_format(file)

                if is_valid:
                    # Every ATS ID must belong to the selected Process and
                    # Location. Checked before the file is saved so a rejected
                    # file is never stored and never reaches L1 - no partial
                    # upload is possible.
                    is_valid, msg = validate_process_location(
                        file, selected_process, selected_location
                    )

                if is_valid:
                    uploaded_instance = UploadedFile(
                        file=file,
                        uploaded_by=request.user,
                        is_valid_format=True,
                        is_split=False,
                        process=selected_process,
                        location=selected_location,
                    )
                    uploaded_instance.save()

                    try:
                        uploaded_file_path = os.path.join(settings.MEDIA_ROOT, uploaded_instance.file.name)

                        if ext == 'csv':
                            uploaded_df = pd.read_csv(uploaded_file_path)
                        else:
                            uploaded_df = pd.read_excel(uploaded_file_path, engine='openpyxl')

                        uploaded_df.columns = [col.strip() for col in uploaded_df.columns]
                        # Trim as well as upper-case: validation accepts " ATS4575 "
                        # by stripping before it matches, so without the same strip
                        # here the space would survive into the saved file and on to
                        # L1, L2 and HRMS.
                        uploaded_df['EmpCode'] = (
                            uploaded_df['EmpCode'].astype(str).str.strip().str.upper()
                        )

                        today_str = now().astimezone(ist).strftime("%Y-%m-%d")

                        save_dir = os.path.join(settings.MEDIA_ROOT, 'L1_approved')
                        if not os.path.exists(save_dir):
                            os.makedirs(save_dir)

                        # One file per upload, carrying the uploaded columns unchanged.
                        # No Cluster Head split and no ats_process_ch.xlsx lookup, so the
                        # file holds exactly EmpCode / Date / Minutes and is picked up by
                        # the queue shared by every L1 user.
                        random_str = uuid.uuid4().hex[:8]  # Generate 8-character random string
                        output_filename = f"{today_str}_{random_str}.{ext}"
                        output_path = os.path.join(save_dir, output_filename)

                        if ext == "csv":
                            uploaded_df.to_csv(output_path, index=False)
                        else:
                            uploaded_df.to_excel(output_path, index=False, engine='openpyxl')

                        # Django stores FileField paths with forward slashes; relpath
                        # returns a backslash on Windows, which breaks the media URL.
                        rel_path = os.path.relpath(
                            output_path, settings.MEDIA_ROOT
                        ).replace(os.sep, '/')
                        l1_pending_instance = UploadedFile.objects.create(
                            file=rel_path,
                            uploaded_by=request.user,
                            is_valid_format=True,
                            is_split=True,
                            legacy_ch_routed=False,
                            # Carried onto the row L1 and L2 act on, so both
                            # approvers see the Process/Location the file was
                            # validated against.
                            process=selected_process,
                            location=selected_location,
                        )

                        # Keep the bytes in the database straight away. The
                        # copy under media/ is the one that goes to HRMS, but it
                        # has been lost before now, which left approvals unable
                        # to succeed and unable to stop trying.
                        try:
                            store_file_content(l1_pending_instance)
                        except Exception as exc:
                            logger.warning(
                                "Could not store a database copy of %s: %s",
                                l1_pending_instance.file.name, exc, exc_info=True,
                            )

                        # --- Build summary for email ---
                        total_records = len(uploaded_df)
                        total_empcodes = uploaded_df['EmpCode'].nunique()

                        # HTML summary generation
                        summary_html = f"""
                        <p>Dear Team,</p>
                        <p>This is to inform you that a file named <strong>{file.name}</strong> has been successfully uploaded on <strong>{now().astimezone(ist).strftime('%Y-%m-%d %H:%M:%S')} IST</strong>.</p>

                        <p><strong>Summary of the file:</strong></p>
                        <ul>
                            <li><strong>Process:</strong> {selected_process}</li>
                            <li><strong>Location:</strong> {selected_location}</li>
                            <li><strong>Total Records:</strong> {total_records}</li>
                            <li><strong>Unique EmpCodes:</strong> {total_empcodes}</li>
                        </ul>

                        <p><strong>This file has been sent to L1 for approval.</strong></p>
                        """
                        messages.success(request, "File uploaded and sent for approval.")

                    except Exception as e:
                        messages.error(request, f"File upload succeeded, but processing failed: {str(e)}")

                    else:
                        # The notification email is best-effort. By this point the
                        # L1_approved file is already written and saved, so a mail failure
                        # must not be reported to the user as a processing failure.
                        try:
                            email = EmailMessage(
                                subject='Downtime Uploaded Successfully',
                                body=summary_html,
                                from_email=settings.DEFAULT_FROM_EMAIL,
                                to=['rahul.kumar@iccs.in', 'mis.support@iccs.in', 'mangesh.bhayje@iccs.in','santosh.kumar@iccs.in','akshat.bhatnagar@iccs.in'],
                                # to=['sourabh.kumar@iccs.in'],
                            )
                            email.content_subtype = 'html'  # Set the email content to HTML

                            # Attach the original uploaded file
                            email.attach_file(uploaded_file_path)

                            # Attach the file awaiting L1 approval
                            email.attach_file(
                                os.path.join(settings.MEDIA_ROOT, l1_pending_instance.file.name)
                            )

                            # Send the email
                            email.send()

                        except Exception as e:
                            logger.warning("Upload notification email failed: %s", e, exc_info=True)
                            messages.warning(
                                request,
                                "The file was uploaded and split successfully, but the "
                                f"notification email could not be sent: {e}"
                            )

                    # Post/Redirect/Get. Every other outcome already redirects;
                    # without this one the success path renders the response to
                    # the POST itself, so a refresh re-submits the upload and the
                    # same file is processed and sent to L1 a second time. The
                    # outcome is carried by the messages framework, which survives
                    # the redirect.
                    return redirect('home')

                else:
                    # messages.error(request, "Either file format does not match the required structure or the file contains duplicate records.")
                    if msg.startswith(mapping.UNKNOWN_ATS_ERROR):
                        # Held in the session so it survives refreshes. Not also
                        # added as a message, which would show it twice on the
                        # first render.
                        request.session[INACTIVE_ATS_SESSION_KEY] = msg
                    else:
                        messages.error(request, msg)
                    return redirect("home")
    else:
        form = CSVUploadForm(user=request.user)

    return render(request, 'users/home.html', {
        'form': form,
        'files': files,
        'files_waiting_approval': files_waiting_approval,
        # Survives refreshes until the next upload attempt clears it.
        'inactive_ats_error': request.session.get(INACTIVE_ATS_SESSION_KEY),
        # Drives the Location dropdown from the selected Process. Embedded in
        # the page rather than fetched, so picking a Process costs no request.
        'process_location_map': mapping.get_process_location_map(),
        # Shown as field hints, so the limits on the page are the ones actually
        # enforced rather than a number copied into the template.
        'process_count': len(mapping.get_processes()),
        'max_upload_rows': MAX_UPLOAD_ROWS,
        'max_record_age_days': MAX_RECORD_AGE_DAYS,
        "MEDIA_URL": settings.MEDIA_URL
    })

# ---------- FILE STATUS PAGE (L1 Users) ----------
@login_required
def approve_files(request):
    """L1 unlocks files. L1 no longer approves or rejects.

    A file is Lock until an L1 user presses Unlock and Save, which is the only
    action on this page. Unlocking records the decision, moves the file to that
    user's Your Decision tab and hands it to L2 as Unlock. Either way the file is
    in front of L2, which approves as it always has; a file counts as approved
    only once L2 approves it.
    """
    if request.user.role.lower() != 'l1':
        return render(request, 'users/approval.html', {
            'message': 'You do not have permission to set file status.'
        })

    # Still to decide: not unlocked yet, and not frozen by L2. Unlocking a file
    # takes it out of this list and into the user's Your Decision tab. Files
    # rejected under the retired L1 approval step stay terminal and are left out.
    files = UploadedFile.objects.filter(
        is_split=True,
        l1_status='',
        rejected_by_l1=False,
        approved_by_l2=False,
        rejected_by_l2=False,
    )
    if request.user.username.lower() != 'ishita':
    # if request.user.username.lower() != 'iccs':
        # Files created since Cluster Head routing was removed are shared by every
        # L1 user. Files created before it still reach only the Cluster Head named
        # in their filename, so they can finish under the rules they started under.
        files = files.filter(
            Q(legacy_ch_routed=False)
            | Q(legacy_ch_routed=True,
                file__icontains=f"_{request.user.username}.")
        )

    for file in files:
        file.timestamp = localtime(file.timestamp, ist)  # Convert UTC to IST

    # What this user unlocked. A file lands here the moment Unlock and Save is
    # pressed, and stays as this user's record of the decision.
    decided_files = UploadedFile.objects.filter(
        l1_status='unlock',
        l1_status_by=request.user,
    )
    for file in decided_files:
        file.timestamp = localtime(file.timestamp, ist)

    if request.method == 'POST':
        file_id = request.POST.get('file_id')

        if 'unlock_and_save' in request.POST:
            try:
                file = UploadedFile.objects.get(id=file_id)
            except UploadedFile.DoesNotExist:
                messages.error(request, "File not found.")
            else:
                # Guarded on L2 not having acted, so an unlock can never land on
                # a row L2 has already approved or rejected.
                changed = UploadedFile.objects.filter(
                    id=file.id, approved_by_l2=False, rejected_by_l2=False
                ).update(
                    l1_status='unlock',
                    l1_status_by=request.user,
                    l1_status_at=now(),
                )
                if changed:
                    messages.success(
                        request,
                        f"File '{file.file.name}' saved and unlocked."
                    )
                else:
                    messages.warning(
                        request,
                        "This file has already been actioned by L2 and its "
                        "status can no longer be changed."
                    )

        return redirect('approve_files')

    return render(request, 'users/approval.html', {
        'files': files,
        'decided_files': decided_files,
    })

def _l2_claim_refused(file):
    """Why a conditional claim did not take: still locked, or already actioned."""
    file.refresh_from_db()
    if file.l1_status != 'unlock':
        return (
            f"File '{file.file.name}' is locked. L1 must unlock it before it "
            "can be approved or rejected."
        )
    return "This file has already been actioned by another L2 user."


def _approval_failed(file, file_name, l2_user, hrms_status, error):
    """Record and mail a failure that happened in the Approve request itself.

    Failures after the task is queued are handled by upload_to_hrms; these two
    never reach it. The approval stands either way - the file moves to File
    Status with the error and is not retried. A fresh key per call so a repeated
    failure is reported again rather than suppressed as a duplicate.
    """
    _record_delivery_failure(file, error)
    _notify_hrms_outcome(
        file, file_name, success=False, hrms_status=hrms_status, error=error,
        task_id="approve-%s" % uuid.uuid4().hex, l2_user=l2_user.username,
    )


# ---------- FILE APPROVAL PAGE (L2 Users) ----------
@login_required
def approve_files_l2(request):
    """Second approval stage. Files reaching HRMS must clear this one."""
    if request.user.role.lower() != 'l2':
        return render(request, 'users/approval_l2.html', {
            'message': 'You do not have permission to approve files.'
        })

    # Everything L2 has not yet actioned, shared by all L2 users, split by what
    # L1 did with it. Only the files L1 unlocked can be approved or rejected; the
    # rest are listed so L2 can see and read them while they wait on L1.
    pending = UploadedFile.objects.filter(
        is_split=True,
        rejected_by_l1=False,
        approved_by_l2=False,
        rejected_by_l2=False,
    )
    files = pending.filter(l1_status='unlock')
    # A file HRMS sent back as "already processed" is L1's to re-mark; it is
    # kept off L2 entirely until L1 unlocks it again.
    locked_files = pending.exclude(l1_status='unlock').exclude(
        last_delivery_error__icontains='already processed'
    )

    for file in files:
        file.timestamp = localtime(file.timestamp, ist)  # Convert UTC to IST
        # Drives the template: a file whose bytes are gone gets no Approve button.
        file.source_available = source_is_available(file)
    for file in locked_files:
        file.timestamp = localtime(file.timestamp, ist)
        file.source_available = source_is_available(file)
    approved_files = UploadedFile.objects.filter(approved_by_l2_user=request.user)
    rejected_files = UploadedFile.objects.filter(rejected_by_l2_user=request.user)

    if request.method == 'POST':
        file_id = request.POST.get('file_id')

        # Approve File -> this is the point the file goes to HRMS
        if 'approve' in request.POST:
            try:
                file = UploadedFile.objects.get(id=file_id)
                # The queue is shared, so claim the file with a conditional update.
                # Without this two L2 users could both trigger the SCP and HRMS
                # would receive the same downtime records twice. l1_status is part
                # of the claim because a file L1 has not unlocked is listed for
                # reading only and must not be actionable.
                if not source_is_available(file):
                    # Checked before the claim so the approval is never taken and
                    # rolled back again; there is nothing left to deliver.
                    messages.error(
                        request,
                        f"File '{file.file.name}' can no longer be delivered: its "
                        "contents are missing from the server and from the "
                        "database. It must be uploaded again."
                    )
                    return redirect('approve_files_l2')

                claimed = UploadedFile.objects.filter(
                    id=file.id, l1_status='unlock',
                    approved_by_l2=False, rejected_by_l2=False
                ).update(
                    approved_by_l2=True,
                    approved_by_l2_user=request.user,
                    approved_at_l2=now(),
                )
                if claimed:
                    approved_path = approve_file_and_copy(file)
                    if approved_path:
                        # Hand the exact media/L2_approved CSV to the HRMS uploader.
                        # Queued rather than run inline: the Selenium import takes
                        # around a minute and must not block the approver's request.
                        try:
                            upload_to_hrms.delay(approved_path)
                            messages.success(
                                request,
                                f"File '{file.file.name}' approved and queued for "
                                "HRMS upload."
                            )
                        except Exception as exc:
                            logger.error(
                                "Could not queue the HRMS upload for %s: %s",
                                approved_path, exc, exc_info=True,
                            )
                            _approval_failed(
                                file, os.path.basename(approved_path), request.user,
                                "Failed - the HRMS upload could not be queued",
                                "The HRMS upload could not be queued (Celery "
                                "broker/worker unavailable): %s" % exc,
                            )
                            messages.warning(
                                request,
                                f"File '{file.file.name}' was approved, but the HRMS "
                                "upload could not be queued. Check that the Celery "
                                "worker and broker are running."
                            )
                    else:
                        # The approval stands and is not retried: the file moves to
                        # File Status with the error, like any later HRMS failure.
                        _approval_failed(
                            file, os.path.basename(file.file.name), request.user,
                            "Failed - the file could not be copied to the HRMS host",
                            "The file could not be copied to the HRMS host. "
                            "See the server log for the transfer error.",
                        )
                        messages.error(
                            request,
                            f"File '{file.file.name}' was approved but could NOT be "
                            "delivered to HRMS. See the File Status tab for the error."
                        )
                else:
                    messages.warning(request, _l2_claim_refused(file))

            except UploadedFile.DoesNotExist:
                messages.error(request, "File not found.")

        # Reject File -> terminal, the uploader must submit a new file
        elif 'reject' in request.POST:
            rejection_reason = request.POST.get('rejection_reason', '').strip()
            if not rejection_reason:
                messages.error(request, "Rejection reason is required.")
            else:
                try:
                    file = UploadedFile.objects.get(id=file_id)
                    claimed = UploadedFile.objects.filter(
                        id=file.id, l1_status='unlock',
                        approved_by_l2=False, rejected_by_l2=False
                    ).update(
                        rejected_by_l2=True,
                        rejected_by_l2_user=request.user,
                        rejected_at_l2=now(),
                        rejection_reason_l2=rejection_reason,
                    )
                    if claimed:
                        messages.warning(request, f"File '{file.file.name}' rejected.")
                    else:
                        messages.warning(request, _l2_claim_refused(file))
                except UploadedFile.DoesNotExist:
                    messages.error(request, "File not found.")

        return redirect('approve_files_l2')

    return render(request, 'users/approval_l2.html', {
        'files': files,
        'locked_files': locked_files,
        'approved_files': approved_files,
        'rejected_files': rejected_files,
    })


# ---------- IN-BROWSER FILE PREVIEW ----------
# Row cap for the preview. Uploads are capped at MAX_UPLOAD_ROWS anyway, so this
# only guards against a legacy file that predates that limit.
PREVIEW_MAX_ROWS = 500


def store_file_content(uploaded_file):
    """Copy a file's bytes into the database the first time it is previewed.

    Returns the StoredFile row, or None when the file has never been stored and
    is not on disk either, so there is nothing to copy. Once a file is stored
    the disk copy is not read again: the stored bytes are what the preview
    renders from. Download is unaffected and still serves the file from disk.
    """
    stored = StoredFile.objects.filter(uploaded_file=uploaded_file).first()
    if stored is not None:
        return stored

    path = os.path.join(settings.MEDIA_ROOT, uploaded_file.file.name)
    if not os.path.exists(path):
        return None

    with open(path, 'rb') as handle:
        raw = handle.read()

    # get_or_create rather than create: two people can press View at the same
    # moment, and the one-to-one would then raise on the second insert.
    stored, created = StoredFile.objects.get_or_create(
        uploaded_file=uploaded_file,
        defaults={
            'content': raw,
            'size': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'content_type': mimetypes.guess_type(path)[0] or '',
        },
    )
    if created:
        logger.info(
            "Stored '%s' in the database for preview (%d bytes).",
            uploaded_file.file.name, len(raw),
        )
    return stored


@login_required
def view_file(request, file_id):
    """Render a file's contents as an HTML table, without downloading it.

    The first View copies the file's bytes into the database; every View after
    that renders from that stored copy, so a file stays viewable even once the
    disk copy is gone. Download still serves the file from disk, unchanged.

    Access mirrors the existing pages rather than widening it: L1 and L2 users
    can preview any file in their workflow, a regular user only their own, and
    admins everything. No role or permission rule is changed here.
    """
    try:
        uploaded_file = UploadedFile.objects.get(id=file_id)
    except UploadedFile.DoesNotExist:
        return HttpResponse("File not found.", status=404)

    role = (request.user.role or '').strip().lower()
    is_admin = request.user.is_superuser or request.user.is_staff or role == 'admin'

    if role in ('l1', 'l2') or is_admin:
        pass
    elif uploaded_file.uploaded_by_id == request.user.id:
        pass
    else:
        return HttpResponse("You do not have permission to view this file.", status=403)

    context = {
        'uploaded_file': uploaded_file,
        'file_name': os.path.basename(uploaded_file.file.name),
        'uploaded_at': localtime(uploaded_file.timestamp, ist),
        'preview_max_rows': PREVIEW_MAX_ROWS,
    }

    # Stored on the first View, read from the database on every View after it.
    stored = store_file_content(uploaded_file)
    if stored is None:
        context['error'] = "The file is no longer available on the server."
        return render(request, 'users/file_preview.html', context)

    # SQLite hands back a memoryview; pandas wants something it can read.
    raw = bytes(stored.content)

    try:
        ext = os.path.splitext(uploaded_file.file.name)[1].lower()
        if ext == '.csv':
            df = pd.read_csv(BytesIO(raw))
        else:
            df = pd.read_excel(BytesIO(raw), engine='openpyxl')
    except Exception as exc:
        logger.error("Could not render preview for '%s': %s",
                     uploaded_file.file.name, exc, exc_info=True)
        context['error'] = "This file could not be displayed."
        return render(request, 'users/file_preview.html', context)

    df.columns = [str(col).strip() for col in df.columns]
    context['total_rows'] = len(df)
    context['truncated'] = len(df) > PREVIEW_MAX_ROWS
    context['columns'] = list(df.columns)
    context['rows'] = (
        df.head(PREVIEW_MAX_ROWS)
          .astype(str)
          .replace({'nan': '', 'NaT': '', 'None': ''})
          .values.tolist()
    )
    return render(request, 'users/file_preview.html', context)


@login_required
def profile_view(request):
    user_role = request.user.role.strip().lower() if request.user.role else ""
    return render(request, 'users/profile.html', {'user': request.user, 'user_role': user_role})

def source_is_available(file_record):
    """True while the file's bytes can still be reached.

    On disk is the normal case; the database copy is the fallback for a file
    whose media has been lost. A record with neither can never reach HRMS, so
    offering an approval for it only produces a failure and a rollback.
    """
    path = os.path.join(settings.MEDIA_ROOT, file_record.file.name)
    if os.path.exists(path):
        return True
    return StoredFile.objects.filter(uploaded_file=file_record).exists()


def approve_file_and_copy(file):
    """Build the L2-approved CSV and SCP it to the archive host.

    Returns the absolute path of the CSV under media/L2_approved/ once it has been
    written and delivered, or None on any failure. The path is what gets handed to
    the HRMS uploader, so callers can treat the result as both a success flag and
    the artifact to upload.
    """
    source_file_path = os.path.join(settings.MEDIA_ROOT, file.file.name)
    remote_dir = HRMS_REMOTE_DIR
    original_filename = os.path.basename(file.file.name)
    
    # Ensure the L2-approved directory exists. Files land here only after L2 has
    # approved them, and this is the copy that is pushed to HRMS.
    approved_dir = os.path.join(settings.MEDIA_ROOT, "L2_approved")
    os.makedirs(approved_dir, exist_ok=True)

    ext = os.path.splitext(original_filename)[1].lower()
    base_name = os.path.splitext(original_filename)[0]
    approved_path = os.path.join(approved_dir, f"{base_name}.csv")  # Final approved file will always be .csv

    # The copy under media/ is the normal source. When it has been lost, the
    # bytes stored in the database stand in for it - without this, an approval
    # could only fail and roll itself back, forever.
    source_bytes = None
    if not os.path.exists(source_file_path):
        stored = StoredFile.objects.filter(uploaded_file=file).first()
        if stored is None:
            logger.error(
                "Source file '%s' not found and no stored copy exists; "
                "this file can no longer be delivered.", source_file_path,
            )
            return None
        source_bytes = bytes(stored.content)
        logger.warning(
            "Source file '%s' missing from disk; delivering the stored copy "
            "(%s bytes).", source_file_path, stored.size,
        )

    try:
        # Convert .xls or .xlsx to .csv
        if ext in [".xls", ".xlsx"]:
            df = pd.read_excel(
                BytesIO(source_bytes) if source_bytes is not None else source_file_path,
                engine="openpyxl",
            )
            df.to_csv(approved_path, index=False)
            print(f"Converted Excel file to CSV: {approved_path}")
        elif ext == ".csv":
            if source_bytes is not None:
                with open(approved_path, "wb") as handle:
                    handle.write(source_bytes)
            else:
                shutil.copy(source_file_path, approved_path)
            print(f"Copied CSV file to approved folder: {approved_path}")
        else:
            logger.error("Unsupported file type: %s", ext)
            return None

        # Read the CSV for processing
        approved_df = pd.read_csv(approved_path)

        # Drop unwanted columns
        for col in ['Process', 'CH_ATSID']:
            if col in approved_df.columns:
                approved_df.drop(col, axis=1, inplace=True)

        # Add IsWH column
        approved_df['IsWH'] = 'N'

        # Write back to the same CSV file
        approved_df.to_csv(approved_path, index=False)
        print(f"Processed and saved CSV at: {approved_path}")

    except Exception as e:
        logger.error(
            "Error during file conversion or processing: %s", e, exc_info=True
        )
        return None

    # Rename file for remote copy
    unique_filename = f"{base_name}_{file.id}.csv"
    remote_path = os.path.join(remote_dir, unique_filename).replace("\\", "/")

    ssh = None
    delivered = False
    try:
        # Create SSH client
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=HRMS_HOST,
            username=HRMS_USER,
            password=HRMS_PASSWORD,
            timeout=HRMS_SSH_TIMEOUT,
        )

        # Create SCP client
        with SCPClient(ssh.get_transport()) as scp:
            scp.put(approved_path, remote_path)
            delivered = True
            print(f"File '{unique_filename}' copied to remote server successfully.")

    except Exception as e:
        logger.error(
            "Error copying '%s' to %s on the HRMS host: %s",
            unique_filename, remote_path, e, exc_info=True,
        )

    finally:
        if ssh:
            ssh.close()

    # The path doubles as the success flag and as the artifact the HRMS uploader
    # receives. It is never moved or rewritten after this point.
    return approved_path if delivered else None

def process_empcode_not_found_file(not_found_path):
    # Configuration. Credentials come from the environment, never from source.
    remote_dir = os.environ.get(
        'HRMS_NOT_FOUND_DIR', '/D:/Revenue/media/EmpCode_Not_Found'
    )
    hostname = HRMS_HOST
    username = HRMS_USER
    password = HRMS_PASSWORD

    # Validate source file
    if not os.path.exists(not_found_path):
        print(f"[ERROR] File not found: {not_found_path}")
        return

    # Prepare filename and remote path
    original_filename = os.path.basename(not_found_path)
    base_name, ext = os.path.splitext(original_filename)
    unique_filename = f"{base_name}{ext}"
    remote_path = os.path.join(remote_dir, unique_filename).replace("\\", "/")

    try:
        # Set up SSH client
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=hostname,
            username=username,
            password=password,
            timeout=10
        )

        # Use SCP to copy the file
        with SCPClient(ssh.get_transport()) as scp:
            scp.put(not_found_path, remote_path)
            print(f"[SUCCESS] File '{unique_filename}' copied to remote server at {remote_path}")

    except Exception as e:
        print(f"[ERROR] Failed to upload via SCP: {e}")

    finally:
        if ssh:
            ssh.close()
