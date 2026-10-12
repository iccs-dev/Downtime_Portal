# import hashlib
# import os
# import shutil
# import tempfile
# from datetime import datetime, timedelta
# from io import BytesIO
# from smtplib import SMTPSenderRefused
# from unittest.mock import patch

# import pandas as pd
# from django.core import mail
# from django.contrib.messages import get_messages
# from django.core.files.uploadedfile import SimpleUploadedFile
# from django.core.mail.backends.base import BaseEmailBackend
# from django.conf import settings
# from django.template.loader import render_to_string
# from django.test import TestCase, override_settings
# from django.utils.timezone import now

# from .models import StoredFile, UploadedFile, User
# from users import mapping as users_mapping

# # Captured at import time, before any patch replaces it, so a test can put the
# # genuine Process/Location check back where the shared fixture stubbed it.
# REAL_VALIDATE_ATS_IDS = users_mapping.validate_ats_ids

# XLSX_CONTENT_TYPE = (
#     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# )

# # The view accepts mm-dd-yyyy only, rejects future dates, and rejects anything
# # older than 41 days. Derive it from today so the fixture does not expire.
# RECENT_DATE = (datetime.today() - timedelta(days=1)).strftime("%m-%d-%Y")

# TEMP_MEDIA_ROOT = tempfile.mkdtemp(prefix="downtime-test-media-")

# # The upload form validates Process and Location against the mapping file, so
# # the pair the fixtures post has to be a real one. Derived rather than written
# # out, so a change to the workbook cannot quietly invalidate it.
# try:
#     from users import mapping as _mapping

#     UPLOAD_PROCESS = _mapping.get_processes()[0]
#     UPLOAD_LOCATION = _mapping.locations_for_process(UPLOAD_PROCESS)[0]
# except Exception:  # no workbook in this environment
#     UPLOAD_PROCESS, UPLOAD_LOCATION = "DIC", "Noida"

# # Never open a real SSH connection to the HRMS host during tests.
# SCP_TARGET = "users.views.approve_file_and_copy"


# class ExplodingEmailBackend(BaseEmailBackend):
#     """Reproduces the Gmail failure seen in production (530 Authentication Required)."""

#     def send_messages(self, email_messages):
#         raise SMTPSenderRefused(
#             530,
#             b"5.7.0 Authentication Required. For more information, go to\n"
#             b"5.7.0 https://support.google.com/accounts/troubleshooter/2402620.",
#             "digx.automation@iccs.in",
#         )


# @override_settings(
#     MEDIA_ROOT=TEMP_MEDIA_ROOT,
#     EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
# )
# class DowntimeWorkflowTestCase(TestCase):
#     """Shared fixtures for the upload -> L1 -> L2 -> HRMS workflow."""

#     @classmethod
#     def tearDownClass(cls):
#         super().tearDownClass()
#         shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)

#     def setUp(self):
#         # No test may reach the Celery broker or launch a real HRMS upload.
#         queue_patch = patch("users.views.upload_to_hrms")
#         self.mock_upload_to_hrms = queue_patch.start()
#         self.addCleanup(queue_patch.stop)

#         # The fixtures' ATS IDs are invented and are not in the mapping file.
#         # What these tests exercise is the date/row rules and the approval
#         # workflow, so the per-ID Process/Location check is stubbed out here.
#         ats_patch = patch(
#             "users.views.mapping.validate_ats_ids", return_value=(True, None)
#         )
#         self.mock_validate_ats_ids = ats_patch.start()
#         self.addCleanup(ats_patch.stop)

#         self.uploader = User.objects.create_user(
#             username="uploader", password="pw", role="regular",
#             email="uploader@iccs.in",
#         )
#         self.l1_a = User.objects.create_user(
#             username="ATS90664", password="pw", role="l1",
#             first_name="First", last_name="Approver", email="l1a@iccs.in",
#         )
#         self.l1_b = User.objects.create_user(
#             username="ATS80133", password="pw", role="l1",
#             first_name="Second", last_name="Approver", email="l1b@iccs.in",
#         )
#         self.l2 = User.objects.create_user(
#             username="l2user", password="pw", role="l2",
#             first_name="Level", last_name="Two", email="l2@iccs.in",
#         )

#         # The only reference file the upload path still reads. It needs at least one
#         # row: the view drops all-empty columns, so a header-only sheet would
#         # validate as having no columns at all.
#         reference_dir = os.path.join(TEMP_MEDIA_ROOT, "reference")
#         os.makedirs(reference_dir, exist_ok=True)
#         pd.DataFrame(
#             {"EmpCode": ["ATS00001"], "Date": [RECENT_DATE], "Minutes": [1]}
#         ).to_excel(
#             os.path.join(reference_dir, "Upload_Format.xlsx"),
#             index=False,
#             engine="openpyxl",
#         )

#     def upload_as(self, user, empcodes=("ATS66261", "ATS67167")):
#         self.client.force_login(user)
#         buffer = BytesIO()
#         pd.DataFrame(
#             {
#                 "EmpCode": list(empcodes),
#                 "Date": [RECENT_DATE] * len(empcodes),
#                 "Minutes": [30] * len(empcodes),
#             }
#         ).to_excel(buffer, index=False, engine="openpyxl")
#         buffer.seek(0)
#         return self.client.post(
#             "/home/",
#             {
#                 "process": UPLOAD_PROCESS,
#                 "location": UPLOAD_LOCATION,
#                 "file": SimpleUploadedFile(
#                     "downtime.xlsx", buffer.read(), content_type=XLSX_CONTENT_TYPE
#                 ),
#             },
#         )

#     def post_df(self, df, user=None):
#         """Upload an arbitrary dataframe, for exercising validation rules."""
#         self.client.force_login(user or self.uploader)
#         buffer = BytesIO()
#         df.to_excel(buffer, index=False, engine="openpyxl")
#         buffer.seek(0)
#         return self.client.post(
#             "/home/",
#             {
#                 "process": UPLOAD_PROCESS,
#                 "location": UPLOAD_LOCATION,
#                 "file": SimpleUploadedFile(
#                     "downtime.xlsx", buffer.read(), content_type=XLSX_CONTENT_TYPE
#                 ),
#             },
#         )

#     @staticmethod
#     def rows(count, days_old=1):
#         date = (datetime.today() - timedelta(days=days_old)).strftime("%m-%d-%Y")
#         return pd.DataFrame(
#             {
#                 "EmpCode": [f"ATS{i:06d}" for i in range(1, count + 1)],
#                 "Date": [date] * count,
#                 "Minutes": [30] * count,
#             }
#         )

#     @staticmethod
#     def messages_of(response):
#         return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]

#     def pending_file(self):
#         return UploadedFile.objects.get(is_split=True)


# class UploadWritesToL1ApprovedTests(DowntimeWorkflowTestCase):
#     """Upload writes one unmodified file into media/L1_approved/."""

#     def test_single_file_lands_in_l1_approved_folder(self):
#         self.upload_as(self.uploader)

#         rows = UploadedFile.objects.filter(is_split=True)
#         self.assertEqual(rows.count(), 1)

#         stored = rows.first().file.name
#         self.assertTrue(
#             stored.startswith("L1_approved/"),
#             f"expected L1_approved/ prefix, got {stored!r}",
#         )
#         self.assertNotIn("\\", stored)  # forward slashes only, for the media URL
#         self.assertTrue(os.path.exists(os.path.join(TEMP_MEDIA_ROOT, stored)))

#     def test_file_name_carries_no_cluster_head_suffix(self):
#         self.upload_as(self.uploader)
#         name = os.path.basename(self.pending_file().file.name)
#         self.assertNotIn("ATS90664", name)
#         self.assertNotIn("ATS80133", name)

#     def test_file_is_not_enriched_with_process_or_cluster_head(self):
#         self.upload_as(self.uploader)

#         path = os.path.join(TEMP_MEDIA_ROOT, self.pending_file().file.name)
#         df = pd.read_excel(path, engine="openpyxl")

#         self.assertEqual(list(df.columns), ["EmpCode", "Date", "Minutes"])
#         self.assertNotIn("Process", df.columns)
#         self.assertNotIn("CH_ATSID", df.columns)
#         self.assertEqual(len(df), 2)

#     def test_upload_succeeds_without_the_mapping_file(self):
#         """ats_process_ch.xlsx is never read, so its absence must not matter."""
#         self.assertFalse(
#             os.path.exists(os.path.join(TEMP_MEDIA_ROOT, "process_ch"))
#         )
#         response = self.upload_as(self.uploader)
#         text = " ".join(b for _, b in self.messages_of(response))

#         self.assertIn("sent for approval", text)
#         self.assertNotIn("processing failed", text)


# class UploadValidationTests(DowntimeWorkflowTestCase):
#     """Age, size and future-date rules. All reject before anything is saved."""

#     def assert_rejected(self, response, fragment):
#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn(fragment, text)
#         # Nothing may be persisted: not the raw upload, not the L1 file.
#         self.assertEqual(
#             UploadedFile.objects.count(), 0,
#             "a rejected upload must not create any database row",
#         )

#    # --- Rule 1: 35 days old or older is rejected -------------------------

#     def test_file_exactly_35_days_old_is_rejected(self):
#         response = self.post_df(self.rows(2, days_old=35))
#         self.assert_rejected(response, "older than 35 days")


#     def test_file_older_than_35_days_is_rejected(self):
#         response = self.post_df(self.rows(2, days_old=45))
#         self.assert_rejected(response, "older than 35 days")


#     def test_file_34_days_old_is_accepted(self):
#         response = self.post_df(self.rows(2, days_old=34))
#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("sent for approval", text)
#         self.assertEqual(
#             UploadedFile.objects.filter(is_split=True).count(),
#             1
#         )
#     # --- Rule 2: maximum 300 rows ----------------------------------------
#     def test_file_with_301_rows_is_rejected(self):
#         response = self.post_df(self.rows(301))
#         self.assert_rejected(response, "300")

#     def test_file_with_exactly_300_rows_is_accepted(self):
#         response = self.post_df(self.rows(300))
#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("sent for approval", text)
#         self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 1)

#     # --- Rule 3: future dates are rejected --------------------------------
#     def test_future_dated_file_is_rejected(self):
#         response = self.post_df(self.rows(2, days_old=-1))
#         self.assert_rejected(response, "Future Date")

#     def test_todays_date_is_accepted(self):
#         response = self.post_df(self.rows(2, days_old=0))
#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("sent for approval", text)


# class L1QueueTests(DowntimeWorkflowTestCase):
#     """The L1 queue is shared, but legacy split files stay with their Cluster Head."""

#     def test_new_file_is_visible_to_every_l1_user(self):
#         self.upload_as(self.uploader)

#         for l1 in (self.l1_a, self.l1_b):
#             self.client.force_login(l1)
#             response = self.client.get("/approve/")
#             self.assertEqual(
#                 [f.id for f in response.context["files"]],
#                 [self.pending_file().id],
#                 f"{l1.username} should see the shared file",
#             )

#     def test_legacy_file_is_visible_only_to_its_cluster_head(self):
#         legacy = UploadedFile.objects.create(
#             file="L1_approved/2026-01-01_abcd1234_ATS90664.csv",
#             uploaded_by=self.uploader,
#             is_valid_format=True,
#             is_split=True,
#             legacy_ch_routed=True,
#         )

#         self.client.force_login(self.l1_a)
#         self.assertIn(
#             legacy.id, [f.id for f in self.client.get("/approve/").context["files"]]
#         )

#         self.client.force_login(self.l1_b)
#         self.assertNotIn(
#             legacy.id, [f.id for f in self.client.get("/approve/").context["files"]]
#         )


# class ApprovalChainTests(DowntimeWorkflowTestCase):
#     """Upload -> L1 -> L2 -> HRMS. Only L2 approval reaches HRMS."""

#     def setUp(self):
#         super().setUp()
#         self.upload_as(self.uploader)
#         self.file = self.pending_file()

#     def approve_as(self, user, url):
#         self.client.force_login(user)
#         return self.client.post(url, {"file_id": self.file.id, "approve": "1"})

#     def unlock_as(self, user):
#         self.client.force_login(user)
#         return self.client.post("/approve/", {
#             "file_id": self.file.id,
#             "unlock_and_save": "1",
#         })

