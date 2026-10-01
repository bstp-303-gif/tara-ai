"""
Reusable pipeline operations shared between the manual dashboard buttons
(agents/views.py) and the autonomous pipeline agent (agents/ai_agent.py).

Kept separate from both so neither module has to import the other.
"""
import os
import smtplib
import socket
import threading

import pandas as pd
from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.utils import timezone

from . import content_defaults

SEND_TIMEOUT_SECONDS = 25


def _classify_email_error(exc):
    """Turn a raw send_mail() exception into (category, suggested_action) a
    non-technical programme officer can act on, for the ErrorLog dashboard tab."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'email_auth', (
            "The mail server rejected the login credentials. Check GPGD_EMAIL_USER and "
            "GPGD_EMAIL_PASSWORD in .env — for Gmail, this must be a 16-character App "
            "Password (with 2-Step Verification turned on), not the account's normal "
            "login password, and it may have been revoked or regenerated since."
        )
    if isinstance(exc, TimeoutError) or 'timed out' in str(exc).lower():
        return 'email_timeout', (
            f"The mail server never responded (waited {SEND_TIMEOUT_SECONDS}s). This "
            "usually means outbound SMTP is blocked by a firewall or network policy on "
            "whichever machine is running this server — common on sandboxed/restricted "
            "networks. Try again from the real production server/network, and confirm "
            "GPGD_EMAIL_HOST/GPGD_EMAIL_PORT in .env are correct."
        )
    if isinstance(exc, (smtplib.SMTPConnectError, socket.gaierror, ConnectionRefusedError)):
        return 'email_connection', (
            "Could not connect to the mail server at all. Check GPGD_EMAIL_HOST and "
            "GPGD_EMAIL_PORT in .env for typos, and confirm this network allows outbound "
            "connections to that host/port."
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'email_invalid_recipient', (
            "The mail server refused this recipient's address as invalid or "
            "undeliverable — check the teacher's email address on file for typos."
        )
    return 'email_other', f"Unexpected error type ({type(exc).__name__}) — see technical detail below."


def _send_mail_with_hard_timeout(subject, body, to_email):
    """send_mail(), bounded by a hard wall-clock timeout.

    settings.EMAIL_TIMEOUT is meant to bound smtplib's own socket operations, but on
    some networks a blocked/filtered SMTP connection can still hang past it (observed:
    the connection succeeds and STARTTLS negotiates fine, then AUTH never gets a
    response and the call never returns). The send runs on a *daemon* thread so that,
    if it's still stuck after SEND_TIMEOUT_SECONDS, we give up and move on without
    waiting for it — a non-daemon thread (e.g. via ThreadPoolExecutor) would instead
    get joined by Python's atexit handling later and could hang the process on exit.
    """
    outcome = {}

    def _worker():
        try:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)
            outcome['ok'] = True
        except Exception as e:
            outcome['error'] = e

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=SEND_TIMEOUT_SECONDS)

    if thread.is_alive():
        raise TimeoutError(
            f'No response from the mail server after {SEND_TIMEOUT_SECONDS}s — check '
            'GPGD_EMAIL_HOST/PORT/credentials in .env, or whether outbound SMTP is blocked '
            'on this network.'
        )
    if 'error' in outcome:
        raise outcome['error']

from .collector import normalize_dataframe
from .eligibility import normalize_and_deduplicate, save_dataframe_to_excel, save_to_database
from .models import Application, ErrorLog, FileUpload, MonthlyReminderLog, ProgrammeSettings, Teacher

PROVIDERS = ['Google', 'Microsoft', 'Apple']


def send_single_email_async(subject, body, to_email, context_label):
    """Fire-and-forget a one-off email (acknowledgements, recognition letters, ...).

    Runs off-thread so the request handler that triggered it (e.g. a teacher
    submitting an application) doesn't wait on SMTP at all. On failure, classifies
    the error and logs it to ErrorLog so it shows up on the dashboard's Error Log
    tab — previously these sends used fail_silently=True, so a failure here left
    literally no trace anywhere.
    """
    def _worker():
        try:
            _send_mail_with_hard_timeout(subject, body, to_email)
        except Exception as e:
            category, suggested_action = _classify_email_error(e)
            ErrorLog.objects.create(
                category=category,
                context=context_label,
                technical_detail=str(e),
                suggested_action=suggested_action,
            )

    threading.Thread(target=_worker, daemon=True).start()

TEMP_DIR = os.path.join(settings.BASE_DIR, 'temp_uploads')
os.makedirs(TEMP_DIR, exist_ok=True)

COMBINED_PATH = os.path.join(TEMP_DIR, 'combined_normalized.xlsx')
DEDUP_PATH = os.path.join(TEMP_DIR, 'deduplicated_list.xlsx')
ELIGIBLE_PATH = os.path.join(TEMP_DIR, 'eligible_candidates.xlsx')


def run_certification_pipeline():
    """Normalize, deduplicate, and classify eligibility across every provider's latest valid upload.

    Persists the resulting teacher roster and writes the three export files at fixed paths.
    Safe to re-run — always recomputes from whichever valid files currently exist.
    """
    try:
        dataframes_dict = {}
        providers_list = []
        skipped_providers = []

        for provider in PROVIDERS:
            file_record = FileUpload.objects.filter(
                provider=provider, validation_status='valid'
            ).order_by('-uploaded_at').first()

            if not file_record:
                skipped_providers.append(provider)
                continue

            df = pd.read_excel(file_record.file_path)
            normalized_df = normalize_dataframe(df, provider)
            if normalized_df is not None:
                dataframes_dict[provider] = normalized_df
                providers_list.append(provider)

        if not dataframes_dict:
            cleared = Teacher.objects.count()
            Teacher.objects.all().delete()
            if cleared:
                reason = f'No valid files for any provider. Cleared the stale {cleared}-teacher roster since no valid file backs it anymore.'
            else:
                reason = 'No valid files for any provider. The teacher roster was already empty — nothing to clear.'
            return {'ran': False, 'reason': reason, 'skipped_providers': skipped_providers}

        combined_df, deduplicated_df, eligible_df, stats = normalize_and_deduplicate(dataframes_dict, providers_list)
        if deduplicated_df is None:
            return {'ran': False, 'reason': 'Deduplication failed.', 'skipped_providers': skipped_providers}

        save_to_database(deduplicated_df)

        save_dataframe_to_excel(combined_df, COMBINED_PATH, 'Combined')
        save_dataframe_to_excel(deduplicated_df, DEDUP_PATH, 'Deduplicated')
        save_dataframe_to_excel(
            eligible_df[['rank', 'ic', 'name', 'email', 'school', 'state', 'provider', 'certification', 'cert_level', 'cert_year', 'multi_certified']],
            ELIGIBLE_PATH, 'Eligible'
        )

        return {'ran': True, 'stats': stats, 'skipped_providers': skipped_providers}
    except Exception as e:
        return {'ran': False, 'reason': f'Error processing files: {e}'}


def _make_apply_token(ic_number):
    return signing.dumps(ic_number, salt='gpgd-apply')


def submissions_closed(deadline):
    """Applications are accepted up to and including the deadline day."""
    return bool(deadline) and timezone.localdate() > deadline


def invitation_block_reason(deadline):
    """Why invitations can't be sent right now, or '' if they can."""
    if not deadline:
        return 'No submission deadline is set. Set the deadline on the dashboard before sending invitations.'
    if submissions_closed(deadline):
        return (f"The submission deadline ({deadline.strftime('%d %B %Y')}) has already passed. "
                "Set a new deadline before sending invitations.")
    return ''


