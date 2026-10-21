from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.contrib.auth.models import AbstractUser
from django.conf import settings

class User(AbstractUser):
    ROLE_CHOICES = [
        ('regular', 'Regular User'),
        ('l1', 'L1 User'),
        ('l2', 'L2 User'),
        ('admin', 'Admin'),
    ]
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='regular')

    def __str__(self):
        return self.username

class UploadedFile(models.Model):
    ROLE_CHOICES = [
        ('regular', 'Regular User'),
        ('l1', 'L1 User'),
        ('admin', 'Admin'),  # Changed "Admin" to lowercase "admin" for consistency
    ]

    file = models.FileField(upload_to='uploads/')
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='uploaded_files')

    # Process and Location the uploader selected on the upload page. Both are
    # taken from media/mapping/mapping.csv and every ATS ID was verified
    # against them before the row was created, so they describe the whole file.
    # Blank on files uploaded before this was introduced.
    process = models.CharField(max_length=150, blank=True, default='')
    location = models.CharField(max_length=100, blank=True, default='')

    user_role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='regular')

    # ----- L1 Lock/Unlock status -----
    # L1 no longer approves or rejects. It marks each file Lock or Unlock, and
    # that mark is what releases the file to L2 and travels with it. Empty means
    # L1 has not marked the file yet, so the file is not on L2's queue. The mark
    # stays changeable until L2 acts, after which the row is frozen.
    L1_STATUS_CHOICES = [
        ('lock', 'Lock'),
        ('unlock', 'Unlock'),
    ]
    l1_status = models.CharField(
        max_length=6, choices=L1_STATUS_CHOICES, blank=True, default=''
    )
    l1_status_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='l1_status_files'
    )
    l1_status_at = models.DateTimeField(null=True, blank=True)

    # ----- L1 approval (retired) -----
    # Kept so files decided under the old two-approval workflow still explain
    # themselves in reports and in the uploader's history. Nothing writes these
    # any more; l1_status above is what L1 sets now.
    approved_by_l1 = models.BooleanField(default=False)
    rejected_by_l1 = models.BooleanField(default=False)  # New field
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='rejected_files'
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, null=True)  # New field
    timestamp = models.DateTimeField(auto_now_add=True)
    is_valid_format = models.BooleanField(default=False)  # Flag for format validation

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='approved_files'
    )  # The L1 user who approved the file, under the retired L1 approval step

    approved_at = models.DateTimeField(null=True, blank=True)  # Timestamp of approval
    # models.py
    is_split = models.BooleanField(default=False)

    # ----- L2 approval (second stage, mirrors the L1 fields above) -----
    # A file goes to HRMS only once L2 has approved it.
    approved_by_l2 = models.BooleanField(default=False)
    approved_by_l2_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='l2_approved_files'
    )
    approved_at_l2 = models.DateTimeField(null=True, blank=True)

    # ----- Why a delivery to HRMS did not stand -----
    # An L2 approval is rolled back when the file does not reach HRMS, which
    # returns the file to the pending list. Without recording why, it simply
    # reappears and the approver has no way to tell a failed delivery from a
    # file nobody has looked at yet.
    last_delivery_error = models.TextField(blank=True, default='')
    last_delivery_attempt_at = models.DateTimeField(null=True, blank=True)
    delivery_attempts = models.PositiveIntegerField(default=0)

    # The last HRMS outcome an email was sent for, as "<outcome>:<task id>".
    # A Celery retry reuses its task id, so retrying the same run does not mail
    # again; a fresh L2 approval is a new run and does.
    hrms_notified_key = models.CharField(max_length=120, blank=True, default='')

    rejected_by_l2 = models.BooleanField(default=False)
    rejected_by_l2_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='l2_rejected_files'
    )
    rejected_at_l2 = models.DateTimeField(null=True, blank=True)
    rejection_reason_l2 = models.TextField(blank=True, null=True)

    # True for files created before Cluster Head routing was removed. Those files
    # still reach their L1 approver via the CH_ATSID embedded in the filename;
    # everything created afterwards goes to a queue shared by all L1 users.
    legacy_ch_routed = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.file.name} - Uploaded by {self.uploaded_by.username} ({self.user_role})"