#     def release_to_l2(self):
#         """The normal path: L1 unlocks, then L2 decides."""
#         return self.unlock_as(self.l1_a)

#     @patch(SCP_TARGET)
#     def test_unlocking_does_not_push_to_hrms(self, mock_scp):
#         self.release_to_l2()

#         self.file.refresh_from_db()
#         self.assertEqual(self.file.l1_status, "unlock")
#         self.assertEqual(self.file.l1_status_by, self.l1_a)
#         self.assertIsNotNone(self.file.l1_status_at)
#         self.assertFalse(self.file.approved_by_l2)
#         # The retired L1 approval step is not written any more.
#         self.assertFalse(self.file.approved_by_l1)
#         mock_scp.assert_not_called()

#     @patch(SCP_TARGET)
#     def test_l2_sees_a_locked_file_in_the_lock_tab_only(self, _mock_scp):
#         """Not held back from L2, but listed where it cannot be actioned."""
#         self.client.force_login(self.l2)
#         response = self.client.get("/approve-l2/")

#         self.assertEqual(list(response.context["files"]), [], "not approvable")
#         self.assertEqual(
#             [f.id for f in response.context["locked_files"]], [self.file.id]
#         )
#         self.file.refresh_from_db()
#         self.assertEqual(self.file.l1_status, "")
#         self.assertContains(response, "Pending Approvals for Lock Files")
#         self.assertContains(response, "Lock")

#     @patch(SCP_TARGET)
#     def test_unlocking_moves_the_file_into_the_unlock_tab(self, _mock_scp):
#         self.release_to_l2()

#         self.client.force_login(self.l2)
#         response = self.client.get("/approve-l2/")

#         self.assertEqual([f.id for f in response.context["files"]], [self.file.id])
#         self.assertEqual(response.context["files"][0].l1_status, "unlock")
#         self.assertEqual(list(response.context["locked_files"]), [],
#                          "and out of the lock tab")
#         self.assertContains(response, "Pending Approvals for Unlock Files")
#         self.assertContains(response, "Unlock")

#     @patch(SCP_TARGET)
#     def test_failed_delivery_releases_the_approval(self, mock_scp):
#         """A file that never reached HRMS must not be recorded as delivered."""
#         mock_scp.return_value = False  # transfer failed

#         self.release_to_l2()
#         response = self.approve_as(self.l2, "/approve-l2/")

#         self.file.refresh_from_db()
#         self.assertFalse(self.file.approved_by_l2)
#         self.assertIsNone(self.file.approved_by_l2_user)
#         self.assertIsNone(self.file.approved_at_l2)

#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("could NOT be delivered to HRMS", text)

#         # Still retryable.
#         self.client.force_login(self.l2)
#         self.assertEqual(
#             [f.id for f in self.client.get("/approve-l2/").context["files"]],
#             [self.file.id],
#         )

#     @patch(SCP_TARGET)
#     def test_l2_approval_pushes_to_hrms_once(self, mock_scp):
#         self.release_to_l2()
#         self.approve_as(self.l2, "/approve-l2/")

#         self.file.refresh_from_db()
#         self.assertTrue(self.file.approved_by_l2)
#         self.assertEqual(self.file.approved_by_l2_user, self.l2)
#         self.assertIsNotNone(self.file.approved_at_l2)
#         mock_scp.assert_called_once()

#     @patch(SCP_TARGET)
#     def test_hrms_upload_is_queued_with_the_l2_approved_csv(self, mock_scp):
#         """The exact media/L2_approved CSV path must reach the Celery task."""
#         approved_csv = os.path.join(
#             TEMP_MEDIA_ROOT, "L2_approved", "2026-01-01_deadbeef.csv"
#         )
#         mock_scp.return_value = approved_csv

#         self.release_to_l2()
#         self.mock_upload_to_hrms.reset_mock()
#         self.approve_as(self.l2, "/approve-l2/")

#         self.mock_upload_to_hrms.delay.assert_called_once_with(approved_csv)

#         queued = self.mock_upload_to_hrms.delay.call_args.args[0]
#         self.assertIn("L2_approved", queued)
#         self.assertTrue(queued.endswith(".csv"))
#         self.assertNotIn("Revenue", queued)
#         self.assertNotIn("L1_approved", queued)

#     @patch(SCP_TARGET)
#     def test_hrms_upload_is_not_queued_when_delivery_fails(self, mock_scp):
#         mock_scp.return_value = None  # nothing was produced or delivered

#         self.release_to_l2()
#         self.mock_upload_to_hrms.reset_mock()
#         self.approve_as(self.l2, "/approve-l2/")

#         self.mock_upload_to_hrms.delay.assert_not_called()

#     @patch(SCP_TARGET)
#     def test_approval_survives_a_broker_outage(self, mock_scp):
#         mock_scp.return_value = "/tmp/x.csv"
#         self.mock_upload_to_hrms.delay.side_effect = OSError("broker unreachable")

#         self.release_to_l2()
#         response = self.approve_as(self.l2, "/approve-l2/")

#         self.file.refresh_from_db()
#         self.assertTrue(self.file.approved_by_l2)
#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("could not be queued", text)

#     @patch(SCP_TARGET)
#     def test_second_l2_approval_does_not_push_again(self, mock_scp):
#         self.release_to_l2()
#         self.approve_as(self.l2, "/approve-l2/")

#         other_l2 = User.objects.create_user(
#             username="l2other", password="pw", role="l2"
#         )
#         response = self.approve_as(other_l2, "/approve-l2/")

#         mock_scp.assert_called_once()
#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("already been actioned", text)

#     @patch(SCP_TARGET)
#     def test_l2_rejection_is_terminal_and_skips_hrms(self, mock_scp):
#         self.release_to_l2()

#         self.client.force_login(self.l2)
#         self.client.post(
#             "/approve-l2/",
#             {"file_id": self.file.id, "reject": "1", "rejection_reason": "bad data"},
#         )

#         self.file.refresh_from_db()
#         self.assertTrue(self.file.rejected_by_l2)
#         self.assertEqual(self.file.rejection_reason_l2, "bad data")
#         self.assertFalse(self.file.approved_by_l2)
#         mock_scp.assert_not_called()

#         self.client.force_login(self.l2)
#         self.assertEqual(list(self.client.get("/approve-l2/").context["files"]), [])

#     @patch(SCP_TARGET)
#     def test_unlocking_moves_the_file_to_your_decision(self, _mock_scp):
#         """It leaves the working list, lands in Your Decision, and reaches L2."""
#         self.client.force_login(self.l1_a)
#         page = self.client.get("/approve/")
#         self.assertEqual([f.id for f in page.context["files"]], [self.file.id])
#         self.assertEqual(list(page.context["decided_files"]), [])

#         self.release_to_l2()

#         page = self.client.get("/approve/")
#         self.assertEqual(list(page.context["files"]), [], "left the working list")
#         self.assertEqual(
#             [f.id for f in page.context["decided_files"]], [self.file.id],
#             "arrived in Your Decision",
#         )
#         self.assertEqual(page.context["decided_files"][0].l1_status, "unlock")

#         # And it is in front of L2, marked Unlock.
#         self.client.force_login(self.l2)
#         l2_page = self.client.get("/approve-l2/")
#         self.assertEqual([f.id for f in l2_page.context["files"]], [self.file.id])
#         self.assertEqual(l2_page.context["files"][0].l1_status, "unlock")

#     @patch(SCP_TARGET)
#     def test_your_decision_is_per_user(self, _mock_scp):
#         """Another L1 user's decision is not listed as yours."""
#         self.release_to_l2()  # l1_a unlocks

#         self.client.force_login(self.l1_b)
#         page = self.client.get("/approve/")

#         self.assertEqual(list(page.context["decided_files"]), [])
#         self.assertEqual(list(page.context["files"]), [], "and it is decided, so not to mark")

#     @patch(SCP_TARGET)
#     def test_your_decision_survives_the_l2_decision(self, _mock_scp):
#         """The record stays after L2 acts; it is not a pending-work list."""
#         self.release_to_l2()
#         self.approve_as(self.l2, "/approve-l2/")

#         self.client.force_login(self.l1_a)
#         page = self.client.get("/approve/")
#         self.assertEqual(
#             [f.id for f in page.context["decided_files"]], [self.file.id]
#         )

#     @patch(SCP_TARGET)
#     def test_unlocking_is_idempotent_and_records_the_last_l1_user(self, _mock_scp):
#         self.release_to_l2()
#         self.file.refresh_from_db()
#         self.assertEqual(self.file.l1_status_by, self.l1_a)

#         self.unlock_as(self.l1_b)

#         self.file.refresh_from_db()
#         self.assertEqual(self.file.l1_status, "unlock")
#         self.assertEqual(self.file.l1_status_by, self.l1_b)

#     @patch(SCP_TARGET)
#     def test_status_is_frozen_once_l2_has_approved(self, _mock_scp):
#         self.release_to_l2()                      # l1_a unlocks it
#         self.approve_as(self.l2, "/approve-l2/")  # L2 approves it

#         response = self.unlock_as(self.l1_b)      # a late press by another L1

#         self.file.refresh_from_db()
#         self.assertEqual(self.file.l1_status, "unlock")
#         self.assertEqual(self.file.l1_status_by, self.l1_a, "stamp unchanged")
#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("already been actioned by L2", text)

#     @patch(SCP_TARGET)
#     def test_a_locked_file_cannot_be_approved(self, mock_scp):
#         """No button for it, and the view refuses a posted approval too."""
#         self.file.refresh_from_db()
#         self.assertEqual(self.file.l1_status, "")  # never unlocked

#         response = self.approve_as(self.l2, "/approve-l2/")

#         self.file.refresh_from_db()
#         self.assertFalse(self.file.approved_by_l2)
#         mock_scp.assert_not_called()
#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("is locked", text)

#     @patch(SCP_TARGET)
#     def test_a_locked_file_cannot_be_rejected(self, mock_scp):
#         self.client.force_login(self.l2)
#         response = self.client.post("/approve-l2/", {
#             "file_id": self.file.id, "reject": "1", "rejection_reason": "no",
#         })

#         self.file.refresh_from_db()
#         self.assertFalse(self.file.rejected_by_l2)
#         mock_scp.assert_not_called()
#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("is locked", text)

#     @patch(SCP_TARGET)
#     def test_the_lock_tab_offers_no_approve_or_reject(self, _mock_scp):
#         self.client.force_login(self.l2)
#         html = self.client.get("/approve-l2/").content.decode()

#         tab = html[html.index('id="locked"'):html.index('id="history"')]
#         self.assertNotIn('name="approve"', tab)
#         self.assertNotIn('name="reject"', tab)
#         self.assertIn(">View</a>", tab)
#         self.assertIn(">Download</a>", tab)

#     @patch(SCP_TARGET)
#     def test_unlocking_then_approving_still_reaches_hrms(self, mock_scp):
#         """The unchanged path: L1 unlocks, L2 approves, the file goes to HRMS."""
#         self.release_to_l2()

#         self.approve_as(self.l2, "/approve-l2/")

#         self.file.refresh_from_db()
#         self.assertTrue(self.file.approved_by_l2)
#         mock_scp.assert_called_once()
#         self.mock_upload_to_hrms.delay.assert_called_once()

#     @patch(SCP_TARGET)
#     def test_l1_can_no_longer_approve_or_reject(self, mock_scp):
#         """The retired L1 actions are gone: posting them changes nothing."""
#         self.client.force_login(self.l1_a)
#         self.client.post("/approve/", {"file_id": self.file.id, "approve": "1"})
#         self.client.post("/approve/", {
#             "file_id": self.file.id, "reject": "1", "rejection_reason": "nope",
#         })

#         self.file.refresh_from_db()
#         self.assertFalse(self.file.approved_by_l1)
#         self.assertFalse(self.file.rejected_by_l1)
#         self.assertEqual(self.file.l1_status, "")  # still Lock
#         mock_scp.assert_not_called()


# class HrmsOutputTests(DowntimeWorkflowTestCase):
#     """The file pushed to HRMS is built in media/L2_approved/."""

#     def setUp(self):
#         super().setUp()
#         self.upload_as(self.uploader)
#         self.file = self.pending_file()

