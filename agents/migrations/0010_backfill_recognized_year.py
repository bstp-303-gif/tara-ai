from django.db import migrations


def backfill_recognized_year(apps, schema_editor):
    Application = apps.get_model('agents', 'Application')
    for application in Application.objects.filter(status='Approved', recognized_year__isnull=True):
        reference_date = application.moe_decision_at or application.submitted_at
        if reference_date:
            application.recognized_year = reference_date.year
            application.save(update_fields=['recognized_year'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0009_application_award_category_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_recognized_year, noop_reverse),
    ]
