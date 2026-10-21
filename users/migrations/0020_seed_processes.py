"""Seed the Process table with the processes and Cluster Heads in use today.

Until this migration the upload dropdown and L2 routing were driven by the
ALLOWED_PROCESSES and PROCESS_CLUSTER_HEADS constants in users/mapping.py. The
rows below are those lists, one per process/location the mapping file has for
them, so the portal behaves exactly as before on the day this is deployed.
From then on processes are managed from the admin site.
"""

from django.db import migrations

# (process, location, cluster head name, cluster head ATS ID), spelled as the
# mapping file spells the process and location.
SEED = [
    ('Bajaj Allianz-Pune', 'PUNE', 'Aftab Khan', 'ATS106490'),
    ('DIC', 'Delhi', 'Meghvijay', 'ATS80558'),
    ('DIC', 'Noida', 'Meghvijay', 'ATS80558'),
    ('E-Saras', 'Noida', 'Meghvijay', 'ATS80558'),
    ('EMAAR', 'GURUGRAM', 'Meghvijay', 'ATS80558'),
    ('Jindal Steel LTD', 'HISAR', 'Meghvijay', 'ATS80558'),
    ('JVVNL', 'JAIPUR', 'Hemant Kumar', 'ATS63774'),
    ('Naaptol', 'Hyderabad', 'Sheesha Rajesh', 'ATS108723'),
    ('Namma yatri-Bengaluru', 'Bengaluru', 'Harshavardhana P', 'ATS77782'),
    ('NetMeds', 'Chennai', 'Vijay B', 'ATS88557'),
    ('Tamil Nadu CM Helpline', 'Chennai', 'Vijay B', 'ATS88557'),
    ('Think Gas', 'Chennai', 'Meghvijay', 'ATS80558'),
    ('Think Gas', 'Noida', 'Meghvijay', 'ATS80558'),
    ('UGVCL', 'GUJARAT', 'Meghvijay', 'ATS80558'),
    ('Upbhokta', 'JAIPUR', 'Hemant Kumar', 'ATS63774'),
]


def seed(apps, schema_editor):
    Process = apps.get_model('users', 'Process')
    for name, location, head_name, head_ats in SEED:
        exists = Process.objects.filter(
            name__iexact=name, location__iexact=location
        ).exists()
        if not exists:
            Process.objects.create(
                name=name, location=location,
                cluster_head_name=head_name, cluster_head_ats_id=head_ats,
            )


def unseed(apps, schema_editor):
    Process = apps.get_model('users', 'Process')
    for name, location, _head_name, _head_ats in SEED:
        Process.objects.filter(name__iexact=name, location__iexact=location).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0019_process'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