#     @patch("users.views.SCPClient")
#     @patch("users.views.paramiko")
#     def test_approved_csv_is_written_to_l2_approved(self, _ssh, _scp):
#         from users.views import approve_file_and_copy

#         approve_file_and_copy(self.file)

#         out_dir = os.path.join(TEMP_MEDIA_ROOT, "L2_approved")
#         produced = os.listdir(out_dir)
#         self.assertEqual(len(produced), 1)
#         self.assertTrue(produced[0].endswith(".csv"))  # always CSV, even from xlsx

#         df = pd.read_csv(os.path.join(out_dir, produced[0]))
#         self.assertEqual(list(df.columns), ["EmpCode", "Date", "Minutes", "IsWH"])
#         self.assertTrue((df["IsWH"] == "N").all())

#     @patch("users.views.SCPClient")
#     @patch("users.views.paramiko")
#     def test_legacy_columns_are_stripped_before_hrms(self, _ssh, _scp):
#         """Older files still carry Process/CH_ATSID and must be cleaned."""
#         from users.views import approve_file_and_copy

#         legacy_dir = os.path.join(TEMP_MEDIA_ROOT, "L1_approved")
#         os.makedirs(legacy_dir, exist_ok=True)
#         legacy_name = "L1_approved/legacy_ATS90664.csv"
#         pd.DataFrame(
#             {
#                 "EmpCode": ["ATS66261"],
#                 "Date": [RECENT_DATE],
#                 "Minutes": [30],
#                 "Process": ["Max life-SDPL"],
#                 "CH_ATSID": ["ATS90664"],
#             }
#         ).to_csv(os.path.join(TEMP_MEDIA_ROOT, legacy_name), index=False)

#         legacy = UploadedFile.objects.create(
#             file=legacy_name, uploaded_by=self.uploader,
#             is_valid_format=True, is_split=True, legacy_ch_routed=True,
#         )
#         approve_file_and_copy(legacy)

#         out = os.path.join(TEMP_MEDIA_ROOT, "L2_approved", "legacy_ATS90664.csv")
#         df = pd.read_csv(out)
#         self.assertEqual(list(df.columns), ["EmpCode", "Date", "Minutes", "IsWH"])


# class ViewStoresFileInDatabaseTests(DowntimeWorkflowTestCase):
#     """View copies the file into the database, then renders from that copy."""

#     def setUp(self):
#         super().setUp()
#         self.upload_as(self.uploader)
#         self.file = self.pending_file()
#         self.path = os.path.join(TEMP_MEDIA_ROOT, self.file.file.name)

#     def view(self, user=None):
#         self.client.force_login(user or self.uploader)
#         return self.client.get(f"/file/{self.file.id}/view/")

#     def test_the_upload_is_stored_immediately(self):
#         """Bytes are kept from the moment the file is created, not on first View.

#         A file whose media was lost before anyone previewed it could not be
#         delivered to L2 and could not stop being retried, so waiting for a View
#         was too late.
#         """
#         stored = StoredFile.objects.get(uploaded_file=self.file)

#         with open(self.path, "rb") as handle:
#             self.assertEqual(bytes(stored.content), handle.read())

#     def test_view_stores_the_file_and_renders_it(self):
#         response = self.view()

#         stored = StoredFile.objects.get(uploaded_file=self.file)
#         with open(self.path, "rb") as handle:
#             on_disk = handle.read()

#         self.assertEqual(bytes(stored.content), on_disk, "byte-for-byte copy")
#         self.assertEqual(stored.size, len(on_disk))
#         self.assertEqual(stored.sha256, hashlib.sha256(on_disk).hexdigest())
#         self.assertEqual(response.status_code, 200)
#         self.assertContains(response, "EmpCode")
#         self.assertContains(response, "ATS66261")

#     def test_a_second_view_reuses_the_stored_row(self):
#         self.view()
#         stored_at = StoredFile.objects.get(uploaded_file=self.file).stored_at

#         self.view()

#         self.assertEqual(StoredFile.objects.filter(uploaded_file=self.file).count(), 1)
#         self.assertEqual(
#             StoredFile.objects.get(uploaded_file=self.file).stored_at, stored_at,
#             "not rewritten on every view",
#         )

#     def test_the_file_still_displays_once_the_disk_copy_is_gone(self):
#         self.view()                      # stores it
#         os.remove(self.path)             # the disk copy disappears
#         self.assertFalse(os.path.exists(self.path))

#         response = self.view()           # served from the database

#         self.assertEqual(response.status_code, 200)
#         self.assertContains(response, "ATS66261")
#         self.assertNotContains(response, "no longer available")

#     def test_the_rendered_rows_come_from_the_stored_bytes(self):
#         self.view()

#         # Rewrite the stored copy; the page must follow the database, not disk.
#         marker = b"EmpCode,Date,Minutes\nATS99999,01-01-2026,7\n"
#         UploadedFile.objects.filter(id=self.file.id).update(file="uploads/x.csv")
#         StoredFile.objects.filter(uploaded_file=self.file).update(
#             content=marker, size=len(marker),
#             sha256=hashlib.sha256(marker).hexdigest(),
#         )

#         response = self.view()

#         self.assertContains(response, "ATS99999")
#         self.assertNotContains(response, "ATS66261")

#     def test_a_never_viewed_file_survives_losing_its_disk_copy(self):
#         """The stored copy carries it, even though nobody ever pressed View."""
#         os.remove(self.path)

#         response = self.view()

#         self.assertEqual(response.status_code, 200)
#         self.assertContains(response, "ATS66261")
#         self.assertNotContains(response, "no longer available")

#     def test_a_record_with_no_bytes_anywhere_reports_unavailable(self):
#         """The genuinely unrecoverable case: no disk copy and no stored copy."""
#         StoredFile.objects.filter(uploaded_file=self.file).delete()
#         os.remove(self.path)

#         response = self.view()

#         self.assertContains(response, "no longer available")

#     def test_source_availability_drives_the_l2_guard(self):
#         """L2 must not be offered an approval that could only fail."""
#         from users.views import source_is_available

#         self.assertTrue(source_is_available(self.file), "on disk and stored")

#         os.remove(self.path)
#         self.assertTrue(source_is_available(self.file), "stored copy still carries it")

#         StoredFile.objects.filter(uploaded_file=self.file).delete()
#         self.assertFalse(source_is_available(self.file), "nothing left anywhere")

#     def test_download_still_points_at_the_file_on_disk(self):
#         """Requirement 4: Download is untouched by any of this."""
#         self.view()

#         self.client.force_login(self.uploader)
#         html = self.client.get("/home/").content.decode()

#         self.assertIn(f'href="{self.file.file.url}" download', html)
#         self.assertTrue(os.path.exists(self.path))

#     def test_storing_does_not_disturb_the_workflow(self):
#         """The approval fields are untouched by a preview."""
#         before = UploadedFile.objects.filter(id=self.file.id).values().first()

#         self.view(self.l1_a)
#         self.view(self.l2)

#         after = UploadedFile.objects.filter(id=self.file.id).values().first()
#         self.assertEqual(before, after)


# class EmpCodeNormalisationTests(DowntimeWorkflowTestCase):
#     """An EmpCode is accepted around whitespace/case, and stored canonically."""

#     ACCEPTED = ["ATS4575", "ATS4575 ", " ATS4575", "Ats4575", "ats4575 "]
#     REJECTED = ["ATS", "ATSABC", "AT4575", "ABC4575", "ATS 4575"]

#     def upload_code(self, code):
#         buf = BytesIO()
#         pd.DataFrame({"EmpCode": [code], "Date": [RECENT_DATE], "Minutes": [30]}).to_excel(
#             buf, index=False, engine="openpyxl"
#         )
#         buf.seek(0)
#         self.client.force_login(self.uploader)
#         return self.client.post("/home/", {
#             "process": UPLOAD_PROCESS, "location": UPLOAD_LOCATION,
#             "file": SimpleUploadedFile("dt.xlsx", buf.read(), content_type=XLSX_CONTENT_TYPE),
#         })

#     def test_whitespace_and_case_variants_are_accepted(self):
#         for code in self.ACCEPTED:
#             with self.subTest(code=code):
#                 text = " ".join(b for _, b in self.messages_of(self.upload_code(code)))
#                 self.assertNotIn("Invalid EmpCode", text, f"{code!r} was rejected")

#     def test_malformed_codes_are_rejected(self):
#         for code in self.REJECTED:
#             with self.subTest(code=code):
#                 text = " ".join(b for _, b in self.messages_of(self.upload_code(code)))
#                 self.assertIn("Invalid EmpCode", text, f"{code!r} was accepted")

#     def test_the_saved_file_holds_the_canonical_code(self):
#         """The point of the fix: no stray space reaches L1, L2 or HRMS."""
#         for code in self.ACCEPTED:
#             with self.subTest(code=code):
#                 UploadedFile.objects.all().delete()
#                 self.upload_code(code)
#                 record = UploadedFile.objects.filter(is_split=True).first()
#                 self.assertIsNotNone(record, f"{code!r} produced no upload")
#                 path = os.path.join(TEMP_MEDIA_ROOT, record.file.name)
#                 saved = (
#                     pd.read_csv(path) if path.lower().endswith(".csv")
#                     else pd.read_excel(path, engine="openpyxl")
#                 )
#                 self.assertEqual(list(saved["EmpCode"]), ["ATS4575"])


# class UploadRedirectsAfterPostTests(DowntimeWorkflowTestCase):
#     """Post/Redirect/Get: a refresh must not upload the same file again."""

#     def test_successful_upload_redirects(self):
#         response = self.upload_as(self.uploader)

#         self.assertEqual(response.status_code, 302, "POST should redirect, not render")
#         self.assertEqual(response["Location"], "/home/")

#     def test_the_success_message_survives_the_redirect(self):
#         response = self.client.get(self.upload_as(self.uploader)["Location"])

#         text = " ".join(b for _, b in self.messages_of(response))
#         self.assertIn("sent for approval", text)

#     def refresh(self, response):
#         """Do what a browser reload does, given what the upload returned.

#         After a redirect the address bar holds the GET target, so a reload is a
#         GET. After a rendered 200 the last request was the POST itself, and the
#         browser re-sends it - which is the duplicate this fix exists to stop.
#         Deriving it from the response is what makes this test fail without the
#         fix instead of passing either way.
#         """
#         if response.status_code == 302:
#             return self.client.get(response["Location"])
#         return self.upload_as(self.uploader)

#     def test_refreshing_after_an_upload_does_not_upload_again(self):
#         response = self.upload_as(self.uploader)
#         self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 1)

#         self.refresh(response)
#         self.refresh(response)

#         self.assertEqual(
#             UploadedFile.objects.filter(is_split=True).count(), 1,
#             "a browser refresh uploaded the same file again",
#         )

#     def test_a_rejected_upload_still_redirects(self):
#         """Unchanged behaviour: failures already redirected and still do."""
#         response = self.post_df(self.rows(2, days_old=400))

#         self.assertEqual(response.status_code, 302)
#         self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 0)


# class InactiveAtsIdTests(DowntimeWorkflowTestCase):
#     """An inactive ATS ID is named, and the error outlives a refresh."""

#     INACTIVE = "ATS4575"

#     def setUp(self):
#         super().setUp()
#         # These tests are about the real mapping check, so the shared stub is
#         # replaced with the genuine function for the duration.
#         self.real_check = self.use_real_ats_check()

#     def use_real_ats_check(self):
#         patcher = patch(
#             "users.views.mapping.validate_ats_ids", new=REAL_VALIDATE_ATS_IDS
#         )
#         patcher.start()
#         self.addCleanup(self._safe_stop, patcher)
#         return patcher

#     @staticmethod
#     def _safe_stop(patcher):
#         try:
#             patcher.stop()
#         except RuntimeError:
#             pass  # already stopped by a test

#     def upload(self, codes):
#         buf = BytesIO()
#         pd.DataFrame({
#             "EmpCode": list(codes),
#             "Date": [RECENT_DATE] * len(codes),
#             "Minutes": [30] * len(codes),
#         }).to_excel(buf, index=False, engine="openpyxl")
#         buf.seek(0)
#         self.client.force_login(self.uploader)
#         return self.client.post("/home/", {
#             "process": UPLOAD_PROCESS, "location": UPLOAD_LOCATION,
#             "file": SimpleUploadedFile("dt.xlsx", buf.read(), content_type=XLSX_CONTENT_TYPE),
#         })

