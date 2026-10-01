from django.db import migrations

# State Officer profiles must use the same spellings as the application form's State dropdown
# (agents.constants.MALAYSIA_STATES), otherwise their applications never reach them.
RENAMES = {
    'WP Kuala Lumpur': 'W.P. Kuala Lumpur',
    'WP Putrajaya': 'W.P. Putrajaya',
    'WP Labuan': 'W.P. Labuan',
}


def forwards(apps, schema_editor):
    OfficerProfile = apps.get_model('agents', 'OfficerProfile')
    for old, new in RENAMES.items():
        OfficerProfile.objects.filter(state=old).update(state=new)


def backwards(apps, schema_editor):
    OfficerProfile = apps.get_model('agents', 'OfficerProfile')
    for old, new in RENAMES.items():
        OfficerProfile.objects.filter(state=new).update(state=old)


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0011_programmesettings_apply_form_field_labels_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
