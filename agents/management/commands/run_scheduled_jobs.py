"""Sends whatever monthly email is due today. Meant to run every hour (the `scheduler` service in
docker-compose.yml); every step is idempotent, so running it often is safe and a missed run catches up:

- from ProgrammeSettings.reminder_day (default 15th, the 3rd week): this month's reminder to the
  current GPGDs, asking them to report their activities
- from ProgrammeSettings.report_day (default 7th, end of the 1st week): last month's training
  statistics to the BSTP Director, State Directors, District Education Leads and school leaders
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from agents import monthly_reports, pipeline_ops
from agents.models import AgentActivityLog, ProgrammeSettings


class Command(BaseCommand):
    help = 'Sends the monthly reminder and monthly statistics emails when they are due (safe to run hourly).'

    def handle(self, *args, **options):
        today = timezone.localdate()
        programme = ProgrammeSettings.load()

        if today.day >= programme.report_day:
            result = monthly_reports.send_monthly_reports(monthly_reports.previous_period(today))
            self._log('Monthly statistics', result['sent'], len(result['failed']),
                      f"report for {pipeline_ops.month_label(result['period'])}", result)

        if today.day >= programme.reminder_day:
            result = pipeline_ops.send_monthly_report_reminders()
            self._log('Monthly reminder', result['sent'], len(result['failed']),
                      f"reminder for {pipeline_ops.month_label(result['period'])}", result)

    def _log(self, what, sent, failed, detail, result):
        self.stdout.write(f"{what}: {sent} sent, {failed} failed ({detail}).")
        if sent or failed:  # only record runs that did something, not every quiet hourly check
            AgentActivityLog.objects.create(
                trigger_reason=f'{what} (scheduled)',
                summary=f'{what}: {sent} email(s) sent, {failed} failed — {detail}.',
                actions_taken=[{'tool': 'run_scheduled_jobs', 'input': {}, 'result': result}],
                status='error' if failed else 'success',
            )