#     def page(self):
#         self.client.force_login(self.uploader)
#         return self.client.get("/home/")

#     def test_the_inactive_id_is_named(self):
#         self.upload([self.INACTIVE])

#         self.assertContains(self.page(), "Inactive ATSid: %s" % self.INACTIVE)

#     def test_every_inactive_id_is_listed(self):
#         self.upload([self.INACTIVE, "ATS9999999"])

#         body = self.page().content.decode()
#         self.assertIn(self.INACTIVE, body)
#         self.assertIn("ATS9999999", body)

#     def test_no_file_is_created_for_an_inactive_id(self):
#         self.upload([self.INACTIVE])

#         self.assertEqual(UploadedFile.objects.count(), 0)

#     def test_the_error_survives_repeated_refreshes(self):
#         """The point: the messages framework would drop it after one render."""
#         self.upload([self.INACTIVE])

#         for attempt in range(3):
#             self.assertContains(
#                 self.page(), "Inactive ATSid",
#                 msg_prefix="lost after %d refresh(es)" % attempt,
#             )

#     def test_a_successful_upload_clears_the_error(self):
#         self.upload([self.INACTIVE])
#         self.assertContains(self.page(), "Inactive ATSid")

#         # Back to the permissive stub, so the next file uploads cleanly.
#         self.real_check.stop()
#         self.upload(["ATS66261"])

#         self.assertNotContains(self.page(), "Inactive ATSid")
#         self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 1)


# class PendingReminderEmailTests(DowntimeWorkflowTestCase):
#     """The 10:00 digests: one mail per stage, only what is still outstanding."""

#     def setUp(self):
#         super().setUp()
#         self.upload_as(self.uploader)
#         self.file = self.pending_file()
#         mail.outbox = []

#     def run_both(self):
#         from users.tasks import notify_l1_pending_files, notify_l2_pending_files
#         return notify_l1_pending_files(), notify_l2_pending_files()

#     # The workflow helpers live on ApprovalChainTests; repeated here rather than
#     # moved, so that class keeps working exactly as it does.
#     def release_to_l2(self):
#         self.client.force_login(self.l1_a)
#         return self.client.post("/approve/", {
#             "file_id": self.file.id, "unlock_and_save": "1",
#         })

#     def approve_as(self, user, url):
#         self.client.force_login(user)
#         return self.client.post(url, {"file_id": self.file.id, "approve": "1"})

#     def test_a_new_upload_is_reported_to_l1_only(self):
#         """Not yet unlocked, so it is L1's to action, not L2's."""
#         l1_result, l2_result = self.run_both()

#         self.assertEqual(len(mail.outbox), 1)
#         sent = mail.outbox[0]
#         self.assertEqual(sent.to, [settings.L1_PENDING_NOTIFY_EMAIL])
#         self.assertEqual(sent.subject, "Pending Files - Lock/Unlock Required")
#         self.assertIn("pending for Lock/Unlock action", sent.body)
#         self.assertIn(os.path.basename(self.file.file.name), sent.body)
#         self.assertIn("Nothing pending for L2", l2_result)

#     def test_once_unlocked_it_moves_to_the_l2_digest(self):
#         self.release_to_l2()
#         mail.outbox = []

#         self.run_both()

#         self.assertEqual(len(mail.outbox), 1)
#         sent = mail.outbox[0]
#         self.assertEqual(sent.to, [settings.L2_PENDING_NOTIFY_EMAIL])
#         self.assertEqual(sent.subject, "Pending Files - Approval Required")
#         self.assertIn("pending for approval", sent.body)
#         self.assertIn(os.path.basename(self.file.file.name), sent.body)

#     @patch(SCP_TARGET)
#     def test_an_approved_file_is_reported_to_nobody(self, _mock_scp):
#         """The core rule: processed files must never appear in a reminder."""
#         self.release_to_l2()
#         self.approve_as(self.l2, "/approve-l2/")
#         mail.outbox = []

#         l1_result, l2_result = self.run_both()

#         self.assertEqual(len(mail.outbox), 0, "an approved file was mailed about")
#         self.assertIn("Nothing pending", l1_result)
#         self.assertIn("Nothing pending", l2_result)

#     def test_nothing_pending_sends_no_mail(self):
#         UploadedFile.objects.all().delete()

#         l1_result, l2_result = self.run_both()

#         self.assertEqual(len(mail.outbox), 0)
#         self.assertIn("Nothing pending", l1_result)
#         self.assertIn("Nothing pending", l2_result)

#     def test_several_pending_files_go_in_one_email(self):
#         for _ in range(2):
#             self.upload_as(self.uploader)
#         pending = UploadedFile.objects.filter(is_split=True, l1_status="")
#         self.assertGreater(pending.count(), 1)
#         mail.outbox = []

#         self.run_both()

#         self.assertEqual(len(mail.outbox), 1, "one digest per recipient, not one per file")
#         body = mail.outbox[0].body
#         for record in pending:
#             self.assertIn(os.path.basename(record.file.name), body)

#     def test_the_schedule_is_ten_am_local(self):
#         from django.conf import settings as dj
#         schedule = dj.CELERY_BEAT_SCHEDULE
#         self.assertEqual(dj.CELERY_TIMEZONE, dj.TIME_ZONE)
#         for entry in schedule.values():
#             self.assertEqual(entry["schedule"].hour, {10})
#             self.assertEqual(entry["schedule"].minute, {0})


# class LockedAttendanceMessageTests(DowntimeWorkflowTestCase):
#     """A locked attendance period reads as that, not as a delivery fault."""

#     LOCKED = ("HRMS rejected all 23 record(s). "
#               "Reason: Attendence is already processed")

#     def setUp(self):
#         super().setUp()
#         self.upload_as(self.uploader)
#         self.file = self.pending_file()
#         self.client.force_login(self.l1_a)
#         self.client.post("/approve/", {"file_id": self.file.id, "unlock_and_save": "1"})

#     def render_l2(self):
#         from users.views import source_is_available
#         base = dict(is_split=True, rejected_by_l1=False,
#                     approved_by_l2=False, rejected_by_l2=False)
#         pend = list(UploadedFile.objects.filter(**base))
#         for f in pend:
#             f.source_available = source_is_available(f)

#         class Req:
#             def __init__(self, user):
#                 self.user = user

#         return render_to_string("users/approval_l2.html", {
#             "request": Req(self.l2), "messages": [],
#             "files": [f for f in pend if f.l1_status == "unlock"],
#             "locked_files": [f for f in pend if f.l1_status != "unlock"],
#             "approved_files": [], "rejected_files": [],
#         })

#     def mark_failed(self, reason):
#         UploadedFile.objects.filter(pk=self.file.pk).update(
#             last_delivery_error=reason, delivery_attempts=1,
#             last_delivery_attempt_at=now(),
#         )

#     def test_a_locked_period_is_named_as_such(self):
#         self.mark_failed(self.LOCKED)

#         html = self.render_l2()

#         self.assertIn("Attendance is already Processed", html)
#         self.assertIn("HRMS Locked", html)

#     def test_the_record_count_is_not_shown(self):
#         """The count is noise here, and is not hardcoded anywhere."""
#         self.mark_failed(self.LOCKED)

#         self.assertNotIn("rejected all 23 record(s)", self.render_l2())

#     def test_capitalisation_from_hrms_does_not_matter(self):
#         self.mark_failed("HRMS rejected all 6 record(s). "
#                          "Reason: Attendance is Already Processed")

#         self.assertIn("HRMS Locked", self.render_l2())

#     def test_other_failures_keep_their_own_message(self):
#         for reason in ("HRMS rejected all 5 record(s). Reason: Invalid EmpCode",
#                        "HRMS rejected the login (portal said: 'valid password').",
#                        "The file could not be copied to the HRMS host."):
#             with self.subTest(reason=reason):
#                 self.mark_failed(reason)
#                 html = self.render_l2()
#                 self.assertIn("Delivery failed", html)
#                 self.assertNotIn("HRMS Locked", html)

#     def test_a_file_with_no_failure_is_unaffected(self):
#         html = self.render_l2()

#         self.assertIn("Pending L2", html)
#         self.assertNotIn("HRMS Locked", html)


# class AccessControlTests(DowntimeWorkflowTestCase):
#     def test_l2_user_lands_on_l2_page_after_login(self):
#         response = self.client.post(
#             "/login/", {"username": "l2user", "password": "pw"}
#         )
#         self.assertRedirects(response, "/approve-l2/", fetch_redirect_response=False)

#     def test_l1_user_cannot_use_l2_page(self):
#         self.client.force_login(self.l1_a)
#         response = self.client.get("/approve-l2/")
#         self.assertContains(response, "do not have permission")


# class UploadNotificationEmailTests(DowntimeWorkflowTestCase):
#     """A notification-email failure must not be reported as a processing failure."""

#     @override_settings(EMAIL_BACKEND="users.tests.ExplodingEmailBackend")
#     def test_email_failure_is_not_reported_as_processing_failure(self):
#         response = self.upload_as(self.uploader)
#         text = " ".join(body for _, body in self.messages_of(response))

#         self.assertNotIn("processing failed", text)
#         self.assertIn("notification email could not be sent", text)

#     @override_settings(EMAIL_BACKEND="users.tests.ExplodingEmailBackend")
#     def test_file_still_created_when_email_fails(self):
#         self.upload_as(self.uploader)
#         self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 1)

#     def test_successful_upload_sends_notification(self):
#         from django.core import mail

#         response = self.upload_as(self.uploader)
#         text = " ".join(body for _, body in self.messages_of(response))

#         self.assertIn("sent for approval", text)
#         self.assertNotIn("could not be sent", text)
#         self.assertEqual(len(mail.outbox), 1)

import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from io import BytesIO
from smtplib import SMTPSenderRefused
from unittest.mock import patch

import pandas as pd
from django.core import mail
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.mail.backends.base import BaseEmailBackend
from django.conf import settings
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils.timezone import now

from .models import StoredFile, UploadedFile, User
from users import mapping as users_mapping

# Captured at import time, before any patch replaces it, so a test can put the
# genuine Process/Location check back where the shared fixture stubbed it.
REAL_VALIDATE_ATS_IDS = users_mapping.validate_ats_ids

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

# The view accepts mm-dd-yyyy only, rejects future dates, and rejects anything
# older than 41 days. Derive it from today so the fixture does not expire.
RECENT_DATE = (datetime.today() - timedelta(days=1)).strftime("%m-%d-%Y")

TEMP_MEDIA_ROOT = tempfile.mkdtemp(prefix="downtime-test-media-")

# The upload form validates Process and Location against the mapping file, so
# the pair the fixtures post has to be a real one. Derived rather than written
# out, so a change to the workbook cannot quietly invalidate it.
try:
    from users import mapping as _mapping

    UPLOAD_PROCESS = _mapping.get_processes()[0]
    UPLOAD_LOCATION = _mapping.locations_for_process(UPLOAD_PROCESS)[0]
except Exception:  # no workbook in this environment
    UPLOAD_PROCESS, UPLOAD_LOCATION = "DIC", "Noida"

# Never open a real SSH connection to the HRMS host during tests.
SCP_TARGET = "users.views.approve_file_and_copy"


class ExplodingEmailBackend(BaseEmailBackend):
    """Reproduces the Gmail failure seen in production (530 Authentication Required)."""

    def send_messages(self, email_messages):
        raise SMTPSenderRefused(
            530,
            b"5.7.0 Authentication Required. For more information, go to\n"
            b"5.7.0 https://support.google.com/accounts/troubleshooter/2402620.",
            "digx.automation@iccs.in",
        )


