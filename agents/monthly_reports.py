"""Monthly training statistics emailed to programme leaders, for the activities the current group of
GPGDs reported in one month:

- BSTP Director: the whole country, broken down by state
- State Director: their state, broken down by district (PPD)
- District Education Lead: their district, broken down by school
- School leader: their own school's GPGD(s), activity by activity

Recipients are set on the Report Recipients page (ReportRecipient); school leaders come from each
GPGD's application. Sent at the end of the first week of the next month (ProgrammeSettings.report_day)
by the run_scheduled_jobs command, or from the Monthly Reports page. Each email is logged in
MonthlyReportLog, so nobody gets the same report twice.
"""
from collections import defaultdict
from datetime import date

from django.template.loader import render_to_string
from django.utils import timezone

from . import pipeline_ops, ranking
from .constants import MALAYSIA_STATES, PPD_BY_STATE
from .models import ActivityReport, ErrorLog, MonthlyReportLog, ReportRecipient

COUNT_FIELDS = ['num_teachers', 'num_students', 'num_school_leaders', 'num_others']


def previous_period(today=None):
    """The "YYYY-MM" before `today`'s month: the month a report sent in the first week covers."""
    today = today or timezone.localdate()
    first = today.replace(day=1)
    return (date(first.year - 1, 12, 1) if first.month == 1 else first.replace(month=first.month - 1)).strftime('%Y-%m')


def _totals(reports, gpgds):
    """Headline numbers for a set of activity reports and the GPGDs who could have reported them."""
    totals = {'activities': len(reports), 'hours': sum(r.hours for r in reports),
              'gpgds': len(gpgds), 'gpgds_reporting': len({r.gpgd_id for r in reports})}
    for field in COUNT_FIELDS:
        totals[field] = sum(getattr(r, field) for r in reports)
    totals['participants'] = sum(totals[f] for f in COUNT_FIELDS)
    return totals


def _breakdown(reports, gpgds, key, names=()):
    """Rows of _totals per key(application), e.g. per state. `names` are always listed, even with nothing."""
    reports_by, gpgds_by = defaultdict(list), defaultdict(list)
    for gpgd in gpgds:
        gpgds_by[key(gpgd)].append(gpgd)
    for report in reports:
        reports_by[key(report.gpgd)].append(report)
    ordered = list(names) + sorted(k for k in set(gpgds_by) | set(reports_by) if k not in names)
    return [{'name': name, **_totals(reports_by[name], gpgds_by[name])} for name in ordered]


def _district(gpgd):
    return ranking.canonical_district(gpgd.state, gpgd.district) or gpgd.state


def collect(period):
    """Everything the month's emails are built from: the current GPGDs and the activities they reported."""
    year, month = (int(p) for p in period.split('-'))
    gpgds = list(pipeline_ops.current_gpgds())
    reports = list(ActivityReport.objects.filter(
        gpgd__in=gpgds, training_date__year=year, training_date__month=month).select_related('gpgd'))
    return gpgds, reports


def build_reports(period):
    """One email per recipient: dicts with level, scope, email, name, subject, text and html."""
    gpgds, reports = collect(period)
    month = pipeline_ops.month_label(period)
    emails = []

    def add(level, scope, email, name, title, totals, rows=None, row_label=None, activities=None):
        context = {'title': title, 'month': month, 'name': name, 'totals': totals, 'rows': rows or [],
                   'row_label': row_label, 'activities': activities or [], 'level': level}
        emails.append({
            'level': level, 'scope': scope, 'email': email, 'name': name,
            'subject': f"GPGD Monthly Training Report — {month} — {title}",
            'text': render_to_string('emails/monthly_report.txt', context),
            'html': render_to_string('emails/monthly_report.html', context),
            'totals': totals,
        })

    bstp = ReportRecipient.objects.filter(level='bstp').first()
    if bstp:
        add('bstp', 'Malaysia', bstp.email, bstp.name, 'Malaysia', _totals(reports, gpgds),
            _breakdown(reports, gpgds, lambda g: g.state, MALAYSIA_STATES), 'State')

    for recipient in ReportRecipient.objects.filter(level='state'):
        state = recipient.state
        in_state = [g for g in gpgds if g.state == state]
        state_reports = [r for r in reports if r.gpgd.state == state]
        add('state', state, recipient.email, recipient.name, state, _totals(state_reports, in_state),
            _breakdown(state_reports, in_state, _district, PPD_BY_STATE.get(state, [])), 'District (PPD)')

    for recipient in ReportRecipient.objects.filter(level='district'):
        in_district = [g for g in gpgds if g.state == recipient.state and _district(g) == recipient.district]
        district_reports = [r for r in reports if r.gpgd in in_district]
        add('district', recipient.district, recipient.email, recipient.name, f"{recipient.district}, {recipient.state}",
            _totals(district_reports, in_district),
            _breakdown(district_reports, in_district, lambda g: g.school_name.strip()), 'School')

    leaders = defaultdict(list)
    for gpgd in gpgds:
        if gpgd.school_leader_email:
            leaders[gpgd.school_leader_email.strip().lower()].append(gpgd)
    for email, school_gpgds in sorted(leaders.items()):
        school_reports = sorted((r for r in reports if r.gpgd in school_gpgds), key=lambda r: (r.training_date, r.gpgd.full_name))
        schools = sorted({g.school_name.strip() for g in school_gpgds})
        add('school_leader', ', '.join(schools), email, school_gpgds[0].school_leader_name, ', '.join(schools),
            _totals(school_reports, school_gpgds),
            _breakdown(school_reports, school_gpgds, lambda g: g.full_name), 'GPGD', activities=school_reports)
    return emails


def missing_recipients():
    """Which leaders have no email set, so the Admin can see who won't get a report."""
    have = set(ReportRecipient.objects.values_list('level', 'state', 'district'))
    missing = {'bstp': not any(level == 'bstp' for level, _, _ in have)}
    missing['states'] = [s for s in MALAYSIA_STATES if ('state', s, '') not in have]
    missing['districts'] = sum(1 for s, ds in PPD_BY_STATE.items() for d in ds if ('district', s, d) not in have)
    missing['gpgds_without_school_leader'] = pipeline_ops.current_gpgds().filter(school_leader_email='').count()
    return missing


def send_monthly_reports(period=None):
    """Sends the month's reports to everyone who hasn't already had theirs. Safe to run repeatedly."""
    period = period or previous_period()
    result = {'period': period, 'sent': 0, 'already_sent': 0, 'failed': []}
    for report in build_reports(period):
        key = dict(period=period, level=report['level'], scope=report['scope'][:255], email=report['email'])
        if MonthlyReportLog.objects.filter(**key, success=True).exists():
            result['already_sent'] += 1
            continue
        try:
            pipeline_ops._send_mail_with_hard_timeout(report['subject'], report['text'], report['email'], html=report['html'])
            MonthlyReportLog.objects.update_or_create(**key, defaults={'success': True, 'error_message': ''})
            result['sent'] += 1
        except Exception as e:
            category, suggested_action = pipeline_ops._classify_email_error(e)
            ErrorLog.objects.create(category=category, context=f"Monthly report {period} — {report['email']}",
                                    technical_detail=str(e), suggested_action=suggested_action)
            MonthlyReportLog.objects.update_or_create(**key, defaults={'success': False, 'error_message': str(e)})
            result['failed'].append({'email': report['email'], 'error': str(e)})
    return result
