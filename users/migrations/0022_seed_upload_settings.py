"""Create the Upload Settings row with the 45-day window that was hard-coded.

MAX_RECORD_AGE_DAYS in users/views.py was 45 until this setting replaced it,
so uploads behave exactly as before on the day this is deployed.
"""

from django.db import migrations


def seed(apps, schema_editor):
    UploadSettings = apps.get_model('users', 'UploadSettings')
    UploadSettings.objects.get_or_create(pk=1, defaults={'allowed_upload_days': 45})


def unseed(apps, schema_editor):
    apps.get_model('users', 'UploadSettings').objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0021_uploadsettings'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