@override_settings(
    MEDIA_ROOT=TEMP_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class DowntimeWorkflowTestCase(TestCase):
    """Shared fixtures for the upload -> L1 -> L2 -> HRMS workflow."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        # No test may reach the Celery broker or launch a real HRMS upload.
        queue_patch = patch("users.views.upload_to_hrms")
        self.mock_upload_to_hrms = queue_patch.start()
        self.addCleanup(queue_patch.stop)

        # The fixtures' ATS IDs are invented and are not in the mapping file.
        # What these tests exercise is the date/row rules and the approval
        # workflow, so the per-ID Process/Location check is stubbed out here.
        ats_patch = patch(
            "users.views.mapping.validate_ats_ids", return_value=(True, None)
        )
        self.mock_validate_ats_ids = ats_patch.start()
        self.addCleanup(ats_patch.stop)

        self.uploader = User.objects.create_user(
            username="uploader", password="pw", role="regular",
            email="uploader@iccs.in",
        )
        self.l1_a = User.objects.create_user(
            username="ATS90664", password="pw", role="l1",
            first_name="First", last_name="Approver", email="l1a@iccs.in",
        )
        self.l1_b = User.objects.create_user(
            username="ATS80133", password="pw", role="l1",
            first_name="Second", last_name="Approver", email="l1b@iccs.in",
        )
        self.l2 = User.objects.create_user(
            username="l2user", password="pw", role="l2",
            first_name="Level", last_name="Two", email="l2@iccs.in",
        )

        # The only reference file the upload path still reads. It needs at least one
        # row: the view drops all-empty columns, so a header-only sheet would
        # validate as having no columns at all.
        reference_dir = os.path.join(TEMP_MEDIA_ROOT, "reference")
        os.makedirs(reference_dir, exist_ok=True)
        pd.DataFrame(
            {"EmpCode": ["ATS00001"], "Date": [RECENT_DATE], "Minutes": [1]}
        ).to_excel(
            os.path.join(reference_dir, "Upload_Format.xlsx"),
            index=False,
            engine="openpyxl",
        )

    def upload_as(self, user, empcodes=("ATS66261", "ATS67167")):
        self.client.force_login(user)
        buffer = BytesIO()
        pd.DataFrame(
            {
                "EmpCode": list(empcodes),
                "Date": [RECENT_DATE] * len(empcodes),
                "Minutes": [30] * len(empcodes),
            }
        ).to_excel(buffer, index=False, engine="openpyxl")
        buffer.seek(0)
        return self.client.post(
            "/home/",
            {
                "process": UPLOAD_PROCESS,
                "location": UPLOAD_LOCATION,
                "file": SimpleUploadedFile(
                    "downtime.xlsx", buffer.read(), content_type=XLSX_CONTENT_TYPE
                ),
            },
        )

    def post_df(self, df, user=None):
        """Upload an arbitrary dataframe, for exercising validation rules."""
        self.client.force_login(user or self.uploader)
        buffer = BytesIO()
        df.to_excel(buffer, index=False, engine="openpyxl")
        buffer.seek(0)
        return self.client.post(
            "/home/",
            {
                "process": UPLOAD_PROCESS,
                "location": UPLOAD_LOCATION,
                "file": SimpleUploadedFile(
                    "downtime.xlsx", buffer.read(), content_type=XLSX_CONTENT_TYPE
                ),
            },
        )

    @staticmethod
    def rows(count, days_old=1):
        date = (datetime.today() - timedelta(days=days_old)).strftime("%m-%d-%Y")
        return pd.DataFrame(
            {
                "EmpCode": [f"ATS{i:06d}" for i in range(1, count + 1)],
                "Date": [date] * count,
                "Minutes": [30] * count,
            }
        )

    @staticmethod
    def messages_of(response):
        return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]

    def pending_file(self):
        return UploadedFile.objects.get(is_split=True)


class UploadWritesToL1ApprovedTests(DowntimeWorkflowTestCase):
    """Upload writes one unmodified file into media/L1_approved/."""

    def test_single_file_lands_in_l1_approved_folder(self):
        self.upload_as(self.uploader)

        rows = UploadedFile.objects.filter(is_split=True)
        self.assertEqual(rows.count(), 1)

        stored = rows.first().file.name
        self.assertTrue(
            stored.startswith("L1_approved/"),
            f"expected L1_approved/ prefix, got {stored!r}",
        )
        self.assertNotIn("\\", stored)  # forward slashes only, for the media URL
        self.assertTrue(os.path.exists(os.path.join(TEMP_MEDIA_ROOT, stored)))

    def test_file_name_carries_no_cluster_head_suffix(self):
        self.upload_as(self.uploader)
        name = os.path.basename(self.pending_file().file.name)
        self.assertNotIn("ATS90664", name)
        self.assertNotIn("ATS80133", name)

    def test_file_is_not_enriched_with_process_or_cluster_head(self):
        self.upload_as(self.uploader)

        path = os.path.join(TEMP_MEDIA_ROOT, self.pending_file().file.name)
        df = pd.read_excel(path, engine="openpyxl")

        self.assertEqual(list(df.columns), ["EmpCode", "Date", "Minutes"])
        self.assertNotIn("Process", df.columns)
        self.assertNotIn("CH_ATSID", df.columns)
        self.assertEqual(len(df), 2)

    def test_upload_succeeds_without_the_mapping_file(self):
        """ats_process_ch.xlsx is never read, so its absence must not matter."""
        self.assertFalse(
            os.path.exists(os.path.join(TEMP_MEDIA_ROOT, "process_ch"))
        )
        response = self.upload_as(self.uploader)
        text = " ".join(b for _, b in self.messages_of(response))

        self.assertIn("sent for approval", text)
        self.assertNotIn("processing failed", text)


class UploadValidationTests(DowntimeWorkflowTestCase):
    """Age, size and future-date rules. All reject before anything is saved."""

    def assert_rejected(self, response, fragment):
        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn(fragment, text)
        # Nothing may be persisted: not the raw upload, not the L1 file.
        self.assertEqual(
            UploadedFile.objects.count(), 0,
            "a rejected upload must not create any database row",
        )

   # --- Rule 1: 45 days old or older is rejected -------------------------

    def test_file_exactly_45_days_old_is_rejected(self):
        response = self.post_df(self.rows(2, days_old=45))
        self.assert_rejected(response, "older than 45 days")


    def test_file_older_than_45_days_is_rejected(self):
        response = self.post_df(self.rows(2, days_old=55))
        self.assert_rejected(response, "older than 45 days")


    def test_file_44_days_old_is_accepted(self):
        response = self.post_df(self.rows(2, days_old=44))
        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("sent for approval", text)
        self.assertEqual(
            UploadedFile.objects.filter(is_split=True).count(),
            1
        )
    # --- Rule 2: maximum 300 rows ----------------------------------------
    def test_file_with_301_rows_is_rejected(self):
        response = self.post_df(self.rows(301))
        self.assert_rejected(response, "300")

    def test_file_with_exactly_300_rows_is_accepted(self):
        response = self.post_df(self.rows(300))
        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("sent for approval", text)
        self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 1)

    # --- Rule 3: future dates are rejected --------------------------------
    def test_future_dated_file_is_rejected(self):
        response = self.post_df(self.rows(2, days_old=-1))
        self.assert_rejected(response, "Future Date")

    def test_todays_date_is_accepted(self):
        response = self.post_df(self.rows(2, days_old=0))
        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("sent for approval", text)


class L1QueueTests(DowntimeWorkflowTestCase):
    """The L1 queue is shared, but legacy split files stay with their Cluster Head."""

    def test_new_file_is_visible_to_every_l1_user(self):
        self.upload_as(self.uploader)

        for l1 in (self.l1_a, self.l1_b):
            self.client.force_login(l1)
            response = self.client.get("/approve/")
            self.assertEqual(
                [f.id for f in response.context["files"]],
                [self.pending_file().id],
                f"{l1.username} should see the shared file",
            )

    def test_legacy_file_is_visible_only_to_its_cluster_head(self):
        legacy = UploadedFile.objects.create(
            file="L1_approved/2026-01-01_abcd1234_ATS90664.csv",
            uploaded_by=self.uploader,
            is_valid_format=True,
            is_split=True,
            legacy_ch_routed=True,
        )

        self.client.force_login(self.l1_a)
        self.assertIn(
            legacy.id, [f.id for f in self.client.get("/approve/").context["files"]]
        )

        self.client.force_login(self.l1_b)
        self.assertNotIn(
            legacy.id, [f.id for f in self.client.get("/approve/").context["files"]]
        )


class ApprovalChainTests(DowntimeWorkflowTestCase):
    """Upload -> L1 -> L2 -> HRMS. Only L2 approval reaches HRMS."""

    def setUp(self):
        super().setUp()
        self.upload_as(self.uploader)
        self.file = self.pending_file()

    def approve_as(self, user, url):
        self.client.force_login(user)
        return self.client.post(url, {"file_id": self.file.id, "approve": "1"})

    def unlock_as(self, user):
        self.client.force_login(user)
        return self.client.post("/approve/", {
            "file_id": self.file.id,
            "unlock_and_save": "1",
        })

    def release_to_l2(self):
        """The normal path: L1 unlocks, then L2 decides."""
        return self.unlock_as(self.l1_a)

    @patch(SCP_TARGET)
    def test_unlocking_does_not_push_to_hrms(self, mock_scp):
        self.release_to_l2()

        self.file.refresh_from_db()
        self.assertEqual(self.file.l1_status, "unlock")
        self.assertEqual(self.file.l1_status_by, self.l1_a)
        self.assertIsNotNone(self.file.l1_status_at)
        self.assertFalse(self.file.approved_by_l2)
        # The retired L1 approval step is not written any more.
        self.assertFalse(self.file.approved_by_l1)
        mock_scp.assert_not_called()

    @patch(SCP_TARGET)
    def test_l2_sees_a_locked_file_in_the_lock_tab_only(self, _mock_scp):
        """Not held back from L2, but listed where it cannot be actioned."""
        self.client.force_login(self.l2)
        response = self.client.get("/approve-l2/")

        self.assertEqual(list(response.context["files"]), [], "not approvable")
        self.assertEqual(
            [f.id for f in response.context["locked_files"]], [self.file.id]
        )
        self.file.refresh_from_db()
        self.assertEqual(self.file.l1_status, "")
        self.assertContains(response, "Pending Approvals for Lock Files")
        self.assertContains(response, "Lock")

    @patch(SCP_TARGET)
    def test_unlocking_moves_the_file_into_the_unlock_tab(self, _mock_scp):
        self.release_to_l2()

        self.client.force_login(self.l2)
        response = self.client.get("/approve-l2/")

        self.assertEqual([f.id for f in response.context["files"]], [self.file.id])
        self.assertEqual(response.context["files"][0].l1_status, "unlock")
        self.assertEqual(list(response.context["locked_files"]), [],
                         "and out of the lock tab")
        self.assertContains(response, "Pending Approvals for Unlock Files")
        self.assertContains(response, "Unlock")

    @patch(SCP_TARGET)
    def test_failed_delivery_keeps_the_approval(self, mock_scp):
        """Once approved, a file leaves the queue for good and is not retried."""
        mock_scp.return_value = False  # transfer failed

        self.release_to_l2()
        response = self.approve_as(self.l2, "/approve-l2/")

        self.file.refresh_from_db()
        self.assertTrue(self.file.approved_by_l2)
        self.assertEqual(self.file.approved_by_l2_user, self.l2)
        self.assertIn("could not be copied", self.file.last_delivery_error)

        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("could NOT be", text)

        # Off the pending queue, onto File Status.
        self.client.force_login(self.l2)
        page = self.client.get("/approve-l2/")
        self.assertEqual(list(page.context["files"]), [])
        self.assertIn(self.file, page.context["approved_files"])

    @patch(SCP_TARGET)
    def test_l2_approval_pushes_to_hrms_once(self, mock_scp):
        self.release_to_l2()
        self.approve_as(self.l2, "/approve-l2/")

        self.file.refresh_from_db()
        self.assertTrue(self.file.approved_by_l2)
        self.assertEqual(self.file.approved_by_l2_user, self.l2)
        self.assertIsNotNone(self.file.approved_at_l2)
        mock_scp.assert_called_once()

    @patch(SCP_TARGET)
    def test_hrms_upload_is_queued_with_the_l2_approved_csv(self, mock_scp):
        """The exact media/L2_approved CSV path must reach the Celery task."""
        approved_csv = os.path.join(
            TEMP_MEDIA_ROOT, "L2_approved", "2026-01-01_deadbeef.csv"
        )
        mock_scp.return_value = approved_csv

        self.release_to_l2()
        self.mock_upload_to_hrms.reset_mock()
        self.approve_as(self.l2, "/approve-l2/")

        self.mock_upload_to_hrms.delay.assert_called_once_with(approved_csv)

        queued = self.mock_upload_to_hrms.delay.call_args.args[0]
        self.assertIn("L2_approved", queued)
        self.assertTrue(queued.endswith(".csv"))
        self.assertNotIn("Revenue", queued)
        self.assertNotIn("L1_approved", queued)

    @patch(SCP_TARGET)
    def test_hrms_upload_is_not_queued_when_delivery_fails(self, mock_scp):
        mock_scp.return_value = None  # nothing was produced or delivered

        self.release_to_l2()
        self.mock_upload_to_hrms.reset_mock()
        self.approve_as(self.l2, "/approve-l2/")

        self.mock_upload_to_hrms.delay.assert_not_called()

    @patch(SCP_TARGET)
    def test_approval_survives_a_broker_outage(self, mock_scp):
        mock_scp.return_value = "/tmp/x.csv"
        self.mock_upload_to_hrms.delay.side_effect = OSError("broker unreachable")

        self.release_to_l2()
        response = self.approve_as(self.l2, "/approve-l2/")

        self.file.refresh_from_db()
        self.assertTrue(self.file.approved_by_l2)
        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("could not be queued", text)

    @patch(SCP_TARGET)
    def test_second_l2_approval_does_not_push_again(self, mock_scp):
        self.release_to_l2()
        self.approve_as(self.l2, "/approve-l2/")

        other_l2 = User.objects.create_user(
            username="l2other", password="pw", role="l2"
        )
        response = self.approve_as(other_l2, "/approve-l2/")

        mock_scp.assert_called_once()
        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("already been actioned", text)

    @patch(SCP_TARGET)
    def test_l2_rejection_is_terminal_and_skips_hrms(self, mock_scp):
        self.release_to_l2()

        self.client.force_login(self.l2)
        self.client.post(
            "/approve-l2/",
            {"file_id": self.file.id, "reject": "1", "rejection_reason": "bad data"},
        )

        self.file.refresh_from_db()
        self.assertTrue(self.file.rejected_by_l2)
        self.assertEqual(self.file.rejection_reason_l2, "bad data")
        self.assertFalse(self.file.approved_by_l2)
        mock_scp.assert_not_called()

        self.client.force_login(self.l2)
        self.assertEqual(list(self.client.get("/approve-l2/").context["files"]), [])

    @patch(SCP_TARGET)
    def test_unlocking_moves_the_file_to_your_decision(self, _mock_scp):
        """It leaves the working list, lands in Your Decision, and reaches L2."""
        self.client.force_login(self.l1_a)
        page = self.client.get("/approve/")
        self.assertEqual([f.id for f in page.context["files"]], [self.file.id])
        self.assertEqual(list(page.context["decided_files"]), [])

        self.release_to_l2()

        page = self.client.get("/approve/")
        self.assertEqual(list(page.context["files"]), [], "left the working list")
        self.assertEqual(
            [f.id for f in page.context["decided_files"]], [self.file.id],
            "arrived in Your Decision",
        )
        self.assertEqual(page.context["decided_files"][0].l1_status, "unlock")

        # And it is in front of L2, marked Unlock.
        self.client.force_login(self.l2)
        l2_page = self.client.get("/approve-l2/")
        self.assertEqual([f.id for f in l2_page.context["files"]], [self.file.id])
        self.assertEqual(l2_page.context["files"][0].l1_status, "unlock")

    @patch(SCP_TARGET)
    def test_your_decision_is_per_user(self, _mock_scp):
        """Another L1 user's decision is not listed as yours."""
        self.release_to_l2()  # l1_a unlocks

        self.client.force_login(self.l1_b)
        page = self.client.get("/approve/")

        self.assertEqual(list(page.context["decided_files"]), [])
        self.assertEqual(list(page.context["files"]), [], "and it is decided, so not to mark")

    @patch(SCP_TARGET)
    def test_your_decision_survives_the_l2_decision(self, _mock_scp):
        """The record stays after L2 acts; it is not a pending-work list."""
        self.release_to_l2()
        self.approve_as(self.l2, "/approve-l2/")

        self.client.force_login(self.l1_a)
        page = self.client.get("/approve/")
        self.assertEqual(
            [f.id for f in page.context["decided_files"]], [self.file.id]
        )

    @patch(SCP_TARGET)
    def test_unlocking_is_idempotent_and_records_the_last_l1_user(self, _mock_scp):
        self.release_to_l2()
        self.file.refresh_from_db()
        self.assertEqual(self.file.l1_status_by, self.l1_a)

        self.unlock_as(self.l1_b)

        self.file.refresh_from_db()
        self.assertEqual(self.file.l1_status, "unlock")
        self.assertEqual(self.file.l1_status_by, self.l1_b)

    @patch(SCP_TARGET)
    def test_status_is_frozen_once_l2_has_approved(self, _mock_scp):
        self.release_to_l2()                      # l1_a unlocks it
        self.approve_as(self.l2, "/approve-l2/")  # L2 approves it

        response = self.unlock_as(self.l1_b)      # a late press by another L1

        self.file.refresh_from_db()
        self.assertEqual(self.file.l1_status, "unlock")
        self.assertEqual(self.file.l1_status_by, self.l1_a, "stamp unchanged")
        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("already been actioned by L2", text)

    @patch(SCP_TARGET)
    def test_a_locked_file_cannot_be_approved(self, mock_scp):
        """No button for it, and the view refuses a posted approval too."""
        self.file.refresh_from_db()
        self.assertEqual(self.file.l1_status, "")  # never unlocked

        response = self.approve_as(self.l2, "/approve-l2/")

        self.file.refresh_from_db()
        self.assertFalse(self.file.approved_by_l2)
        mock_scp.assert_not_called()
        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("is locked", text)

    @patch(SCP_TARGET)
    def test_a_locked_file_cannot_be_rejected(self, mock_scp):
        self.client.force_login(self.l2)
        response = self.client.post("/approve-l2/", {
            "file_id": self.file.id, "reject": "1", "rejection_reason": "no",
        })

        self.file.refresh_from_db()
        self.assertFalse(self.file.rejected_by_l2)
        mock_scp.assert_not_called()
        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("is locked", text)

    @patch(SCP_TARGET)
    def test_the_lock_tab_offers_no_approve_or_reject(self, _mock_scp):
        self.client.force_login(self.l2)
        html = self.client.get("/approve-l2/").content.decode()

        tab = html[html.index('id="locked"'):html.index('id="history"')]
        self.assertNotIn('name="approve"', tab)
        self.assertNotIn('name="reject"', tab)
        self.assertIn(">View</a>", tab)
        self.assertIn(">Download</a>", tab)

    @patch(SCP_TARGET)
    def test_unlocking_then_approving_still_reaches_hrms(self, mock_scp):
        """The unchanged path: L1 unlocks, L2 approves, the file goes to HRMS."""
        self.release_to_l2()

        self.approve_as(self.l2, "/approve-l2/")

        self.file.refresh_from_db()
        self.assertTrue(self.file.approved_by_l2)
        mock_scp.assert_called_once()
        self.mock_upload_to_hrms.delay.assert_called_once()

    @patch(SCP_TARGET)
    def test_l1_can_no_longer_approve_or_reject(self, mock_scp):
        """The retired L1 actions are gone: posting them changes nothing."""
        self.client.force_login(self.l1_a)
        self.client.post("/approve/", {"file_id": self.file.id, "approve": "1"})
        self.client.post("/approve/", {
            "file_id": self.file.id, "reject": "1", "rejection_reason": "nope",
        })

        self.file.refresh_from_db()
        self.assertFalse(self.file.approved_by_l1)
        self.assertFalse(self.file.rejected_by_l1)
        self.assertEqual(self.file.l1_status, "")  # still Lock
        mock_scp.assert_not_called()


class HrmsOutputTests(DowntimeWorkflowTestCase):
    """The file pushed to HRMS is built in media/L2_approved/."""

    def setUp(self):
        super().setUp()
        self.upload_as(self.uploader)
        self.file = self.pending_file()

    @patch("users.views.SCPClient")
    @patch("users.views.paramiko")
    def test_approved_csv_is_written_to_l2_approved(self, _ssh, _scp):
        from users.views import approve_file_and_copy

        approve_file_and_copy(self.file)

        out_dir = os.path.join(TEMP_MEDIA_ROOT, "L2_approved")
        produced = os.listdir(out_dir)
        self.assertEqual(len(produced), 1)
        self.assertTrue(produced[0].endswith(".csv"))  # always CSV, even from xlsx

        df = pd.read_csv(os.path.join(out_dir, produced[0]))
        self.assertEqual(list(df.columns), ["EmpCode", "Date", "Minutes", "IsWH"])
        self.assertTrue((df["IsWH"] == "N").all())

    @patch("users.views.SCPClient")
    @patch("users.views.paramiko")
    def test_legacy_columns_are_stripped_before_hrms(self, _ssh, _scp):
        """Older files still carry Process/CH_ATSID and must be cleaned."""
        from users.views import approve_file_and_copy

        legacy_dir = os.path.join(TEMP_MEDIA_ROOT, "L1_approved")
        os.makedirs(legacy_dir, exist_ok=True)
        legacy_name = "L1_approved/legacy_ATS90664.csv"
        pd.DataFrame(
            {
                "EmpCode": ["ATS66261"],
                "Date": [RECENT_DATE],
                "Minutes": [30],
                "Process": ["Max life-SDPL"],
                "CH_ATSID": ["ATS90664"],
            }
        ).to_csv(os.path.join(TEMP_MEDIA_ROOT, legacy_name), index=False)

        legacy = UploadedFile.objects.create(
            file=legacy_name, uploaded_by=self.uploader,
            is_valid_format=True, is_split=True, legacy_ch_routed=True,
        )
        approve_file_and_copy(legacy)

        out = os.path.join(TEMP_MEDIA_ROOT, "L2_approved", "legacy_ATS90664.csv")
        df = pd.read_csv(out)
        self.assertEqual(list(df.columns), ["EmpCode", "Date", "Minutes", "IsWH"])


class ViewStoresFileInDatabaseTests(DowntimeWorkflowTestCase):
    """View copies the file into the database, then renders from that copy."""

    def setUp(self):
        super().setUp()
        self.upload_as(self.uploader)
        self.file = self.pending_file()
        self.path = os.path.join(TEMP_MEDIA_ROOT, self.file.file.name)

    def view(self, user=None):
        self.client.force_login(user or self.uploader)
        return self.client.get(f"/file/{self.file.id}/view/")

    def test_the_upload_is_stored_immediately(self):
        """Bytes are kept from the moment the file is created, not on first View.

        A file whose media was lost before anyone previewed it could not be
        delivered to L2 and could not stop being retried, so waiting for a View
        was too late.
        """
        stored = StoredFile.objects.get(uploaded_file=self.file)

        with open(self.path, "rb") as handle:
            self.assertEqual(bytes(stored.content), handle.read())

    def test_view_stores_the_file_and_renders_it(self):
        response = self.view()

        stored = StoredFile.objects.get(uploaded_file=self.file)
        with open(self.path, "rb") as handle:
            on_disk = handle.read()

        self.assertEqual(bytes(stored.content), on_disk, "byte-for-byte copy")
        self.assertEqual(stored.size, len(on_disk))
        self.assertEqual(stored.sha256, hashlib.sha256(on_disk).hexdigest())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "EmpCode")
        self.assertContains(response, "ATS66261")

    def test_a_second_view_reuses_the_stored_row(self):
        self.view()
        stored_at = StoredFile.objects.get(uploaded_file=self.file).stored_at

        self.view()

        self.assertEqual(StoredFile.objects.filter(uploaded_file=self.file).count(), 1)
        self.assertEqual(
            StoredFile.objects.get(uploaded_file=self.file).stored_at, stored_at,
            "not rewritten on every view",
        )

    def test_the_file_still_displays_once_the_disk_copy_is_gone(self):
        self.view()                      # stores it
        os.remove(self.path)             # the disk copy disappears
        self.assertFalse(os.path.exists(self.path))

        response = self.view()           # served from the database

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ATS66261")
        self.assertNotContains(response, "no longer available")

    def test_the_rendered_rows_come_from_the_stored_bytes(self):
        self.view()

        # Rewrite the stored copy; the page must follow the database, not disk.
        marker = b"EmpCode,Date,Minutes\nATS99999,01-01-2026,7\n"
        UploadedFile.objects.filter(id=self.file.id).update(file="uploads/x.csv")
        StoredFile.objects.filter(uploaded_file=self.file).update(
            content=marker, size=len(marker),
            sha256=hashlib.sha256(marker).hexdigest(),
        )

        response = self.view()

        self.assertContains(response, "ATS99999")
        self.assertNotContains(response, "ATS66261")

    def test_a_never_viewed_file_survives_losing_its_disk_copy(self):
        """The stored copy carries it, even though nobody ever pressed View."""
        os.remove(self.path)

        response = self.view()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ATS66261")
        self.assertNotContains(response, "no longer available")

    def test_a_record_with_no_bytes_anywhere_reports_unavailable(self):
        """The genuinely unrecoverable case: no disk copy and no stored copy."""
        StoredFile.objects.filter(uploaded_file=self.file).delete()
        os.remove(self.path)

        response = self.view()

        self.assertContains(response, "no longer available")

    def test_source_availability_drives_the_l2_guard(self):
        """L2 must not be offered an approval that could only fail."""
        from users.views import source_is_available

        self.assertTrue(source_is_available(self.file), "on disk and stored")

        os.remove(self.path)
        self.assertTrue(source_is_available(self.file), "stored copy still carries it")

        StoredFile.objects.filter(uploaded_file=self.file).delete()
        self.assertFalse(source_is_available(self.file), "nothing left anywhere")

    def test_download_still_points_at_the_file_on_disk(self):
        """Requirement 4: Download is untouched by any of this."""
        self.view()

        self.client.force_login(self.uploader)
        html = self.client.get("/home/").content.decode()

        self.assertIn(f'href="{self.file.file.url}" download', html)
        self.assertTrue(os.path.exists(self.path))

    def test_storing_does_not_disturb_the_workflow(self):
        """The approval fields are untouched by a preview."""
        before = UploadedFile.objects.filter(id=self.file.id).values().first()

        self.view(self.l1_a)
        self.view(self.l2)

        after = UploadedFile.objects.filter(id=self.file.id).values().first()
        self.assertEqual(before, after)