def send_invitation_emails():
    """Email every eligible teacher who hasn't applied yet a secure, personalised application link."""
    eligible = Teacher.objects.filter(eligibility_status='Eligible').exclude(application__isnull=False)
    site_url = getattr(settings, 'GPGD_SITE_URL', 'http://localhost:8000')
    programme_settings = ProgrammeSettings.load()
    deadline = programme_settings.submission_deadline

    block_reason = invitation_block_reason(deadline)
    if block_reason:
        return {'sent': 0, 'no_email': 0, 'failed': [], 'eligible_total': eligible.count(), 'skipped_reason': block_reason}

    deadline_text = deadline.strftime('%d %B %Y')

    subject = programme_settings.invitation_email_subject or content_defaults.DEFAULT_INVITATION_SUBJECT
    body_template = programme_settings.invitation_email_body or content_defaults.DEFAULT_INVITATION_BODY

    sent = 0
    no_email = 0
    failed = []
    for teacher in eligible:
        if not teacher.email or teacher.email.lower() == 'nan':
            no_email += 1
            continue

        token = _make_apply_token(teacher.ic_number)
        apply_url = f"{site_url}/agents/apply/{token}/"

        body = content_defaults.render_placeholders(
            body_template, full_name=teacher.full_name, apply_url=apply_url, deadline=deadline_text
        )
        try:
            _send_mail_with_hard_timeout(subject, body, teacher.email)
            sent += 1
        except Exception as e:
            failed.append({'teacher': teacher.full_name, 'email': teacher.email, 'error': str(e)})
            category, suggested_action = _classify_email_error(e)
            ErrorLog.objects.create(
                category=category,
                context=f"{teacher.full_name} <{teacher.email}>",
                technical_detail=str(e),
                suggested_action=suggested_action,
            )

    return {
        'sent': sent,
        'no_email': no_email,
        'failed': failed,
        'eligible_total': eligible.count(),
    }