class StoredFile(models.Model):
    """An uploaded file's bytes, held in the database.

    Written the first time someone previews a file and read by every preview
    after that, so a file stays viewable once it has been seen even if the copy
    under MEDIA_ROOT is later removed. This is storage for the preview only:
    the FileField on UploadedFile remains what Download serves, and nothing
    about the upload, approval or HRMS paths reads this table.
    """
    uploaded_file = models.OneToOneField(
        UploadedFile, on_delete=models.CASCADE, related_name='stored'
    )
    content = models.BinaryField()
    size = models.PositiveIntegerField()
    # Identifies the exact bytes held, so a stored copy can be compared with a
    # file on disk without reading both into memory twice.
    sha256 = models.CharField(max_length=64)
    content_type = models.CharField(max_length=100, blank=True, default='')
    stored_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.uploaded_file.file.name} ({self.size} bytes)"


class DailySummary(models.Model):
    date = models.CharField(max_length=10)  # dd-mm-yyyy format
    process = models.CharField(max_length=100)
    pending = models.IntegerField(default=0)
    approved = models.IntegerField(default=0)

    class Meta:
        unique_together = ('date', 'process')

    def __str__(self):
        return f"{self.date} - {self.process}"



class Process(models.Model):
    """A process offered on the upload page, and who approves it at L2.

    Managed from the admin site. The name and location are picked from the HRMS
    mapping file, which is still what ties each ATS ID to a process; a row here
    only decides whether that process/location may be uploaded against and which
    Cluster Head receives its files after L1.

    Deactivating a row only takes it off the upload dropdown. Files keep the
    process name as text, so nothing already uploaded is touched, and files
    still pending keep routing to the Cluster Head named here.
    """
    name = models.CharField('Process Name', max_length=150)
    location = models.CharField(max_length=100)
    cluster_head_name = models.CharField('Cluster Head Name', max_length=150)
    cluster_head_ats_id = models.CharField('Cluster Head ATS ID', max_length=20)
    is_active = models.BooleanField(
        'Active', default=True,
        help_text='Untick to remove it from the upload Process dropdown. '
                  'Uploaded files and their history are not affected.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Process'
        verbose_name_plural = 'Processes'
        ordering = ['name', 'location']
        constraints = [
            models.UniqueConstraint(
                Lower('name'), Lower('location'),
                name='unique_process_location_ci',
            ),
        ]

    def save(self, *args, **kwargs):
        self.name = ' '.join(self.name.split())
        self.location = ' '.join(self.location.split())
        self.cluster_head_name = ' '.join(self.cluster_head_name.split())
        self.cluster_head_ats_id = self.cluster_head_ats_id.strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.location})"


class UploadSettings(models.Model):
    """Upload rules an admin can change without a code change or restart.

    A single row (pk=1), edited from the admin site. It is read on every upload
    request, so a saved change applies to the very next upload.
    """
    DEFAULT_ALLOWED_UPLOAD_DAYS = 45
    MIN_ALLOWED_UPLOAD_DAYS = 1
    MAX_ALLOWED_UPLOAD_DAYS = 365

    allowed_upload_days = models.PositiveSmallIntegerField(
        'Allowed File Upload Days',
        default=DEFAULT_ALLOWED_UPLOAD_DAYS,
        validators=[
            MinValueValidator(MIN_ALLOWED_UPLOAD_DAYS),
            MaxValueValidator(MAX_ALLOWED_UPLOAD_DAYS),
        ],
        help_text=(
            'How many days a file may cover, counting today (India time). '
            'For example 3 accepts today and the 2 days before; a date that '
            'many days old or older, or any future date, is rejected. '
            'Whole number from 1 to 365.'
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )

    class Meta:
        verbose_name = 'Upload Settings'
        verbose_name_plural = 'Upload Settings'

    def save(self, *args, **kwargs):
        self.pk = 1  # only ever one row
        super().save(*args, **kwargs)

    @classmethod
    def allowed_days(cls):
        """The configured window, or the default when the row is missing."""
        value = cls.objects.filter(pk=1).values_list(
            'allowed_upload_days', flat=True
        ).first()
        return value or cls.DEFAULT_ALLOWED_UPLOAD_DAYS

    def __str__(self):
        return 'Upload Settings'