class EmpCodeNormalisationTests(DowntimeWorkflowTestCase):
    """An EmpCode is accepted around whitespace/case, and stored canonically."""

    ACCEPTED = ["ATS4575", "ATS4575 ", " ATS4575", "Ats4575", "ats4575 "]
    REJECTED = ["ATS", "ATSABC", "AT4575", "ABC4575", "ATS 4575"]

    def upload_code(self, code):
        buf = BytesIO()
        pd.DataFrame({"EmpCode": [code], "Date": [RECENT_DATE], "Minutes": [30]}).to_excel(
            buf, index=False, engine="openpyxl"
        )
        buf.seek(0)
        self.client.force_login(self.uploader)
        return self.client.post("/home/", {
            "process": UPLOAD_PROCESS, "location": UPLOAD_LOCATION,
            "file": SimpleUploadedFile("dt.xlsx", buf.read(), content_type=XLSX_CONTENT_TYPE),
        })

    def test_whitespace_and_case_variants_are_accepted(self):
        for code in self.ACCEPTED:
            with self.subTest(code=code):
                text = " ".join(b for _, b in self.messages_of(self.upload_code(code)))
                self.assertNotIn("Invalid EmpCode", text, f"{code!r} was rejected")

    def test_malformed_codes_are_rejected(self):
        for code in self.REJECTED:
            with self.subTest(code=code):
                text = " ".join(b for _, b in self.messages_of(self.upload_code(code)))
                self.assertIn("Invalid EmpCode", text, f"{code!r} was accepted")

    def test_the_saved_file_holds_the_canonical_code(self):
        """The point of the fix: no stray space reaches L1, L2 or HRMS."""
        for code in self.ACCEPTED:
            with self.subTest(code=code):
                UploadedFile.objects.all().delete()
                self.upload_code(code)
                record = UploadedFile.objects.filter(is_split=True).first()
                self.assertIsNotNone(record, f"{code!r} produced no upload")
                path = os.path.join(TEMP_MEDIA_ROOT, record.file.name)
                saved = (
                    pd.read_csv(path) if path.lower().endswith(".csv")
                    else pd.read_excel(path, engine="openpyxl")
                )
                self.assertEqual(list(saved["EmpCode"]), ["ATS4575"])