def make_report_token(reference_number):
    return signing.dumps(reference_number, salt='gpgd-monthly-report')


def read_report_token(token, max_age_days=60):
    """60 days (not 30, like the apply token) so a link emailed on the 1st stays valid
    for late submissions covering that whole month plus some buffer."""
    try:
        return signing.loads(token, salt='gpgd-monthly-report', max_age=60 * 60 * 24 * max_age_days)
    except signing.BadSignature:
        return None


def send_monthly_report_reminders(period=None):
    """Email every recognized GPGD a secure link to submit their monthly activity report.

    Idempotent per (application, period) via MonthlyReminderLog — safe to re-run (e.g. if the
    scheduled task fires twice, or an officer also clicks the manual "Send Now" override).
    """
    period = period or timezone.now().strftime('%Y-%m')
    site_url = getattr(settings, 'GPGD_SITE_URL', 'http://localhost:8000')

    recognized = Application.objects.filter(status='Approved')
    sent = 0
    skipped_already_sent = 0
    no_email = 0
    failed = []

    for application in recognized:
        # Only a *successful* send blocks a resend — a failed attempt (e.g. a transient SMTP
        # timeout) must stay retryable on the next run, whether that's next month's scheduled
        # run or an officer clicking "Send Monthly Reminders Now" again the same day.
        if MonthlyReminderLog.objects.filter(application=application, period=period, success=True).exists():
            skipped_already_sent += 1
            continue
        if not application.email:
            no_email += 1
            continue

        token = make_report_token(application.reference_number)
        report_url = f"{site_url}/agents/report/{token}/"

        subject = f"Monthly GPGD Activity Report — {period}"
        body = f"""Dear {application.full_name},

This is your monthly reminder to report the professional development activities you've conducted as a Guru Peneraju Generasi Digital (GPGD) — training, mentoring, coaching, or knowledge-sharing sessions delivered this month.

Please submit one report for each activity using the secure link below:

Report here: {report_url}

You'll need: the training title, date, time, number of hours, target audience, number of participants, training mode, a brief description, and two evidence photos (supporting documents are optional).

Thank you for your continued contribution to digital education in Malaysia.

Yours sincerely,
Sektor Pengintegrasian Teknologi Pendidikan (SPTP)
Bahagian Sumber dan Teknologi Pendidikan (BSTP)
Kementerian Pendidikan Malaysia
"""
        try:
            _send_mail_with_hard_timeout(subject, body, application.email)
            MonthlyReminderLog.objects.update_or_create(
                application=application, period=period,
                defaults={'success': True, 'error_message': ''},
            )
            sent += 1
        except Exception as e:
            category, suggested_action = _classify_email_error(e)
            ErrorLog.objects.create(
                category=category,
                context=f"Monthly reminder — {application.full_name} <{application.email}>",
                technical_detail=str(e),
                suggested_action=suggested_action,
            )
            MonthlyReminderLog.objects.update_or_create(
                application=application, period=period,
                defaults={'success': False, 'error_message': str(e)},
            )
            failed.append({'application': application.full_name, 'email': application.email, 'error': str(e)})

    return {
        'period': period,
        'sent': sent,
        'skipped_already_sent': skipped_already_sent,
        'no_email': no_email,
        'failed': failed,
        'recognized_total': recognized.count(),
    }
