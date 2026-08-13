from django.core.management.base import BaseCommand

from agents import pipeline_ops


class Command(BaseCommand):
    help = (
        'Emails every recognized GPGD a secure link to submit their monthly activity report. '
        'Idempotent per (application, period) — safe to re-run within the same month. '
        'Intended to run on the 1st of every month via an OS-level scheduler (e.g. Windows '
        'Task Scheduler: schtasks /create /tn "GPGD Monthly Report Emails" /tr '
        '"python manage.py send_monthly_report_emails" /sc monthly /d 1 /st 08:00).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--period', default=None,
            help='Override the "YYYY-MM" period (defaults to the current month). Useful for testing/backfills.',
        )

    def handle(self, *args, **options):
        result = pipeline_ops.send_monthly_report_reminders(period=options.get('period'))

        self.stdout.write(
            f"Period {result['period']}: sent {result['sent']} of {result['recognized_total']} recognized GPGD(s); "
            f"{result['skipped_already_sent']} already reminded this period; {result['no_email']} skipped (no email on file)."
        )
        if result['failed']:
            self.stdout.write(self.style.ERROR(f"{len(result['failed'])} failed to send — see the Error Log dashboard tab."))
        self.stdout.write(self.style.SUCCESS('Done.'))