class UploadRedirectsAfterPostTests(DowntimeWorkflowTestCase):
    """Post/Redirect/Get: a refresh must not upload the same file again."""

    def test_successful_upload_redirects(self):
        response = self.upload_as(self.uploader)

        self.assertEqual(response.status_code, 302, "POST should redirect, not render")
        self.assertEqual(response["Location"], "/home/")

    def test_the_success_message_survives_the_redirect(self):
        response = self.client.get(self.upload_as(self.uploader)["Location"])

        text = " ".join(b for _, b in self.messages_of(response))
        self.assertIn("sent for approval", text)

    def refresh(self, response):
        """Do what a browser reload does, given what the upload returned.

        After a redirect the address bar holds the GET target, so a reload is a
        GET. After a rendered 200 the last request was the POST itself, and the
        browser re-sends it - which is the duplicate this fix exists to stop.
        Deriving it from the response is what makes this test fail without the
        fix instead of passing either way.
        """
        if response.status_code == 302:
            return self.client.get(response["Location"])
        return self.upload_as(self.uploader)

    def test_refreshing_after_an_upload_does_not_upload_again(self):
        response = self.upload_as(self.uploader)
        self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 1)

        self.refresh(response)
        self.refresh(response)

        self.assertEqual(
            UploadedFile.objects.filter(is_split=True).count(), 1,
            "a browser refresh uploaded the same file again",
        )

    def test_a_rejected_upload_still_redirects(self):
        """Unchanged behaviour: failures already redirected and still do."""
        response = self.post_df(self.rows(2, days_old=400))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 0)


class InactiveAtsIdTests(DowntimeWorkflowTestCase):
    """An inactive ATS ID is named, and the error outlives a refresh."""

    INACTIVE = "ATS4575"

    def setUp(self):
        super().setUp()
        # These tests are about the real mapping check, so the shared stub is
        # replaced with the genuine function for the duration.
        self.real_check = self.use_real_ats_check()

    def use_real_ats_check(self):
        patcher = patch(
            "users.views.mapping.validate_ats_ids", new=REAL_VALIDATE_ATS_IDS
        )
        patcher.start()
        self.addCleanup(self._safe_stop, patcher)
        return patcher

    @staticmethod
    def _safe_stop(patcher):
        try:
            patcher.stop()
        except RuntimeError:
            pass  # already stopped by a test

    def upload(self, codes):
        buf = BytesIO()
        pd.DataFrame({
            "EmpCode": list(codes),
            "Date": [RECENT_DATE] * len(codes),
            "Minutes": [30] * len(codes),
        }).to_excel(buf, index=False, engine="openpyxl")
        buf.seek(0)
        self.client.force_login(self.uploader)
        return self.client.post("/home/", {
            "process": UPLOAD_PROCESS, "location": UPLOAD_LOCATION,
            "file": SimpleUploadedFile("dt.xlsx", buf.read(), content_type=XLSX_CONTENT_TYPE),
        })

    def page(self):
        self.client.force_login(self.uploader)
        return self.client.get("/home/")

    def test_the_inactive_id_is_named(self):
        self.upload([self.INACTIVE])

        self.assertContains(self.page(), "Inactive ATSid: %s" % self.INACTIVE)

    def test_every_inactive_id_is_listed(self):
        self.upload([self.INACTIVE, "ATS9999999"])

        body = self.page().content.decode()
        self.assertIn(self.INACTIVE, body)
        self.assertIn("ATS9999999", body)

    def test_no_file_is_created_for_an_inactive_id(self):
        self.upload([self.INACTIVE])

        self.assertEqual(UploadedFile.objects.count(), 0)

    def test_the_error_survives_repeated_refreshes(self):
        """The point: the messages framework would drop it after one render."""
        self.upload([self.INACTIVE])

        for attempt in range(3):
            self.assertContains(
                self.page(), "Inactive ATSid",
                msg_prefix="lost after %d refresh(es)" % attempt,
            )

    def test_a_successful_upload_clears_the_error(self):
        self.upload([self.INACTIVE])
        self.assertContains(self.page(), "Inactive ATSid")

        # Back to the permissive stub, so the next file uploads cleanly.
        self.real_check.stop()
        self.upload(["ATS66261"])

        self.assertNotContains(self.page(), "Inactive ATSid")
        self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 1)


class PendingReminderEmailTests(DowntimeWorkflowTestCase):
    """The 10:00 digests: one mail per stage, only what is still outstanding."""

    def setUp(self):
        super().setUp()
        self.upload_as(self.uploader)
        self.file = self.pending_file()
        mail.outbox = []

    def run_both(self):
        from users.tasks import notify_l1_pending_files, notify_l2_pending_files
        return notify_l1_pending_files(), notify_l2_pending_files()

    # The workflow helpers live on ApprovalChainTests; repeated here rather than
    # moved, so that class keeps working exactly as it does.
    def release_to_l2(self):
        self.client.force_login(self.l1_a)
        return self.client.post("/approve/", {
            "file_id": self.file.id, "unlock_and_save": "1",
        })

    def approve_as(self, user, url):
        self.client.force_login(user)
        return self.client.post(url, {"file_id": self.file.id, "approve": "1"})

    def test_a_new_upload_is_reported_to_l1_only(self):
        """Not yet unlocked, so it is L1's to action, not L2's."""
        l1_result, l2_result = self.run_both()

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, [settings.L1_PENDING_NOTIFY_EMAIL])
        self.assertEqual(sent.subject, "Pending Files - Lock/Unlock Required")
        self.assertIn("pending for Lock/Unlock action", sent.body)
        self.assertIn(os.path.basename(self.file.file.name), sent.body)
        self.assertIn("Nothing pending for L2", l2_result)

    def test_once_unlocked_it_moves_to_the_l2_digest(self):
        self.release_to_l2()
        mail.outbox = []

        self.run_both()

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, [settings.L2_PENDING_NOTIFY_EMAIL])
        self.assertEqual(sent.subject, "Pending Files - Approval Required")
        self.assertIn("pending for approval", sent.body)
        self.assertIn(os.path.basename(self.file.file.name), sent.body)

    @patch(SCP_TARGET)
    def test_an_approved_file_is_reported_to_nobody(self, _mock_scp):
        """The core rule: processed files must never appear in a reminder."""
        self.release_to_l2()
        self.approve_as(self.l2, "/approve-l2/")
        mail.outbox = []

        l1_result, l2_result = self.run_both()

        self.assertEqual(len(mail.outbox), 0, "an approved file was mailed about")
        self.assertIn("Nothing pending", l1_result)
        self.assertIn("Nothing pending", l2_result)

    def test_nothing_pending_sends_no_mail(self):
        UploadedFile.objects.all().delete()

        l1_result, l2_result = self.run_both()

        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("Nothing pending", l1_result)
        self.assertIn("Nothing pending", l2_result)

    def test_several_pending_files_go_in_one_email(self):
        for _ in range(2):
            self.upload_as(self.uploader)
        pending = UploadedFile.objects.filter(is_split=True, l1_status="")
        self.assertGreater(pending.count(), 1)
        mail.outbox = []

        self.run_both()

        self.assertEqual(len(mail.outbox), 1, "one digest per recipient, not one per file")
        body = mail.outbox[0].body
        for record in pending:
            self.assertIn(os.path.basename(record.file.name), body)

    def test_the_schedule_is_ten_am_local(self):
        from django.conf import settings as dj
        schedule = dj.CELERY_BEAT_SCHEDULE
        self.assertEqual(dj.CELERY_TIMEZONE, dj.TIME_ZONE)
        for entry in schedule.values():
            self.assertEqual(entry["schedule"].hour, {10})
            self.assertEqual(entry["schedule"].minute, {0})


