"""Sends whatever monthly email is due today. Meant to run every hour (the `scheduler` service in
docker-compose.yml); every step is idempotent, so running it often is safe and a missed run catches up:

- from ProgrammeSettings.reminder_day (default 15th, the 3rd week): this month's reminder to the
  current GPGDs, asking them to report their activities
- from ProgrammeSettings.report_day (default 7th, end of the 1st week): last month's training
  statistics to the BSTP Director, State Directors, District Education Leads and school leaders
- two days before that (default 5th): the Reporting Agent drafts last month's briefings, so the Admin
  can review and approve them on the Monthly Reports page before the reports go out
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from datetime import timedelta

from agents import monthly_reports, pipeline_ops, reporting_agent
from agents.models import AgentActivityLog, MonthlyBriefing, ProgrammeSettings


class Command(BaseCommand):
    help = 'Sends the monthly reminder and monthly statistics emails when they are due (safe to run hourly).'

    def handle(self, *args, **options):
        today = timezone.localdate()
        programme = ProgrammeSettings.load()

        period = monthly_reports.previous_period(today)
        if today.day >= max(1, programme.report_day - 2) and self._briefings_needed(period):
            run = reporting_agent.prepare_briefings(period, trigger='scheduled')
            self.stdout.write(f"Reporting Agent: {run.status} ({run.summary or run.error_message}).")

        if today.day >= programme.report_day:
            result = monthly_reports.send_monthly_reports(monthly_reports.previous_period(today))
            self._log('Monthly statistics', result['sent'], len(result['failed']),
                      f"report for {pipeline_ops.month_label(result['period'])}", result)

        if today.day >= programme.reminder_day:
            result = pipeline_ops.send_monthly_report_reminders()
            self._log('Monthly reminder', result['sent'], len(result['failed']),
                      f"reminder for {pipeline_ops.month_label(result['period'])}", result)

    def _briefings_needed(self, period):
        """Drafts are wanted once per month: when there are current GPGDs, no briefing yet, and no agent
        run for that month in the last day (so a failing run isn't retried every hour)."""
        recent_run = AgentActivityLog.objects.filter(
            triggered_at__gte=timezone.now() - timedelta(days=1),
            trigger_reason__startswith=f'Reporting Agent: briefings for {pipeline_ops.month_label(period)}').exists()
        return (pipeline_ops.current_gpgds().exists() and not recent_run
                and not MonthlyBriefing.objects.filter(period=period).exists())

    def _log(self, what, sent, failed, detail, result):
        self.stdout.write(f"{what}: {sent} sent, {failed} failed ({detail}).")
        if sent or failed:  # only record runs that did something, not every quiet hourly check
            AgentActivityLog.objects.create(
                trigger_reason=f'{what} (scheduled)',
                summary=f'{what}: {sent} email(s) sent, {failed} failed — {detail}.',
                actions_taken=[{'tool': 'run_scheduled_jobs', 'input': {}, 'result': result}],
                status='error' if failed else 'success',
            )