class LockedAttendanceMessageTests(DowntimeWorkflowTestCase):
    """Delivery errors show on File Status, never in the pending queue."""

    LOCKED = ("HRMS rejected all 23 record(s). "
              "Reason: Attendence is already processed")
    INVALID = "HRMS rejected all 23 record(s). Reason: Invalid EmpCode"

    def setUp(self):
        super().setUp()
        self.upload_as(self.uploader)
        self.file = self.pending_file()
        self.client.force_login(self.l1_a)
        self.client.post("/approve/", {"file_id": self.file.id, "unlock_and_save": "1"})

    def fail_in_task(self, reason):
        """Approve as L2, then fail the delivery the way upload_to_hrms does."""
        from users.tasks import _record_delivery_failure
        UploadedFile.objects.filter(pk=self.file.pk).update(
            approved_by_l2=True, approved_by_l2_user=self.l2, approved_at_l2=now(),
        )
        _record_delivery_failure(self.file, reason)
        self.file.refresh_from_db()

    def test_a_locked_period_goes_back_to_l1_files_to_mark(self):
        self.fail_in_task(self.LOCKED)

        self.assertFalse(self.file.approved_by_l2)
        self.assertIsNone(self.file.approved_by_l2_user)
        self.assertEqual(self.file.l1_status, "")

        self.client.force_login(self.l1_a)
        l1 = self.client.get("/approve/")
        self.assertIn(self.file, l1.context["files"])
        self.assertContains(l1, "Returned: Attendance already processed")

        l2 = self.l2_page()
        self.assertEqual(list(l2.context["files"]), [])
        self.assertEqual(list(l2.context["locked_files"]), [])
        self.assertEqual(list(l2.context["approved_files"]), [])

    def test_a_returned_file_can_go_round_again(self):
        self.fail_in_task(self.LOCKED)

        self.client.force_login(self.l1_a)
        self.client.post("/approve/", {"file_id": self.file.id, "unlock_and_save": "1"})

        self.assertEqual([f.id for f in self.l2_page().context["files"]],
                         [self.file.id])

    def test_other_failures_stay_with_l2(self):
        self.fail_in_task(self.INVALID)

        self.assertTrue(self.file.approved_by_l2)
        self.assertEqual(self.file.l1_status, "unlock")
        self.assertIn(self.file, self.l2_page().context["approved_files"])

    def l2_page(self):
        self.client.force_login(self.l2)
        return self.client.get("/approve-l2/")

    def mark_approved_and_failed(self, reason):
        UploadedFile.objects.filter(pk=self.file.pk).update(
            approved_by_l2=True, approved_by_l2_user=self.l2,
            approved_at_l2=now(), last_delivery_error=reason,
            delivery_attempts=1, last_delivery_attempt_at=now(),
        )

    def test_a_pending_file_shows_only_pending(self):
        response = self.l2_page()

        self.assertEqual([f.id for f in response.context["files"]], [self.file.id])
        html = response.content.decode()
        self.assertIn('<span class="pill pill-pending">Pending</span>', html)
        self.assertNotIn("HRMS failed", html)

    def test_a_failed_file_leaves_pending_and_shows_its_error(self):
        self.mark_approved_and_failed(self.INVALID)

        response = self.l2_page()

        self.assertEqual(list(response.context["files"]), [])
        self.assertIn(self.file, response.context["approved_files"])
        html = response.content.decode()
        self.assertIn("HRMS failed", html)
        self.assertIn("All records rejected", html)

    def test_the_record_count_is_not_shown(self):
        """The cell shows the short label; the full text is only the tooltip."""
        self.mark_approved_and_failed(self.INVALID)

        html = self.l2_page().content.decode()

        self.assertIn('title="%s"' % self.INVALID, html)
        self.assertNotIn(">%s<" % self.INVALID, html)

    def test_other_failures_get_their_own_short_label(self):
        for reason, label in (
            ("HRMS rejected all 5 record(s). Reason: Invalid EmpCode",
             "All records rejected"),
            ("HRMS rejected the login (portal said: 'valid password').",
             "HRMS login failed"),
            ("The file could not be copied to the HRMS host.",
             "Transfer to HRMS host failed"),
        ):
            with self.subTest(reason=reason):
                self.mark_approved_and_failed(reason)
                html = self.l2_page().content.decode()
                self.assertIn(label, html)
                self.assertNotIn("Attendance already processed", html)


class AllowedProcessTests(TestCase):
    """Only the agreed processes are offered for upload."""

    def test_the_dropdown_offers_only_the_allowed_processes(self):
        from users import mapping
        from users.forms import CSVUploadForm

        offered = mapping.get_processes()
        self.assertTrue(offered, "the mapping file produced no processes")
        for name in offered:
            self.assertTrue(
                mapping.is_allowed_process(name), f"{name!r} is not on the list"
            )

        choices = [c[0] for c in CSVUploadForm(user=None).fields["process"].choices if c[0]]
        self.assertEqual(sorted(choices), sorted(offered))

    def test_matching_ignores_case_and_spacing(self):
        """The file spells Emaar as EMAAR; it must still be offered."""
        from users import mapping

        self.assertTrue(mapping.is_allowed_process("emaar"))
        self.assertTrue(mapping.is_allowed_process("  E-Saras  "))
        self.assertFalse(mapping.is_allowed_process("Adani"))

    def test_the_location_map_matches_the_dropdown(self):
        """A process the user cannot pick must not appear in the cascade."""
        from users import mapping

        self.assertEqual(
            set(mapping.get_process_location_map()), set(mapping.get_processes())
        )

    def test_ats_lookup_still_covers_every_process(self):
        """Restricting the dropdown must not narrow ATS ID validation."""
        from users import mapping

        index = mapping.get_mapping()["index"]
        mapped = {p for p, _ in index.values()}
        self.assertGreater(
            len(mapped), len(mapping.get_processes()),
            "the index should still hold processes that are not offered",
        )

    def test_locations_are_not_restricted(self):
        from users import mapping

        self.assertGreater(len(mapping.get_locations()), 2)


class AccessControlTests(DowntimeWorkflowTestCase):
    def test_l2_user_lands_on_l2_page_after_login(self):
        response = self.client.post(
            "/login/", {"username": "l2user", "password": "pw"}
        )
        self.assertRedirects(response, "/approve-l2/", fetch_redirect_response=False)

    def test_l1_user_cannot_use_l2_page(self):
        self.client.force_login(self.l1_a)
        response = self.client.get("/approve-l2/")
        self.assertContains(response, "do not have permission")


class UploadNotificationEmailTests(DowntimeWorkflowTestCase):
    """A notification-email failure must not be reported as a processing failure."""

    @override_settings(EMAIL_BACKEND="users.tests.ExplodingEmailBackend")
    def test_email_failure_is_not_reported_as_processing_failure(self):
        response = self.upload_as(self.uploader)
        text = " ".join(body for _, body in self.messages_of(response))

        self.assertNotIn("processing failed", text)
        self.assertIn("notification email could not be sent", text)

    @override_settings(EMAIL_BACKEND="users.tests.ExplodingEmailBackend")
    def test_file_still_created_when_email_fails(self):
        self.upload_as(self.uploader)
        self.assertEqual(UploadedFile.objects.filter(is_split=True).count(), 1)

    def test_successful_upload_sends_notification(self):
        from django.core import mail

        response = self.upload_as(self.uploader)
        text = " ".join(body for _, body in self.messages_of(response))

        self.assertIn("sent for approval", text)
        self.assertNotIn("could not be sent", text)
        self.assertEqual(len(mail.outbox), 1)

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AdminCreatedUserPasswordTests(TestCase):
    """A user an admin creates must be able to log in with the password given.

    CustomUserAdmin used to call set_password() on a password the add form had
    already hashed, storing a hash of the hash. The user could only get in after
    a password reset. These go through the real admin, login and reset views.
    """

    ADMIN_PW = "Boss@pw-12345"
    NEW_PW = "Fresh!Pass-2026"

    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="boss", password=self.ADMIN_PW, role="admin",
        )
        self.client.force_login(self.admin_user)

    def admin_add_user(self, username="newl1", password=NEW_PW, role="l1",
                       usable_password="true"):
        response = self.client.post("/admin/users/user/add/", {
            "username": username,
            "usable_password": usable_password,
            "password1": password,
            "password2": password,
            "role": role,
        })
        self.assertEqual(response.status_code, 302, "the admin add form was rejected")
        self.client.logout()
        return User.objects.get(username=username)

    def site_login(self, username, password):
        return self.client.post("/login/", {"username": username, "password": password})

    def test_password_is_stored_hashed_once(self):
        from django.contrib.auth.hashers import identify_hasher

        user = self.admin_add_user()

        self.assertNotEqual(user.password, self.NEW_PW)
        self.assertEqual(identify_hasher(user.password).algorithm, "pbkdf2_sha256")
        self.assertTrue(user.check_password(self.NEW_PW))
        self.assertEqual(user.role, "l1")

    def test_new_user_can_log_in_immediately(self):
        self.admin_add_user()

        response = self.site_login("newl1", self.NEW_PW)

        self.assertRedirects(response, "/approve/", fetch_redirect_response=False)

    def test_wrong_password_still_fails(self):
        self.admin_add_user()

        response = self.site_login("newl1", "Not-The-Password-1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid username or password.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_existing_user_still_logs_in(self):
        User.objects.create_user(username="olduser", password="Old!Pass-2025", role="regular")
        self.client.logout()

        response = self.site_login("olduser", "Old!Pass-2025")

        self.assertRedirects(response, "/home/", fetch_redirect_response=False)

    def test_admin_can_create_user_with_password_login_disabled(self):
        # Django's own "Password-based authentication: Disabled" option. The
        # old override turned the unusable marker into a usable password.
        user = self.admin_add_user(username="nopw", password="", usable_password="false")

        self.assertFalse(user.has_usable_password())
        self.assertEqual(self.site_login("nopw", user.password).status_code, 200)

    def test_editing_a_user_leaves_the_password_alone(self):
        user = self.admin_add_user()
        stored = user.password
        self.client.force_login(self.admin_user)

        response = self.client.post(f"/admin/users/user/{user.pk}/change/", {
            "username": "newl1",
            "first_name": "Renamed",
            "last_name": "",
            "email": "newl1@iccs.in",
            "is_active": "on",
            "date_joined_0": "2026-01-01",
            "date_joined_1": "10:00:00",
            "role": "l1",
        })

        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Renamed")
        self.assertEqual(user.password, stored)
        self.assertTrue(user.check_password(self.NEW_PW))

    def test_admin_change_password_view_still_works(self):
        user = self.admin_add_user()
        self.client.force_login(self.admin_user)

        response = self.client.post(f"/admin/users/user/{user.pk}/password/", {
            "usable_password": "true",
            "password1": "Second!Pass-2026",
            "password2": "Second!Pass-2026",
        })

        self.assertEqual(response.status_code, 302)
        self.client.logout()
        self.assertRedirects(
            self.site_login("newl1", "Second!Pass-2026"), "/approve/",
            fetch_redirect_response=False,
        )
        self.assertEqual(self.site_login("newl1", self.NEW_PW).status_code, 200)

    def test_password_reset_still_works(self):
        import re
        from django.core import mail

        user = self.admin_add_user()
        User.objects.filter(pk=user.pk).update(email="newl1@iccs.in")

        response = self.client.post("/password_reset/", {"email": "newl1@iccs.in"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

        link = re.search(r"/reset/[^/\s]+/[^/\s]+/", mail.outbox[0].body).group(0)
        set_password_url = self.client.get(link)["Location"]
        response = self.client.post(set_password_url, {
            "new_password1": "Reset!Pass-2026",
            "new_password2": "Reset!Pass-2026",
        })
        self.assertRedirects(response, "/reset/done/", fetch_redirect_response=False)

        self.assertRedirects(
            self.site_login("newl1", "Reset!Pass-2026"), "/approve/",
            fetch_redirect_response=False,
        )
        self.assertEqual(self.site_login("newl1", self.NEW_PW).status_code, 200)
