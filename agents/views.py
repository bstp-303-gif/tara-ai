import io
import json
import os
import re
import threading
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

import pandas as pd

from . import ai_agent, content_defaults, monthly_reports, pipeline_ops, ranking
from .collector import highlight_missing_values, normalize_dataframe, save_normalized_file, validate_file
from .constants import MALAYSIA_STATES, PPD_BY_STATE, canonical_state
from .decorators import admin_or_moe_officer_required, admin_required, moe_officer_required, state_officer_required
from .eligibility import eligible_tracks
from .forms import ActivityReportForm, ApplicationForm, CertificationFileUploadForm, CertificationRuleForm, ProviderForm, TeacherForm
from .models import (
    ActivityReport, AgentActivityLog, Application, CertificationRule, ErrorLog, FileUpload,
    InvitationExclusion, InvitationRecord, MonthlyReminderLog, MonthlyReportLog, ProgrammeSettings, Provider,
    ReportRecipient, Teacher, TrackLimit,
)

TEMP_DIR = getattr(settings, 'GPGD_UPLOAD_DIR', os.path.join(settings.BASE_DIR, 'temp_uploads'))
os.makedirs(TEMP_DIR, exist_ok=True)

PROVIDERS = ['Google', 'Microsoft', 'Apple']


def csrf_failure(request, reason=''):
    """A stale form (an old tab, or Back after logging in) carries an expired CSRF token. Send the
    user back to that page to reload it instead of showing Django's bare 403 page."""
    if request.user.is_authenticated:  # the login page doesn't render messages
        messages.warning(request, 'That page had expired, so it was reloaded. Please try again.')
    return redirect(request.get_full_path())


@require_http_methods(["GET", "POST"])
@login_required
def upload_certifications(request):
    """Handle file upload for certification lists."""

    if request.method == 'POST':
        form = CertificationFileUploadForm(request.POST, request.FILES)

        if form.is_valid():
            uploaded_file = request.FILES['file_upload']
            provider = form.cleaned_data['provider']

            try:
                upload_id = str(uuid.uuid4())[:8]
                file_name = f"{provider}_{upload_id}.xlsx"
                file_path = os.path.join(TEMP_DIR, file_name)

                with open(file_path, 'wb+') as destination:
                    for chunk in uploaded_file.chunks():
                        destination.write(chunk)

                validation_result = validate_file(file_path, provider)

                file_record = FileUpload.objects.create(
                    upload_id=upload_id,
                    file_name=uploaded_file.name,
                    provider=provider,
                    file_path=file_path,
                    validation_status='valid' if validation_result['is_valid'] else 'invalid',
                    record_count=validation_result['record_count'],
                    error_message=validation_result['error_message'] or '',
                    missing_values_map=validation_result['missing_values_map']
                )

                if validation_result['is_valid']:
                    highlighted_path = os.path.join(TEMP_DIR, f"{provider}_highlighted_{upload_id}.xlsx")
                    if validation_result['missing_values_map']:
                        highlight_missing_values(file_path, highlighted_path, validation_result['missing_values_map'])

                    if validation_result['dataframe'] is not None:
                        normalized_df = normalize_dataframe(validation_result['dataframe'], provider)
                        if normalized_df is not None:
                            normalized_path = os.path.join(TEMP_DIR, f"{provider}_normalized_{upload_id}.xlsx")
                            save_normalized_file(normalized_df, normalized_path)
                            file_record.normalized_file_path = normalized_path
                            file_record.save()

                    if validation_result['error_message']:
                        messages.warning(request, f'⚠️ {provider} file uploaded with warnings: {validation_result["error_message"]}')
                    else:
                        messages.success(request, f'✅ {provider} file uploaded successfully! {validation_result["record_count"]} records found.')

                    ai_agent.run_pipeline_agent_async(
                        f"{provider} certification file uploaded ({validation_result['record_count']} records)."
                    )
                else:
                    messages.error(request, f'❌ {provider} file could not be read: {validation_result["error_message"]}')

                return redirect('agent1_results')

            except Exception as e:
                messages.error(request, f'Error processing file: {str(e)}')
    else:
        form = CertificationFileUploadForm()

    recent_uploads = FileUpload.objects.all().order_by('-uploaded_at')[:10]

    context = {
        'form': form,
        'recent_uploads': recent_uploads
    }

    return render(request, 'upload_certifications.html', context)


@require_http_methods(["GET"])
@login_required
def agent1_results(request):
    """Display validation results for uploaded files."""

    files = FileUpload.objects.all().order_by('-uploaded_at')

    google_files = files.filter(provider='Google')
    microsoft_files = files.filter(provider='Microsoft')
    apple_files = files.filter(provider='Apple')

    ready_to_process = any(
        FileUpload.objects.filter(provider=p, validation_status='valid').exists()
        for p in PROVIDERS
    )

    context = {
        'google_files': google_files,
        'microsoft_files': microsoft_files,
        'apple_files': apple_files,
        'all_files': files,
        'ready_to_process': ready_to_process,
    }

    return render(request, 'agent1_results.html', context)


@require_http_methods(["GET"])
@login_required
def agent1_dashboard(request):
    """Display Agent 1 processing dashboard."""

    files = FileUpload.objects.filter(validation_status='valid').order_by('-uploaded_at')
    teachers = Teacher.objects.all()

    total_records = FileUpload.objects.filter(validation_status='valid').aggregate(
        total=Sum('record_count')
    )['total'] or 0

    total_unique = teachers.count()
    multi_certified = teachers.filter(multi_certified=True).count()
    # Teachers who have applied are still eligible: their status just moves on to 'Application Submitted'.
    eligible = teachers.exclude(eligibility_status='Not Eligible').count()
    # A preview only: with a national roster the full list is thousands of rows (it's in the Excel download).
    eligible_preview = teachers.exclude(eligibility_status='Not Eligible')[:50]
    not_eligible = teachers.filter(eligibility_status='Not Eligible').count()

    deadline = ProgrammeSettings.load().submission_deadline

    # Attach each teacher's latest invitation attempt (by IC) so the list can show Sent/Failed by the name.
    awaiting_invitation = list(Teacher.objects.filter(
        eligibility_status='Eligible', application__isnull=True
    ).order_by('state', 'full_name'))
    records = InvitationRecord.objects.in_bulk([t.ic_number for t in awaiting_invitation], field_name='ic_number')
    stale_before = timezone.now() - timedelta(minutes=5)
    for teacher in awaiting_invitation:
        record = records.get(teacher.ic_number)
        # A "sending" flag older than 5 minutes means the background send was interrupted
        # (e.g. server restart) — show it as not sent so it's retried and the page stops auto-refreshing.
        if record and record.status == 'sending' and record.attempted_at < stale_before:
            record = None
        teacher.invitation = record
    invite_sending_count = sum(1 for t in awaiting_invitation if t.invitation and t.invitation.status == 'sending')
    invite_pending_count = sum(
        1 for t in awaiting_invitation
        if not (t.invitation and t.invitation.status == 'sent') and t.email and t.email.lower() != 'nan'
    )

    context = {
        'files': files,
        'teachers': teachers,
        'total_records': total_records,
        'total_unique': total_unique,
        'multi_certified': multi_certified,
        'eligible': eligible,
        'not_eligible': not_eligible,
        'eligible_preview': eligible_preview,
        'submission_deadline': deadline,
        'invitation_block_reason': pipeline_ops.invitation_block_reason(deadline),
        'awaiting_invitation': awaiting_invitation,
        'invite_pending_count': invite_pending_count,
        'invite_sending_count': invite_sending_count,
        'invite_sent_count': sum(1 for t in awaiting_invitation if t.invitation and t.invitation.status == 'sent'),
        'invitation_exclusions': InvitationExclusion.objects.all(),
        'pending_compile_count': Application.objects.filter(
            status__in=ranking.PIPELINE_ENTRY_STATUSES
        ).count(),
        'compile_block_reason': pipeline_ops.compile_block_reason(deadline),
        'agent_logs': AgentActivityLog.objects.all()[:10],
        'ANTHROPIC_API_KEY_SET': bool(getattr(settings, 'ANTHROPIC_API_KEY', '')),
    }

    return render(request, 'agent1_dashboard.html', context)


@require_http_methods(["GET"])
@login_required
def error_log(request):
    """Dedicated tab for concrete, actionable system errors — currently just email
    delivery failures, logged by pipeline_ops whenever a send fails."""
    category = request.GET.get('category', '').strip()
    errors = ErrorLog.objects.all()
    if category:
        errors = errors.filter(category=category)

    context = {
        'errors': errors[:200],
        'category': category,
        'category_choices': ErrorLog.CATEGORY_CHOICES,
        'total_count': ErrorLog.objects.count(),
    }
    return render(request, 'error_log.html', context)


@require_http_methods(["POST"])
@login_required
def update_submission_deadline(request):
    """Update the programme-wide application submission deadline used in invitation emails."""

    settings_obj = ProgrammeSettings.load()
    raw_deadline = request.POST.get('submission_deadline', '').strip()
    settings_obj.submission_deadline = parse_date(raw_deadline) if raw_deadline else None
    settings_obj.save()

    if settings_obj.submission_deadline:
        messages.success(request, f'✅ Submission deadline set to {settings_obj.submission_deadline.strftime("%d %B %Y")}.')
    else:
        messages.success(request, '✅ Submission deadline cleared.')

    return redirect('agent1_dashboard')


@require_http_methods(["GET", "POST"])
@login_required
def edit_invitation_email(request):
    """Edit the subject/body of the invitation email sent to eligible teachers."""
    settings_obj = ProgrammeSettings.load()

    if request.method == 'POST':
        settings_obj.invitation_email_subject = request.POST.get('subject', '').strip()
        settings_obj.invitation_email_body = request.POST.get('body', '').strip()
        settings_obj.save()
        messages.success(request, '✅ Invitation email template saved.')
        return redirect('edit_invitation_email')

    context = {
        'subject': settings_obj.invitation_email_subject or content_defaults.DEFAULT_INVITATION_SUBJECT,
        'body': settings_obj.invitation_email_body or content_defaults.DEFAULT_INVITATION_BODY,
        'placeholders': [f'{{{{{p}}}}}' for p in content_defaults.INVITATION_PLACEHOLDERS],
        'is_default': not settings_obj.invitation_email_subject and not settings_obj.invitation_email_body,
    }
    return render(request, 'edit_invitation_email.html', context)


@require_http_methods(["GET", "POST"])
@login_required
def edit_recognition_letter(request):
    """Edit the subject/body of the recognition letter sent when the MoE Officer approves an application."""
    settings_obj = ProgrammeSettings.load()

    if request.method == 'POST':
        settings_obj.recognition_letter_subject = request.POST.get('subject', '').strip()
        settings_obj.recognition_letter_body = request.POST.get('body', '').strip()
        settings_obj.save()
        messages.success(request, '✅ Recognition letter template saved.')
        return redirect('edit_recognition_letter')

    context = {
        'subject': settings_obj.recognition_letter_subject or content_defaults.DEFAULT_RECOGNITION_SUBJECT,
        'body': settings_obj.recognition_letter_body or content_defaults.DEFAULT_RECOGNITION_BODY,
        'placeholders': [f'{{{{{p}}}}}' for p in content_defaults.RECOGNITION_PLACEHOLDERS],
        'is_default': not settings_obj.recognition_letter_subject and not settings_obj.recognition_letter_body,
    }
    return render(request, 'edit_recognition_letter.html', context)


@require_http_methods(["GET", "POST"])
@login_required
def edit_application_form(request):
    """Edit the application form's section headings, field labels, and help text (apply.html)."""
    settings_obj = ProgrammeSettings.load()

    if request.method == 'POST':
        section_labels = {
            key: request.POST.get(f'section__{key}', '').strip() or default
            for key, default in content_defaults.DEFAULT_APPLY_SECTION_LABELS.items()
        }
        field_labels = {
            key: request.POST.get(f'field__{key}', '').strip() or default
            for key, default in content_defaults.DEFAULT_APPLY_FIELD_LABELS.items()
        }
        help_texts = {
            key: request.POST.get(f'help__{key}', '').strip() or default
            for key, default in content_defaults.DEFAULT_APPLY_HELP_TEXTS.items()
        }
        settings_obj.apply_form_section_labels = section_labels
        settings_obj.apply_form_field_labels = field_labels
        settings_obj.apply_form_help_texts = help_texts
        settings_obj.save()
        messages.success(request, '✅ Application form text saved.')
        return redirect('edit_application_form')

    section_labels = {**content_defaults.DEFAULT_APPLY_SECTION_LABELS, **(settings_obj.apply_form_section_labels or {})}
    field_labels = {**content_defaults.DEFAULT_APPLY_FIELD_LABELS, **(settings_obj.apply_form_field_labels or {})}
    help_texts = {**content_defaults.DEFAULT_APPLY_HELP_TEXTS, **(settings_obj.apply_form_help_texts or {})}

    context = {
        'section_labels': section_labels,
        'field_labels': field_labels,
        'help_texts': help_texts,
    }
    return render(request, 'edit_application_form.html', context)


@require_http_methods(["GET"])
@login_required
def download_file(request, file_id, file_type):
    """Download original, highlighted, or normalized file."""

    try:
        file_record = FileUpload.objects.get(id=file_id)

        if file_type == 'original':
            file_path = file_record.file_path
        elif file_type == 'highlighted':
            highlighted_path = os.path.join(TEMP_DIR, f"{file_record.provider}_highlighted_{file_record.upload_id}.xlsx")
            if os.path.exists(highlighted_path):
                file_path = highlighted_path
            else:
                messages.error(request, 'Highlighted file not found.')
                return redirect('agent1_results')
        elif file_type == 'normalized':
            if file_record.normalized_file_path and os.path.exists(file_record.normalized_file_path):
                file_path = file_record.normalized_file_path
            else:
                messages.error(request, 'Normalized file not found.')
                return redirect('agent1_results')
        else:
            messages.error(request, 'Invalid file type.')
            return redirect('agent1_results')

        if os.path.exists(file_path):
            response = FileResponse(open(file_path, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = f'attachment; filename="{file_record.provider}_{file_type}_{file_record.upload_id}.xlsx"'
            return response
        else:
            messages.error(request, 'File not found on server.')
            return redirect('agent1_results')

    except FileUpload.DoesNotExist:
        messages.error(request, 'File record not found.')
        return redirect('agent1_results')
    except Exception as e:
        messages.error(request, f'Error downloading file: {str(e)}')
        return redirect('agent1_results')


@require_http_methods(["POST"])
@login_required
def process_all_files(request):
    """Manual override: normally the pipeline agent runs this automatically after each upload."""

    result = pipeline_ops.run_certification_pipeline()

    if not result['ran']:
        messages.error(request, result['reason'])
        return redirect('agent1_results')

    if result['skipped_providers']:
        messages.warning(request, f'⚠️ No valid file for: {", ".join(result["skipped_providers"])}. Processing continued with the remaining provider(s).')

    stats = result['stats']
    messages.success(request, f'✅ Processing complete! {stats["total_unique"]} unique teachers, {stats["eligible"]} eligible candidates.')

    return redirect('agent1_dashboard')


@require_http_methods(["POST"])
@login_required
def delete_file(request, file_id):
    """Delete an uploaded file record and its associated files."""
    try:
        file_record = FileUpload.objects.get(id=file_id)

        for path in [
            file_record.file_path,
            file_record.normalized_file_path,
            os.path.join(TEMP_DIR, f"{file_record.provider}_highlighted_{file_record.upload_id}.xlsx"),
        ]:
            if path and os.path.exists(path):
                os.remove(path)

        provider = file_record.provider
        file_name = file_record.file_name
        file_record.delete()
        cleared = pipeline_ops.prune_stale_exclusions()
        if cleared:
            messages.success(request, f'🗑️ File "{file_name}" deleted. {cleared} teacher(s) removed by Admin were also forgotten, since they are no longer in any uploaded file.')
        else:
            messages.success(request, f'🗑️ File "{file_name}" deleted.')

        ai_agent.run_pipeline_agent_async(f"{provider} certification file deleted — roster may need recomputing.")
    except FileUpload.DoesNotExist:
        messages.error(request, 'File record not found.')
    except Exception as e:
        messages.error(request, f'Error deleting file: {str(e)}')

    return redirect('upload_certifications')


@require_http_methods(["GET"])
@login_required
def download_combined_normalized(request):
    """Download combined normalized data."""
    if os.path.exists(pipeline_ops.COMBINED_PATH):
        response = FileResponse(open(pipeline_ops.COMBINED_PATH, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="Combined_Normalized_Data.xlsx"'
        return response
    messages.error(request, 'File not found.')
    return redirect('agent1_dashboard')


@require_http_methods(["GET"])
@login_required
def download_deduplicated(request):
    """Download deduplicated teacher list."""
    if os.path.exists(pipeline_ops.DEDUP_PATH):
        response = FileResponse(open(pipeline_ops.DEDUP_PATH, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="Deduplicated_Teacher_List.xlsx"'
        return response
    messages.error(request, 'File not found.')
    return redirect('agent1_dashboard')


@require_http_methods(["GET"])
@login_required
def download_eligible_candidates(request):
    """Download eligible candidates ranked list."""
    if os.path.exists(pipeline_ops.ELIGIBLE_PATH):
        response = FileResponse(open(pipeline_ops.ELIGIBLE_PATH, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="Eligible_Candidates_Ranked.xlsx"'
        return response
    messages.error(request, 'File not found.')
    return redirect('agent1_dashboard')


# ---------------------------------------------------------------------------
# GPGD Application — invitation emails + online form
# ---------------------------------------------------------------------------

def _read_apply_token(token):
    # With a deadline set, the link stays valid until the form closes (see apply());
    # without one, fall back to the original 30-day expiry.
    max_age = None if ProgrammeSettings.load().submission_deadline else 60 * 60 * 24 * 30
    try:
        return signing.loads(token, salt='gpgd-apply', max_age=max_age)
    except signing.BadSignature:
        return None


def _send_invitations_background(triggered_by):
    """Runs the actual SMTP sends and logs the real outcome to AgentActivityLog.

    Sending is one send_mail() round-trip per teacher, and a slow/misconfigured SMTP
    server can take EMAIL_TIMEOUT seconds per recipient — with dozens of eligible
    teachers that adds up fast, so this runs off-thread instead of blocking the
    request (same reasoning as ai_agent.run_pipeline_agent_async).
    """
    result = pipeline_ops.send_invitation_emails(sent_by=triggered_by)
    if result.get('skipped_reason'):
        AgentActivityLog.objects.create(
            trigger_reason=f'Admin approved invitations (by {triggered_by})',
            summary='No invitations sent.',
            actions_taken=[{'tool': 'send_invitation_emails', 'input': {}, 'result': result}],
            status='error',
            error_message=result['skipped_reason'],
        )
        return

    failed = result['failed']
    status = 'error' if failed else 'success'

    summary = (
        f"Sent {result['sent']} invitation(s) out of {result['eligible_total']} eligible teacher(s); "
        f"{result['no_email']} skipped (no email on file); {len(failed)} failed to send."
    )

    # The dashboard shows `summary` OR `error_message` depending on status, never both —
    # so when there's a failure, fold the summary into error_message too, otherwise the
    # "X sent" count would be hidden behind just the failure list.
    error_message = ''
    if failed:
        error_message = summary + ' First failure(s): '
        error_message += '; '.join(f"{f['teacher']} <{f['email']}>: {f['error']}" for f in failed[:5])
        if len(failed) > 5:
            error_message += f'; and {len(failed) - 5} more (see server logs)'

    AgentActivityLog.objects.create(
        trigger_reason=f'Admin approved invitations (by {triggered_by})',
        summary=summary,
        actions_taken=[{'tool': 'send_invitation_emails', 'input': {}, 'result': result}],
        status=status,
        error_message=error_message,
    )


@require_http_methods(["POST"])
@admin_required
def send_invitations(request):
    """Admin approval gate: invitations go out only once the Admin has reviewed the deduplicated
    eligible list and approved sending — the pipeline agent never sends them itself."""
    block_reason = pipeline_ops.invitation_block_reason(ProgrammeSettings.load().submission_deadline)
    if block_reason:
        messages.error(request, f'⚠️ Invitations not sent. {block_reason}')
        return redirect('agent1_dashboard')

    pipeline_ops.mark_invitations_sending(sent_by=request.user.get_username())
    threading.Thread(
        target=_send_invitations_background,
        args=(request.user.get_username(),),
        daemon=True,
    ).start()
    messages.info(
        request,
        '📨 Sending invitation emails now. Each teacher shows "Sending…" and the page refreshes '
        'by itself until it changes to "Email sent" (or "Failed").'
    )
    return redirect('agent1_dashboard')


@require_http_methods(["POST"])
@admin_required
def remove_from_invitations(request):
    """Admin: drop one or more teachers (POST teacher_ids) from the eligible list before invitations
    go out. Recorded as InvitationExclusions so the next pipeline run doesn't bring them back."""
    teachers = list(Teacher.objects.filter(
        id__in=request.POST.getlist('teacher_ids'), eligibility_status='Eligible', application__isnull=True
    ))
    if not teachers:
        messages.warning(request, 'No teachers selected.')
        return redirect(reverse('agent1_dashboard') + '#invitations')

    with transaction.atomic():
        for teacher in teachers:
            InvitationExclusion.objects.update_or_create(
                ic_number=teacher.ic_number,
                defaults={'full_name': teacher.full_name, 'removed_by': request.user},
            )
        Teacher.objects.filter(id__in=[t.id for t in teachers]).delete()

    label = teachers[0].full_name if len(teachers) == 1 else f'{len(teachers)} teachers'
    messages.success(request, f'🗑️ {label} removed from the eligible list — they will not be invited. Removed by mistake? Click Undo in the red "Removed by Admin" box.')
    return redirect(reverse('agent1_dashboard') + '#invitations')


@require_http_methods(["POST"])
@admin_required
def restore_to_invitations(request):
    """Admin: undo one or more removals (POST exclusion_ids). Re-runs the pipeline so the teachers
    are rebuilt from the uploaded files."""
    exclusions = InvitationExclusion.objects.filter(id__in=request.POST.getlist('exclusion_ids'))
    names = list(exclusions.values_list('full_name', flat=True))
    if not names:
        messages.warning(request, 'No teachers selected.')
        return redirect(reverse('agent1_dashboard') + '#invitations')

    exclusions.delete()
    result = pipeline_ops.run_certification_pipeline()
    label = names[0] if len(names) == 1 else f'{len(names)} teachers'
    if result.get('ran'):
        messages.success(request, f'↩️ {label} restored to the eligible list.')
    else:
        messages.warning(request, f'↩️ {label} restored, but the roster could not be rebuilt: {result.get("reason", "")}')
    return redirect(reverse('agent1_dashboard') + '#invitations')


@require_http_methods(["GET", "POST"])
def apply(request, token):
    """Public application form — pre-filled from token."""
    ic_number = _read_apply_token(token)
    if ic_number is None:
        return render(request, 'apply_invalid.html', status=400)

    teacher = Teacher.objects.filter(ic_number=ic_number).first()

    # If already applied, show a message
    if teacher and hasattr(teacher, 'application'):
        return render(request, 'apply_already.html', {'application': teacher.application})

    deadline = ProgrammeSettings.load().submission_deadline
    if pipeline_ops.submissions_closed(deadline):
        return render(request, 'apply_closed.html', {'deadline': deadline})

    initial = {}
    allowed_tracks = None
    if teacher:
        allowed_tracks = eligible_tracks(teacher)
        initial = {
            'full_name':      teacher.full_name,
            'ic_number':      teacher.ic_number,
            'email':          teacher.email if teacher.email.lower() != 'nan' else '',
            'school_name':    teacher.school,
            'state':          canonical_state(teacher.state),
            # Single-track teachers get it preselected; multi-certified teachers must choose.
            'tech_track':     allowed_tracks[0] if len(allowed_tracks) == 1 else '',
            'certifications': ', '.join(c.strip() for c in str(teacher.certification or '').split(',') if c.strip()),
        }

    if request.method == 'POST':
        form = ApplicationForm(request.POST, allowed_tracks=allowed_tracks)
        if form.is_valid():
            application = form.save(commit=False)
            if teacher:
                application.teacher = teacher
                teacher.eligibility_status = 'Application Submitted'
                teacher.save()
            application.save()

            # Send acknowledgement email
            _send_acknowledgement(application)

            # Not compiled here: the MoE Officer compiles every application once, after the deadline.
            return redirect('apply_success', ref=application.reference_number)
    else:
        form = ApplicationForm(initial=initial, allowed_tracks=allowed_tracks)

    settings_obj = ProgrammeSettings.load()
    section_labels = {**content_defaults.DEFAULT_APPLY_SECTION_LABELS, **(settings_obj.apply_form_section_labels or {})}
    help_texts = {**content_defaults.DEFAULT_APPLY_HELP_TEXTS, **(settings_obj.apply_form_help_texts or {})}

    return render(request, 'apply.html', {
        'form': form, 'token': token, 'section_labels': section_labels, 'help_texts': help_texts,
    })


def _send_acknowledgement(application):
    subject = f"GPGD Application Received — Reference: {application.reference_number}"
    body = f"""Dear {application.full_name},

Thank you for submitting your application for the Guru Peneraju Generasi Digital (GPGD) Programme.

Your application has been received successfully. Please keep the following reference number for your records:

  Reference Number: {application.reference_number}

We will review your application and contact you regarding the next steps. Please note that only complete applications will be considered.

If you have any questions, please contact BSTP at sptp@moe.gov.my.

Thank you.

Yours sincerely,
Sektor Pengintegrasian Teknologi Pendidikan (SPTP)
Bahagian Sumber dan Teknologi Pendidikan (BSTP)
Kementerian Pendidikan Malaysia
"""
    pipeline_ops.send_single_email_async(
        subject, body, application.email,
        context_label=f"Acknowledgement — {application.full_name} <{application.email}> ({application.reference_number})",
    )


@require_http_methods(["GET"])
def apply_success(request, ref):
    application = get_object_or_404(Application, reference_number=ref)
    return render(request, 'apply_success.html', {'application': application})


# ---------------------------------------------------------------------------
# In-system management (Providers, Certification Rules, Applications, Teachers)
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@login_required
def manage_providers(request):
    """List providers with their certification rules, plus forms to add/edit both."""
    providers = Provider.objects.prefetch_related('certification_rules').all()

    providers_with_forms = []
    for provider in providers:
        rules_with_forms = [
            (rule, CertificationRuleForm(instance=rule)) for rule in provider.certification_rules.all()
        ]
        providers_with_forms.append({
            'provider': provider,
            'edit_form': ProviderForm(instance=provider),
            'rules': rules_with_forms,
            'add_rule_form': CertificationRuleForm(),
        })

    context = {
        'providers_with_forms': providers_with_forms,
        'add_provider_form': ProviderForm(),
    }
    return render(request, 'manage_providers.html', context)


@require_http_methods(["POST"])
@login_required
def add_provider(request):
    form = ProviderForm(request.POST)
    if form.is_valid():
        provider = form.save()
        messages.success(request, f'✅ Provider "{provider.display_name}" added.')
    else:
        messages.error(request, f'Could not add provider: {form.errors.as_text()}')
    return redirect('manage_providers')


@require_http_methods(["POST"])
@login_required
def edit_provider(request, provider_id):
    provider = get_object_or_404(Provider, id=provider_id)
    form = ProviderForm(request.POST, instance=provider)
    if form.is_valid():
        form.save()
        messages.success(request, f'✅ Provider "{provider.display_name}" updated.')
    else:
        messages.error(request, f'Could not update provider: {form.errors.as_text()}')
    return redirect('manage_providers')


@require_http_methods(["POST"])
@login_required
def delete_provider(request, provider_id):
    provider = get_object_or_404(Provider, id=provider_id)
    name = provider.display_name
    provider.delete()
    messages.success(request, f'🗑️ Provider "{name}" and its certification rules deleted.')
    return redirect('manage_providers')


@require_http_methods(["POST"])
@login_required
def add_rule(request, provider_id):
    provider = get_object_or_404(Provider, id=provider_id)
    form = CertificationRuleForm(request.POST)
    if form.is_valid():
        rule = form.save(commit=False)
        rule.provider = provider
        rule.save()
        messages.success(request, f'✅ Rule "{rule.canonical_name}" added to {provider.display_name}.')
    else:
        messages.error(request, f'Could not add rule: {form.errors.as_text()}')
    return redirect('manage_providers')


@require_http_methods(["POST"])
@login_required
def edit_rule(request, rule_id):
    rule = get_object_or_404(CertificationRule, id=rule_id)
    form = CertificationRuleForm(request.POST, instance=rule)
    if form.is_valid():
        form.save()
        messages.success(request, f'✅ Rule "{rule.canonical_name}" updated.')
    else:
        messages.error(request, f'Could not update rule: {form.errors.as_text()}')
    return redirect('manage_providers')


@require_http_methods(["POST"])
@login_required
def delete_rule(request, rule_id):
    rule = get_object_or_404(CertificationRule, id=rule_id)
    name = rule.canonical_name
    rule.delete()
    messages.success(request, f'🗑️ Rule "{name}" deleted.')
    return redirect('manage_providers')


@require_http_methods(["GET"])
@login_required
def manage_applications(request):
    """List all applications with inline status editing."""
    applications = Application.objects.all().order_by('-submitted_at')

    status_filter = request.GET.get('status', '')
    if status_filter:
        applications = applications.filter(status=status_filter)

    context = {
        'applications': applications,
        'status_choices': Application.STATUS_CHOICES,
        'status_filter': status_filter,
    }
    return render(request, 'manage_applications.html', context)


@require_http_methods(["POST"])
@login_required
def update_application_status(request, application_id):
    application = get_object_or_404(Application, id=application_id)
    new_status = request.POST.get('status', '')
    if new_status in dict(Application.STATUS_CHOICES):
        application.status = new_status
        application.save()
        messages.success(request, f'✅ {application.full_name} ({application.reference_number}) marked as "{new_status}".')
    else:
        messages.error(request, 'Invalid status.')
    return redirect('manage_applications')


@require_http_methods(["POST"])
@login_required
def delete_application(request, application_id):
    application = get_object_or_404(Application, id=application_id)
    ref = application.reference_number
    application.delete()
    messages.success(request, f'🗑️ Application {ref} deleted.')
    return redirect('manage_applications')


@require_http_methods(["GET"])
@login_required
def manage_teachers(request):
    """List all teachers with links to edit/delete."""
    teachers = Teacher.objects.all()

    search = request.GET.get('q', '').strip()
    if search:
        teachers = teachers.filter(full_name__icontains=search)

    context = {
        'teachers': teachers,
        'search': search,
    }
    return render(request, 'manage_teachers.html', context)


@require_http_methods(["GET", "POST"])
@login_required
def edit_teacher(request, teacher_id):
    teacher = get_object_or_404(Teacher, id=teacher_id)

    if request.method == 'POST':
        form = TeacherForm(request.POST, instance=teacher)
        if form.is_valid():
            form.save()
            messages.success(request, f'✅ {teacher.full_name} updated.')
            return redirect('manage_teachers')
    else:
        form = TeacherForm(instance=teacher)

    return render(request, 'teacher_form.html', {'form': form, 'teacher': teacher})


@require_http_methods(["POST"])
@login_required
def delete_teacher(request, teacher_id):
    teacher = get_object_or_404(Teacher, id=teacher_id)
    name = teacher.full_name
    teacher.delete()
    messages.success(request, f'🗑️ Teacher "{name}" deleted.')
    return redirect('manage_teachers')


# ---------------------------------------------------------------------------
# Compilation + two-stage officer approval (State Officer, then MoE Officer)
# ---------------------------------------------------------------------------

@login_required
def officer_home(request):
    """Routes a freshly-logged-in user: officers to their own review page, everyone else to the dashboard."""
    profile = getattr(request.user, 'officerprofile', None)
    if not profile:
        return redirect('agent1_dashboard')
    if profile.role == 'state_officer':
        return redirect('state_review')
    return redirect('moe_review')


@require_http_methods(["POST"])
@admin_or_moe_officer_required
def compile_applications(request):
    """Score, rank, and quota-flag every pending application, ready for State Officer review."""
    # Back to whichever page the button was on: the MoE Review page or the Admin dashboard.
    back = 'moe_review' if getattr(request.user, 'officerprofile', None) else 'agent1_dashboard'
    block_reason = pipeline_ops.compile_block_reason(ProgrammeSettings.load().submission_deadline)
    if block_reason:
        messages.error(request, block_reason)
        return redirect(back)

    stats, shortfalls = ranking.compile_and_rank()

    if stats['total_compiled'] == 0:
        messages.warning(request, 'No pending applications to compile.')
    else:
        messages.success(
            request,
            f"✅ Compiled {stats['total_compiled']} application(s) — "
            f"{stats['recommended']} flagged as Recommended (minimum quota)."
        )

    if shortfalls:
        empty = sum(1 for s in shortfalls if not s['have'])
        states = len({s['state'] for s in shortfalls})
        messages.warning(
            request,
            f"⚠️ {len(shortfalls)} district/track group(s) in {states} state(s) have fewer applicants than the minimum "
            f"of 2 ({empty} with no applicants at all). See By District for where they are.",
        )

    return redirect(back)


@require_http_methods(["GET", "POST"])
@state_officer_required
def state_review(request):
    """State Officer: approve/decline the compiled, ranked shortlist for their own state."""
    profile = request.user.officerprofile

    if request.method == 'POST' and request.POST.get('bulk'):
        # Ticked candidates, still waiting, in this officer's state; best-ranked first, so if a maximum
        # runs out, the higher-ranked candidates get the places.
        chosen = Application.objects.filter(
            id__in=request.POST.getlist('application_ids'), state=profile.state, status='Compiled').order_by('rank_in_state')
        _bulk_decide(request, chosen, request.POST.get('bulk'),
                     lambda a, d: _state_decide(a, d, request.user, request.POST.get('remarks', '')))
        return redirect(f"{reverse('state_review')}?{request.POST.get('filters', '')}")

    if request.method == 'POST':
        # Only while the MoE hasn't acted yet — after that the State decision is locked.
        application = get_object_or_404(
            Application, id=request.POST.get('application_id'), state=profile.state,
            status__in=ranking.COMPILED_PIPELINE_STATUSES,
        )
        back = 'state_decisions' if request.POST.get('next') == 'state_decisions' else 'state_review'
        problem = _state_decide(application, request.POST.get('decision'), request.user, request.POST.get('remarks', ''))
        if problem:
            messages.error(request, problem)
            return redirect(back)
        messages.success(request, f'✅ {application.full_name} ({application.reference_number}) marked as "{application.status}".')
        if back == 'state_review':
            district, track, _ = ranking.quota_group(application)
            return redirect(f"{reverse('state_review')}?{request.POST.get('filters', '')}#{_group_anchor(district, track)}")
        return redirect(back)

    # Waiting applications, grouped by district and technology track: the State Officer decides one
    # group at a time, against that group's minimum of 2 (5 state-wide where there are no districts).
    district_filter, track_filter = request.GET.get('district', ''), request.GET.get('track', '')
    progress = {(row['district'], row['tech_track']): row for row in ranking.state_progress(profile.state)}
    grouped = {}
    for application in Application.objects.filter(state=profile.state, status='Compiled').order_by('rank_in_state'):
        district, track, minimum = ranking.quota_group(application)
        grouped.setdefault((district, track), []).append(application)
    waiting_by_district = {}
    for (district, _), group in grouped.items():
        waiting_by_district[district] = waiting_by_district.get(district, 0) + len(group)

    groups = []
    for (district, track), group in sorted(grouped.items(), key=lambda kv: (kv[0][0] or '', kv[0][1])):
        if (district_filter and (district or '') != district_filter) or (track_filter and track != track_filter):
            continue
        row = progress.get((district, track), {})
        groups.append({
            'district': district, 'track': track, 'applications': group, 'anchor': _group_anchor(district, track),
            'need': row.get('need'), 'approved': row.get('approved', 0), 'remaining': row.get('remaining'),
            'percent': row.get('percent', 0), 'recommended': sum(a.is_recommended for a in group),
        })

    context = {
        'groups': groups,
        'recommended_shown': sum(g['recommended'] for g in groups),
        'waiting_total': sum(len(g) for g in grouped.values()),
        'district_choices': sorted(waiting_by_district.items(), key=lambda kv: kv[0] or ''),
        'tracks': [t for t, _ in Application.TRACK_CHOICES],
        'district_filter': district_filter,
        'track_filter': track_filter,
        'filters': request.GET.urlencode(),
        'state': profile.state,
        'shortfalls': ranking.shortfalls_for_state(profile.state),
        'decided_count': _state_decided_applications(profile.state).count(),
        'limit_usage': ranking.limit_usage(profile.state),
        'district_limits': [
            {'district': d, 'cells': ranking.limit_usage(profile.state, d)}
            for d in TrackLimit.objects.filter(state=profile.state).exclude(district='')
                     .values_list('district', flat=True).distinct().order_by('district')
        ],
    }
    return render(request, 'state_review.html', context)


@require_http_methods(["GET", "POST"])
@moe_officer_required
def moe_decisions(request):
    """MoE Officer: everyone they have recognised or rejected this year, and whether each recognised
    GPGD's Letter of Recognition was emailed."""
    if request.method == 'POST':  # resend a Letter of Recognition
        application = get_object_or_404(Application, id=request.POST.get('application_id'), status='Approved')
        _send_recognition_letter(application)
        messages.success(request, f'📨 Letter of Recognition is being emailed again to {application.full_name} <{application.email}>.')
        return redirect(f"{reverse('moe_decisions')}?{request.POST.get('filters', '')}")

    decided = ranking.this_years_applications().filter(status__in=['Approved', 'Rejected'], moe_decision_at__isnull=False)
    show, state, track = request.GET.get('show', 'all'), request.GET.get('state', ''), request.GET.get('track', '')
    search = request.GET.get('q', '').strip()
    counts = {'all': decided.count(), 'approved': decided.filter(status='Approved').count(),
              'rejected': decided.filter(status='Rejected').count()}
    applications = decided.filter(status={'approved': 'Approved', 'rejected': 'Rejected'}[show]) if show in ('approved', 'rejected') else decided
    if state in MALAYSIA_STATES:
        applications = applications.filter(state=state)
    if track:
        applications = applications.filter(tech_track=track)
    if search:
        applications = applications.filter(Q(full_name__icontains=search) | Q(ic_number__icontains=search)
                                           | Q(reference_number__icontains=search) | Q(school_name__icontains=search))
    applications = applications.order_by('-moe_decision_at', 'full_name')

    if request.GET.get('download'):
        return _excel_response(pd.DataFrame([{
            'Decision': 'Recognised' if a.status == 'Approved' else 'Rejected', 'Decided': timezone.localtime(a.moe_decision_at).strftime('%Y-%m-%d %H:%M'),
            'Name': a.full_name, 'IC Number': a.ic_number, 'Reference': a.reference_number, 'Email': a.email,
            'WhatsApp': a.whatsapp_number, 'State': a.state, 'District': a.district, 'School': a.school_name,
            'Track': a.tech_track, 'Award Category': a.award_category, 'State Remarks': a.state_remarks,
            'MoE Remarks': a.moe_remarks,
            'Letter Emailed': timezone.localtime(a.recognition_letter_sent_at).strftime('%Y-%m-%d %H:%M') if a.recognition_letter_sent_at else '',
        } for a in applications]), 'GPGD_MoE_Decisions.xlsx', 'Decisions')

    page = Paginator(applications, 50).get_page(request.GET.get('page'))
    recently = timezone.now() - timedelta(minutes=10)
    for application in page:
        # No result yet: still sending if just approved; otherwise approved before letters were tracked.
        application.letter_pending = bool(application.moe_decision_at and application.moe_decision_at > recently)
    context = {
        'page': page, 'counts': counts, 'show': show if show in ('approved', 'rejected') else 'all',
        'state_filter': state, 'track_filter': track, 'q': search,
        'states': MALAYSIA_STATES, 'tracks': [t for t, _ in Application.TRACK_CHOICES],
        'filters': request.GET.urlencode(), 'year': timezone.localdate().year,
        'reminder_day': ProgrammeSettings.load().reminder_day,
    }
    return render(request, 'moe_decisions.html', context)


def _state_decide(application, decision, user, remarks):
    """A State Officer's approve/decline. Returns None when done, or why it couldn't be."""
    if decision == 'approve':
        full = ranking.limit_reached(application, ranking.QUOTA_FILLING_STATUSES)
        if full:
            return _limit_message(application, *full)
        application.status = 'State Approved'
    elif decision == 'decline':
        application.status = 'State Declined'
    else:
        return 'Invalid decision.'
    application.state_decision_by = user
    application.state_decision_at = timezone.now()
    application.state_remarks = (remarks or '').strip()
    application.save()
    return None


def _moe_decide(application, decision, user, remarks, award_category=''):
    """The MoE Officer's final approve/reject; approval sends the Letter of Recognition. Returns None
    when done, or why it couldn't be."""
    if decision == 'approve':
        full = ranking.limit_reached(application, ['Approved'])
        if full:
            return _limit_message(application, *full)
        application.status = 'Approved'
        application.recognized_year = timezone.now().year
        application.award_category = (award_category or '').strip()
    elif decision == 'decline':
        application.status = 'Rejected'
    else:
        return 'Invalid decision.'
    application.moe_decision_by = user
    application.moe_decision_at = timezone.now()
    application.moe_remarks = (remarks or '').strip()
    application.save()
    if application.status == 'Approved':
        _send_recognition_letter(application)
    return None


def _bulk_decide(request, applications, decision, decide):
    """Applies `decide(application, decision)` to each ticked application and reports what happened."""
    if decision not in ('approve', 'decline'):
        messages.error(request, 'Invalid decision.')
        return
    done, skipped = 0, []
    for application in applications:
        problem = decide(application, decision)
        if problem:
            skipped.append(f'{application.full_name}: {problem}')
        else:
            done += 1
    if done:
        messages.success(request, f"✅ {done} candidate(s) {'approved' if decision == 'approve' else 'declined'}."
                                  + (" Each is now a recognised GPGD and their Letter of Recognition is being emailed."
                                     if decision == 'approve' and request.resolver_match.url_name == 'moe_review' else ''))
    elif not skipped:
        messages.warning(request, 'Nothing was ticked, or the ticked candidates were already decided.')
    if skipped:
        messages.error(request, f'{len(skipped)} not changed. ' + ' '.join(skipped[:5]) + (' …' if len(skipped) > 5 else ''))


def _moe_back(request, application):
    """Back to MoE Review with the same filters, at the decided candidate's group."""
    district, track, _ = ranking.quota_group(application)
    anchor = _group_anchor(f"{application.state} {district or ''}", track)
    return f"{reverse('moe_review')}?{request.POST.get('filters', '')}#{anchor}"


def _group_anchor(district, track):
    """An HTML id for a district/track group on State Review, e.g. "ppd-kulai-google"."""
    return re.sub(r'[^a-z0-9]+', '-', f"{district or 'state'} {track}".lower()).strip('-')


def _limit_message(application, where, used, maximum):
    return (f"Not approved: {where} has reached its maximum of {maximum} {application.tech_track} "
            f"GPGD(s) this year ({used} already approved). Decline another {application.tech_track} candidate "
            f"in {where} first, or ask the MoE to raise the maximum.")


@require_http_methods(["GET", "POST"])
@admin_or_moe_officer_required
def track_limits(request):
    """Admin / MoE Officer: the maximum number of GPGDs per technology track (blank = no limit), for every
    state, or with ?state= for each district (PPD) of that state."""
    tracks = [t for t, _ in Application.TRACK_CHOICES]
    state = request.GET.get('state') or request.POST.get('state') or ''
    if not PPD_BY_STATE.get(state):
        state = ''  # states without PPDs only have a state-wide maximum
    # Each row is a state (district '') or, on a state's page, one of its districts.
    rows_for = [(state, d) for d in PPD_BY_STATE[state]] if state else [(s, '') for s in MALAYSIA_STATES]

    if request.method == 'POST':
        errors = []
        for row_state, district in rows_for:
            for track in tracks:
                raw = request.POST.get(f'max__{district or row_state}__{track}', '').strip()
                where = dict(state=row_state, district=district, tech_track=track)
                if not raw:
                    TrackLimit.objects.filter(**where).delete()
                elif raw.isdigit():
                    TrackLimit.objects.update_or_create(**where, defaults={'maximum': int(raw), 'updated_by': request.user})
                else:
                    errors.append(f'{district or row_state} / {track}: "{raw}" is not a whole number')
        if errors:
            messages.error(request, 'Not saved: ' + '; '.join(errors) + '. The other values were saved.')
        else:
            messages.success(request, '✅ Maximums saved.')
        return redirect(f"{reverse('track_limits')}?state={state}" if state else 'track_limits')

    rows = []
    totals = {t: {'maximum': 0, 'used': 0, 'all_set': True} for t in tracks}
    for row_state, district in rows_for:
        usage = ranking.limit_usage(row_state, district)
        for cell in usage:
            total = totals[cell['track']]
            total['used'] += cell['used']
            if cell['maximum'] is None:
                total['all_set'] = False
            else:
                total['maximum'] += cell['maximum']
        rows.append({'name': district or row_state, 'cells': usage,
                     'has_districts': not state and bool(PPD_BY_STATE.get(row_state))})
    context = {
        'tracks': tracks, 'rows': rows, 'totals': [{'track': t, **totals[t]} for t in tracks],
        'state': state, 'state_usage': ranking.limit_usage(state) if state else None,
    }
    return render(request, 'track_limits.html', context)


@require_http_methods(["GET"])
@state_officer_required
def state_statistics(request):
    """State Officer: an overview of where every candidate in their state is in the process."""
    state = request.user.officerprofile.state
    applications = ranking.this_years_applications().filter(state=state)
    status_counts = dict(applications.values_list('status').annotate(n=Count('id')))

    def count(*statuses):
        return sum(status_counts.get(s, 0) for s in statuses)

    eligible = sum(
        1 for teacher_state, status in Teacher.objects.values_list('state', 'eligibility_status')
        if canonical_state(teacher_state) == state and status != 'Not Eligible'
    )
    applied = applications.count()
    quota_rows = ranking.state_progress(state)
    quota_need = sum(r['need'] for r in quota_rows)
    quota_filled = sum(r['filled'] for r in quota_rows)

    funnel = {
        'eligible': eligible,
        'applied': applied,
        'awaiting_compile': count(*ranking.PIPELINE_ENTRY_STATUSES),
        'awaiting_you': count('Compiled'),
        'approved': count('State Approved', 'Approved', 'Rejected'),
        'declined': count('State Declined'),
        'awaiting_moe': count('State Approved'),
        'moe_approved': count('Approved'),
        'moe_rejected': count('Rejected'),
    }
    by_track = [
        {
            'track': track,
            'applied': applications.filter(tech_track=track).count(),
            'approved': applications.filter(tech_track=track, status__in=['State Approved', 'Approved', 'Rejected']).count(),
            'declined': applications.filter(tech_track=track, status='State Declined').count(),
        }
        for track, _ in Application.TRACK_CHOICES
    ]

    context = {
        'state': state,
        'funnel': funnel,
        'application_rate': round(100 * applied / eligible) if eligible else None,
        'decided_percent': round(100 * (funnel['approved'] + funnel['declined']) / applied) if applied else 0,
        'quota_rows': quota_rows,
        'quota_need': quota_need,
        'quota_filled': quota_filled,
        'quota_remaining': quota_need - quota_filled,
        'quota_percent': round(100 * quota_filled / quota_need) if quota_need else 0,
        'by_track': by_track,
        'deadline': ProgrammeSettings.load().submission_deadline,
        # Until the MoE compiles, nothing has reached the State Officer yet.
        'compiled_yet': applied > funnel['awaiting_compile'],
        'district_min': ranking.DISTRICT_MINIMUM_PER_TECH,
        'no_district_min': ranking.NO_DISTRICT_STATE_MINIMUM_PER_TECH,
    }
    return render(request, 'state_statistics.html', context)


# Plain-language stage of an application, per viewer. State Officers see their own decisions as "you".
_STAGES = {
    'state_officer': {
        'Application Submitted': 'Waiting for MoE to compile',
        'Under Review': 'Waiting for MoE to compile',
        'Compiled': 'Waiting for your decision',
        'State Approved': 'Approved by you',
        'State Declined': 'Declined by you',
        'Approved': 'Approved by MoE',
        'Rejected': 'Rejected by MoE',
    },
    'moe_officer': {
        'Application Submitted': 'Waiting for you to compile',
        'Under Review': 'Waiting for you to compile',
        'Compiled': 'Waiting for State decision',
        'State Approved': 'Waiting for your final decision',
        'State Declined': 'Declined by State',
        'Approved': 'Approved by you',
        'Rejected': 'Rejected by you',
    },
}


def _grid_counts(group):
    return {
        'total': len(group),
        'approved': sum(1 for a in group if a.status in ('State Approved', 'Approved', 'Rejected')),
        'declined': sum(1 for a in group if a.status == 'State Declined'),
        'pending': sum(1 for a in group if a.status in ('Application Submitted', 'Under Review', 'Compiled')),
    }


def _grid_minimum(state):
    """The minimum per district and track in `state` (5 state-wide where there are no districts)."""
    no_districts = state in ranking.NO_DISTRICT_STATES or not PPD_BY_STATE.get(state)
    return ranking.NO_DISTRICT_STATE_MINIMUM_PER_TECH if no_districts else ranking.DISTRICT_MINIMUM_PER_TECH


def _application_grid(request, applications, row_of, viewer, fixed_rows=(), minimum=None):
    """Builds a rows x technology-track grid of application counts, plus the applicant list for the
    cell picked via ?row=<key>&track=<track>. row_of(application) returns (key, display name);
    fixed_rows are (key, name) pairs always shown, even with no applications. With `minimum`, cells
    with fewer applications than that are flagged as below the minimum."""
    tracks = [track for track, _ in Application.TRACK_CHOICES]
    names = dict(fixed_rows)
    keyed = []
    for application in applications:
        key, name = row_of(application)
        names.setdefault(key, name)
        keyed.append((key, application))

    rows = []
    fixed_keys = {key for key, _ in fixed_rows}
    ordered = list(fixed_rows) + sorted(((k, n) for k, n in names.items() if k not in fixed_keys), key=lambda kv: kv[1])
    for key, name in ordered:
        in_row = [a for k, a in keyed if k == key]
        cells = [{'track': t, **_grid_counts([a for a in in_row if a.tech_track == t])} for t in tracks]
        for cell in cells:
            cell['below_min'] = minimum is not None and cell['total'] < minimum
        rows.append({'key': key, 'name': name, 'cells': cells, 'total': _grid_counts(in_row)})

    selected_row, selected_track = request.GET.get('row'), request.GET.get('track')
    selected = None
    if selected_row in names:
        listed = [a for k, a in keyed if k == selected_row]
        if selected_track in tracks:
            listed = [a for a in listed if a.tech_track == selected_track]
        for a in listed:
            a.stage = _STAGES[viewer].get(a.status, a.status)
        selected = {'name': names[selected_row], 'track': selected_track if selected_track in tracks else None,
                    'applications': listed}

    return {
        'tracks': tracks,
        'rows': rows,
        'totals': [{'track': t, **_grid_counts([a for a in applications if a.tech_track == t])} for t in tracks],
        'grand_total': _grid_counts(applications),
        'selected': selected,
        'selected_row': selected_row,
        'selected_track': selected_track,
        'minimum': minimum,
        'below_min_count': sum(c['below_min'] for r in rows for c in r['cells']),
    }


def _district_of(application):
    """Groups districts by their official PPD name, case-insensitively ("ppd kulai" is "PPD Kulai")."""
    district = ranking.canonical_district(application.state, application.district)
    return district.lower(), district or 'Not stated'


def _ppd_rows(state):
    """Every district of `state` as fixed grid rows, so districts nobody applied from still show (with 0)."""
    return [(d.lower(), d) for d in PPD_BY_STATE.get(state, [])]


@require_http_methods(["GET"])
@state_officer_required
def state_by_district(request):
    """State Officer: a district x technology-track grid of how many applications came in."""
    state = request.user.officerprofile.state
    applications = list(ranking.this_years_applications().filter(state=state).order_by('full_name'))
    context = _application_grid(request, applications, _district_of, 'state_officer', fixed_rows=_ppd_rows(state),
                                minimum=_grid_minimum(state))
    context.update({
        'title': f'Applications by District — {state}',
        'intro': 'How many teachers have applied in each district, per technology track. Click a number to see who applied.',
        'row_label': 'District',
        'empty_message': f'No teachers from {state} have applied yet.',
        'legend_by': 'you',
        'page_url': reverse('state_by_district'),
    })
    return render(request, 'application_grid.html', context)


@require_http_methods(["GET"])
@moe_officer_required
def moe_by_district(request):
    """MoE Officer: the same grid nationwide — states as rows, or one state's districts via ?state=."""
    state = request.GET.get('state', '')
    if state not in MALAYSIA_STATES:
        state = ''
    applications = ranking.this_years_applications().order_by('full_name')
    if state:
        applications = list(applications.filter(state=state))
        context = _application_grid(request, applications, _district_of, 'moe_officer', fixed_rows=_ppd_rows(state),
                                    minimum=_grid_minimum(state))
        title, row_label, empty = f'Applications by District — {state}', 'District', f'No teachers from {state} have applied yet.'
    else:
        applications = list(applications)
        context = _application_grid(
            request, applications, lambda a: (a.state, a.state), 'moe_officer',
            fixed_rows=[(s, s) for s in MALAYSIA_STATES],
        )
        title, row_label, empty = 'Applications by State', 'State', 'No applications yet.'
    context.update({
        'title': title,
        'intro': 'How many teachers have applied, per technology track. Pick a state to see its districts; click a number to see who applied.',
        'row_label': row_label,
        'empty_message': empty,
        'legend_by': 'the State Officer',
        'page_url': reverse('moe_by_district'),
        'states': MALAYSIA_STATES,
        'selected_state': state,
        'drill_into_states': not state,
    })
    return render(request, 'application_grid.html', context)


def _state_decided_applications(state):
    """Applications the State Officer has approved or declined, including ones the MoE has since decided."""
    return Application.objects.filter(state=state, state_decision_at__isnull=False).exclude(status='Compiled')


@require_http_methods(["GET"])
@state_officer_required
def state_decisions(request):
    """State Officer: the applications already approved or declined, filterable by decision."""
    state = request.user.officerprofile.state
    decided = _state_decided_applications(state)
    approved = decided.exclude(status='State Declined')  # State Approved, plus MoE Approved/Rejected after it
    declined = decided.filter(status='State Declined')

    show = request.GET.get('show', 'all')
    applications = {'approved': approved, 'declined': declined}.get(show, decided)

    context = {
        'applications': applications.order_by('-state_decision_at'),
        'state': state,
        'show': show if show in ('approved', 'declined') else 'all',
        'counts': {'all': decided.count(), 'approved': approved.count(), 'declined': declined.count()},
        'editable_statuses': ranking.COMPILED_PIPELINE_STATUSES,
    }
    return render(request, 'state_decisions.html', context)


@require_http_methods(["GET", "POST"])
@moe_officer_required
def moe_review(request):
    """MoE Officer: final approve/decline of state-approved candidates; triggers the recognition letter."""
    if request.method == 'POST' and request.POST.get('bulk'):
        chosen = Application.objects.filter(
            id__in=request.POST.getlist('application_ids'), status='State Approved').order_by('state', 'rank_in_state')
        _bulk_decide(request, chosen, request.POST.get('bulk'),
                     lambda a, d: _moe_decide(a, d, request.user, request.POST.get('remarks', ''),
                                              request.POST.get('award_category', '')))
        return redirect(f"{reverse('moe_review')}?{request.POST.get('filters', '')}")

    if request.method == 'POST':
        application = get_object_or_404(Application, id=request.POST.get('application_id'), status='State Approved')
        problem = _moe_decide(application, request.POST.get('decision'), request.user,
                              request.POST.get('remarks', ''), request.POST.get('award_category', ''))
        if problem:
            messages.error(request, problem)
            return redirect(_moe_back(request, application))

        if application.status == 'Approved':
            messages.success(request, f'✅ {application.full_name} is now a recognised GPGD ({application.recognized_year}). '
                                      f'Their Letter of Recognition is being emailed to {application.email}. '
                                      f'See them under Recognised & Rejected.')
        else:
            messages.success(request, f'{application.full_name} ({application.reference_number}) was rejected.')
        return redirect(_moe_back(request, application))

    # State-approved candidates, grouped by state, then district, then technology track, like State Review.
    state_filter = request.GET.get('state', '')
    district_filter, track_filter = request.GET.get('district', ''), request.GET.get('track', '')
    moe_approved = {}
    for application in ranking.this_years_applications().filter(status='Approved'):
        district, track, _ = ranking.quota_group(application)
        key = (application.state, district, track)
        moe_approved[key] = moe_approved.get(key, 0) + 1

    grouped, waiting_by_state, waiting_by_district = {}, {}, {}
    for application in Application.objects.filter(status='State Approved').order_by('rank_in_state'):
        district, track, minimum = ranking.quota_group(application)
        grouped.setdefault((application.state, district, track, minimum), []).append(application)
        waiting_by_state[application.state] = waiting_by_state.get(application.state, 0) + 1
        if application.state == state_filter:
            waiting_by_district[district] = waiting_by_district.get(district, 0) + 1

    groups = []
    for (state, district, track, minimum), group in sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1] or '', kv[0][2])):
        if ((state_filter and state != state_filter) or (district_filter and (district or '') != district_filter)
                or (track_filter and track != track_filter)):
            continue
        approved = moe_approved.get((state, district, track), 0)
        groups.append({
            'state': state, 'district': district, 'track': track, 'applications': group,
            'anchor': _group_anchor(f'{state} {district or ""}', track), 'recommended': sum(a.is_recommended for a in group),
            'need': minimum, 'approved': approved, 'remaining': max(minimum - approved, 0),
            'percent': min(100, round(100 * approved / minimum)) if minimum else 100,
        })

    deadline = ProgrammeSettings.load().submission_deadline
    context = {
        'groups': groups,
        'recommended_shown': sum(g['recommended'] for g in groups),
        'waiting_total': sum(waiting_by_state.values()),
        'state_choices': [(s, waiting_by_state.get(s, 0)) for s in MALAYSIA_STATES],
        'district_choices': sorted(waiting_by_district.items(), key=lambda kv: kv[0] or ''),
        'tracks': [t for t, _ in Application.TRACK_CHOICES],
        'state_filter': state_filter,
        'district_filter': district_filter,
        'track_filter': track_filter,
        'filters': request.GET.urlencode(),
        'pending_compile_count': Application.objects.filter(status__in=ranking.PIPELINE_ENTRY_STATUSES).count(),
        'compile_block_reason': pipeline_ops.compile_block_reason(deadline),
    }
    return render(request, 'moe_review.html', context)


def _send_recognition_letter(application):
    programme_settings = ProgrammeSettings.load()
    subject = programme_settings.recognition_letter_subject or content_defaults.DEFAULT_RECOGNITION_SUBJECT
    body_template = programme_settings.recognition_letter_body or content_defaults.DEFAULT_RECOGNITION_BODY
    body = content_defaults.render_placeholders(
        body_template, full_name=application.full_name, reference_number=application.reference_number
    )
    # Record the outcome on the application, so the MoE can see whether each letter went out.
    sent = Application.objects.filter(pk=application.pk)
    sent.update(recognition_letter_sent_at=None, recognition_letter_error='')
    pipeline_ops.send_single_email_async(
        subject, body, application.email,
        context_label=f"Recognition letter — {application.full_name} <{application.email}> ({application.reference_number})",
        on_sent=lambda: sent.update(recognition_letter_sent_at=timezone.now(), recognition_letter_error=''),
        on_failed=lambda error: sent.update(recognition_letter_error=error),
    )


# ---------------------------------------------------------------------------
# GPGD Recognition & Activity Dashboard
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@login_required
def recognition_dashboard(request):
    """Page 1 — nationwide view of all recognized GPGDs: filters, distribution charts, map, search."""
    recognized = Application.objects.filter(status='Approved')

    tech_track = request.GET.get('tech_track', '')
    state = request.GET.get('state', '')
    district = request.GET.get('district', '')
    school = request.GET.get('school', '')
    year = request.GET.get('year', '')
    category = request.GET.get('category', '')
    search = request.GET.get('q', '').strip()

    if tech_track:
        recognized = recognized.filter(tech_track=tech_track)
    if state:
        recognized = recognized.filter(state=state)
    if district:
        recognized = recognized.filter(district=district)
    if school:
        recognized = recognized.filter(school_name=school)
    if year:
        recognized = recognized.filter(recognized_year=year)
    if category:
        recognized = recognized.filter(award_category=category)

    if request.GET.get('download'):
        return _excel_response(pd.DataFrame([{
            'Recognised': a.recognized_year, 'Name': a.full_name, 'IC Number': a.ic_number, 'Reference': a.reference_number,
            'Email': a.email, 'WhatsApp': a.whatsapp_number, 'State': a.state, 'District': a.district,
            'School': a.school_name, "School Leader": a.school_leader_name, "School Leader's Email": a.school_leader_email,
            'Track': a.tech_track, 'Award Category': a.award_category, 'Activities Reported': a.activity_reports.count(),
        } for a in recognized.order_by('-recognized_year', 'state', 'full_name')]),
            f"GPGD_Recognised_{year or 'all_years'}.xlsx", 'GPGDs')

    search_result = None
    if search:
        search_result = recognized.filter(
            Q(full_name__icontains=search) | Q(ic_number__icontains=search)
        ).first()

    by_tech_track = list(recognized.values('tech_track').annotate(count=Count('id')).order_by('-count'))
    by_state = list(recognized.values('state').annotate(count=Count('id')).order_by('-count'))
    by_district = list(recognized.exclude(district='').values('district').annotate(count=Count('id')).order_by('-count')[:15])
    by_school = list(recognized.values('school_name').annotate(count=Count('id')).order_by('-count')[:15])
    state_counts = {row['state']: row['count'] for row in by_state}

    years = list(
        Application.objects.filter(status='Approved', recognized_year__isnull=False)
        .values_list('recognized_year', flat=True).distinct().order_by('-recognized_year')
    )
    categories = list(
        Application.objects.filter(status='Approved').exclude(award_category='')
        .values_list('award_category', flat=True).distinct().order_by('award_category')
    )
    districts = list(
        Application.objects.filter(status='Approved').exclude(district='')
        .values_list('district', flat=True).distinct().order_by('district')
    )
    schools = list(
        Application.objects.filter(status='Approved')
        .values_list('school_name', flat=True).distinct().order_by('school_name')
    )

    paginator = Paginator(recognized.order_by('state', 'full_name'), 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'total_recognized': recognized.count(),
        'by_tech_track': by_tech_track,
        'by_state': by_state,
        'by_district': by_district,
        'by_school': by_school,
        'by_tech_track_json': json.dumps(by_tech_track),
        'by_state_json': json.dumps(by_state),
        'by_district_json': json.dumps(by_district),
        'by_school_json': json.dumps(by_school),
        'state_counts_json': json.dumps(state_counts),
        'malaysia_states': MALAYSIA_STATES,
        'tech_tracks': [choice[0] for choice in Application.TRACK_CHOICES],
        'years': years,
        'categories': categories,
        'districts': districts,
        'schools': schools,
        'page_obj': page_obj,
        'search': search,
        'search_result': search_result,
        'filters': {
            'tech_track': tech_track, 'state': state, 'district': district,
            'school': school, 'year': year, 'category': category,
        },
    }
    return render(request, 'recognition_dashboard.html', context)


@require_http_methods(["GET"])
@login_required
def activity_dashboard(request):
    """Page 2 — professional development activity/training reports from every recognized GPGD."""
    reports = ActivityReport.objects.select_related('gpgd')

    # One recognition year (group of GPGDs) at a time: the current one unless another year is picked.
    years = list(Application.objects.filter(status='Approved', recognized_year__isnull=False)
                 .values_list('recognized_year', flat=True).distinct().order_by('-recognized_year'))
    year = request.GET.get('year', '')
    year = int(year) if year.isdigit() and int(year) in years else pipeline_ops.current_cohort_year()
    reports = reports.filter(gpgd__recognized_year=year)

    tech_track = request.GET.get('tech_track', '')
    state = request.GET.get('state', '')
    district = request.GET.get('district', '')
    month = request.GET.get('month', '')  # 'YYYY-MM'
    search = request.GET.get('q', '').strip()

    if tech_track:
        reports = reports.filter(gpgd__tech_track=tech_track)
    if state:
        reports = reports.filter(gpgd__state=state)
    if district:
        reports = reports.filter(gpgd__district=district)
    if month:
        try:
            year_str, month_str = month.split('-')
            reports = reports.filter(training_date__year=int(year_str), training_date__month=int(month_str))
        except ValueError:
            month = ''
    if search:
        reports = reports.filter(Q(gpgd__full_name__icontains=search) | Q(training_title__icontains=search))

    total_trainings = reports.count()
    total_hours = reports.aggregate(total=Sum('hours'))['total'] or 0

    counts = reports.aggregate(teachers=Sum('num_teachers'), students=Sum('num_students'))
    total_teachers_trained = counts['teachers'] or 0
    total_students_trained = counts['students'] or 0

    if request.GET.get('download'):
        return _excel_response(pd.DataFrame([{
            'Recognised': r.gpgd.recognized_year, 'GPGD': r.gpgd.full_name, 'IC Number': r.gpgd.ic_number,
            'State': r.gpgd.state, 'District': r.gpgd.district, 'School': r.gpgd.school_name,
            'Track': r.gpgd.tech_track, 'Activity': r.training_title, 'Date': r.training_date,
            'Hours': float(r.hours), 'Mode': r.get_training_mode_display(), 'Venue / Platform': r.venue_platform,
            'Teachers': r.num_teachers, 'Students': r.num_students, 'School Leaders': r.num_school_leaders,
            'Others': r.num_others, 'Total': r.num_participants, 'Description': r.description,
        } for r in reports.order_by('training_date', 'gpgd__full_name')]),
            f'GPGD_Activity_Reports_{year or "all"}.xlsx', 'Activities')

    by_month = list(
        reports.annotate(month=TruncMonth('training_date'))
        .values('month').annotate(count=Count('id')).order_by('month')
    )
    by_state = list(reports.values('gpgd__state').annotate(count=Count('id')).order_by('-count'))
    by_tech_track = list(reports.values('gpgd__tech_track').annotate(count=Count('id')).order_by('-count'))

    top_active = (
        Application.objects.filter(status='Approved', recognized_year=year)
        .annotate(report_count=Count('activity_reports'))
        .filter(report_count__gt=0)
        .order_by('-report_count')[:10]
    )

    recent_activities = reports.order_by('-training_date', '-submitted_at')[:20]

    context = {
        'total_trainings': total_trainings,
        'total_hours': total_hours,
        'total_teachers_trained': total_teachers_trained,
        'total_students_trained': total_students_trained,
        'by_month_json': json.dumps([{'month': row['month'].strftime('%Y-%m'), 'count': row['count']} for row in by_month]),
        'by_state': by_state,
        'by_tech_track': by_tech_track,
        'by_state_json': json.dumps(by_state),
        'by_tech_track_json': json.dumps(by_tech_track),
        'top_active': top_active,
        'recent_activities': recent_activities,
        'tech_tracks': [choice[0] for choice in Application.TRACK_CHOICES],
        'malaysia_states': MALAYSIA_STATES,
        'filters': {'tech_track': tech_track, 'state': state, 'district': district, 'month': month, 'q': search},
        'years': years,
        'year': year,
        'current_year': pipeline_ops.current_cohort_year(),
        'download_query': request.GET.urlencode(),
    }
    return render(request, 'activity_dashboard.html', context)


@require_http_methods(["GET", "POST"])
def submit_activity_report(request, token):
    """Public monthly activity reporting form — reached via the signed link emailed to recognized GPGDs."""
    reference_number = pipeline_ops.read_report_token(token)
    if reference_number is None:
        return render(request, 'apply_invalid.html', status=400)

    application = get_object_or_404(Application, reference_number=reference_number, status='Approved')

    if request.method == 'POST':
        form = ActivityReportForm(request.POST, request.FILES)
        if form.is_valid():
            report = form.save(commit=False)
            report.gpgd = application
            report.save()
            return redirect('activity_report_success', report_id=report.id)
    else:
        form = ActivityReportForm()

    return render(request, 'activity_report_form.html', {'form': form, 'application': application, 'token': token})


@require_http_methods(["GET"])
def activity_report_success(request, report_id):
    report = get_object_or_404(ActivityReport, id=report_id)
    return render(request, 'activity_report_success.html', {'report': report})


def _send_monthly_reminders_background(triggered_by):
    result = pipeline_ops.send_monthly_report_reminders()
    failed = result['failed']
    status = 'error' if failed else 'success'

    summary = (
        f"Sent {result['sent']} monthly reminder(s) for {result['period']} out of {result['recognized_total']} "
        f"recognized GPGD(s); {result['skipped_already_sent']} already reminded this period; "
        f"{result['no_email']} skipped (no email on file)."
    )

    error_message = ''
    if failed:
        error_message = summary + ' First failure(s): '
        error_message += '; '.join(f"{f['application']} <{f['email']}>: {f['error']}" for f in failed[:5])
        if len(failed) > 5:
            error_message += f'; and {len(failed) - 5} more (see server logs)'

    AgentActivityLog.objects.create(
        trigger_reason=f'Manual: Send Monthly Reminders (by {triggered_by})',
        summary=summary,
        actions_taken=[{'tool': 'send_monthly_report_reminders', 'input': {}, 'result': result}],
        status=status,
        error_message=error_message,
    )


@require_http_methods(["POST"])
@login_required
def send_monthly_reminders(request):
    """Manual override: normally this fires automatically on the 1st of every month via the scheduled command."""
    threading.Thread(
        target=_send_monthly_reminders_background,
        args=(request.user.get_username(),),
        daemon=True,
    ).start()
    messages.info(
        request,
        '📨 Sending monthly activity report reminders now — this runs in the background. Check the '
        '"Agent Activity" panel in a few moments for confirmation of exactly how many were sent, skipped, or failed.'
    )
    return redirect('activity_dashboard')


# ---------------------------------------------------------------------------
# Monthly reminder, report recipients and monthly statistics reports
# ---------------------------------------------------------------------------

def _excel_response(dataframe, filename, sheet='Sheet1'):
    """An .xlsx download of `dataframe`."""
    buffer = io.BytesIO()
    dataframe.to_excel(buffer, index=False, sheet_name=sheet)
    buffer.seek(0)
    response = FileResponse(buffer, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@require_http_methods(["GET", "POST"])
@login_required
def edit_monthly_reminder(request):
    """Edit the monthly reminder sent to GPGDs, and the days the reminder and the leaders' reports go out."""
    settings_obj = ProgrammeSettings.load()

    if request.method == 'POST':
        days = {}
        for field in ('reminder_day', 'report_day'):
            raw = request.POST.get(field, '').strip()
            if not raw.isdigit() or not 1 <= int(raw) <= 28:
                messages.error(request, 'Days must be a number from 1 to 28 (every month has those days).')
                return redirect('edit_monthly_reminder')
            days[field] = int(raw)
        settings_obj.monthly_reminder_subject = request.POST.get('subject', '').strip()
        settings_obj.monthly_reminder_body = request.POST.get('body', '').strip()
        settings_obj.reminder_day, settings_obj.report_day = days['reminder_day'], days['report_day']
        settings_obj.save()
        messages.success(request, '✅ Monthly reminder saved.')
        return redirect('edit_monthly_reminder')

    context = {
        'subject': settings_obj.monthly_reminder_subject or content_defaults.DEFAULT_MONTHLY_REMINDER_SUBJECT,
        'body': settings_obj.monthly_reminder_body or content_defaults.DEFAULT_MONTHLY_REMINDER_BODY,
        'placeholders': [f'{{{{{p}}}}}' for p in content_defaults.MONTHLY_REMINDER_PLACEHOLDERS],
        'is_default': not settings_obj.monthly_reminder_subject and not settings_obj.monthly_reminder_body,
        'reminder_day': settings_obj.reminder_day,
        'report_day': settings_obj.report_day,
        'cohort_year': pipeline_ops.current_cohort_year(),
        'gpgd_count': pipeline_ops.current_gpgds().count(),
    }
    return render(request, 'edit_monthly_reminder.html', context)


def _recipient_rows(state):
    """(level, state, district, label) for the recipient rows shown: the BSTP Director and every State
    Director, or with `state`, that state's District Education Leads."""
    if state:
        return [('district', state, d, d) for d in PPD_BY_STATE[state]]
    return [('bstp', '', '', 'BSTP Director')] + [('state', s, '', f'State Director, {s}') for s in MALAYSIA_STATES]


@require_http_methods(["GET", "POST"])
@login_required
def report_recipients(request):
    """Who receives the monthly statistics: BSTP Director, State Directors, District Education Leads."""
    state = request.GET.get('state') or request.POST.get('state') or ''
    if not PPD_BY_STATE.get(state):
        state = ''

    if request.method == 'POST' and request.FILES.get('upload'):
        return _upload_recipients(request)

    if request.method == 'POST':
        errors = []
        for i, (level, row_state, district, label) in enumerate(_recipient_rows(state)):
            name = request.POST.get(f'name_{i}', '').strip()
            email = request.POST.get(f'email_{i}', '').strip()
            where = dict(level=level, state=row_state, district=district)
            if not email:
                ReportRecipient.objects.filter(**where).delete()
            elif '@' not in email or ' ' in email:
                errors.append(f'{label}: "{email}" is not an email address')
            else:
                ReportRecipient.objects.update_or_create(**where, defaults={'name': name, 'email': email})
        if errors:
            messages.error(request, 'Not saved: ' + '; '.join(errors) + '. The other rows were saved.')
        else:
            messages.success(request, '✅ Recipients saved.')
        return redirect(f"{reverse('report_recipients')}?state={state}" if state else 'report_recipients')

    existing = {(r.level, r.state, r.district): r for r in ReportRecipient.objects.all()}

    if request.GET.get('download'):
        rows = _recipient_rows('') + [row for s in MALAYSIA_STATES if PPD_BY_STATE.get(s) for row in _recipient_rows(s)]
        level_names = dict(ReportRecipient.LEVEL_CHOICES)
        data = [{'Level': level_names[level], 'State': row_state, 'District': district,
                 'Name': getattr(existing.get((level, row_state, district)), 'name', ''),
                 'Email': getattr(existing.get((level, row_state, district)), 'email', '')}
                for level, row_state, district, _ in rows]
        return _excel_response(pd.DataFrame(data), 'GPGD_Report_Recipients.xlsx', 'Recipients')

    rows = [{'index': i, 'label': label, 'recipient': existing.get((level, row_state, district)), 'state': row_state,
             'has_districts': level == 'state' and bool(PPD_BY_STATE.get(row_state)),
             'district_count': sum(1 for key in existing if key[0] == 'district' and key[1] == row_state),
             'district_total': len(PPD_BY_STATE.get(row_state, []))}
            for i, (level, row_state, district, label) in enumerate(_recipient_rows(state))]
    return render(request, 'report_recipients.html', {'rows': rows, 'state': state})


def _upload_recipients(request):
    """Fills recipients from an Excel file with columns Level, State, District, Name, Email (the same
    layout as the download). Blank emails are skipped; nothing already saved is removed."""
    levels = {label.lower(): level for level, label in ReportRecipient.LEVEL_CHOICES}
    try:
        df = pd.read_excel(request.FILES['upload']).fillna('')
    except Exception as e:
        messages.error(request, f'Could not read that file as Excel: {e}')
        return redirect('report_recipients')
    saved, problems = 0, []
    for number, row in enumerate(df.to_dict('records'), start=2):
        email = str(row.get('Email', '')).strip()
        if not email:
            continue
        level = levels.get(str(row.get('Level', '')).strip().lower())
        state = canonical_state(row.get('State', '')) if level in ('state', 'district') else ''
        district = ranking.canonical_district(state, str(row.get('District', ''))) if level == 'district' else ''
        if (not level or (level != 'bstp' and not state) or '@' not in email
                or (level == 'district' and district not in PPD_BY_STATE.get(state, []))):
            problems.append(f'row {number}')
            continue
        ReportRecipient.objects.update_or_create(
            level=level, state=state, district=district,
            defaults={'name': str(row.get('Name', '')).strip(), 'email': email})
        saved += 1
    messages.success(request, f'✅ {saved} recipient(s) saved from the file.')
    if problems:
        messages.warning(request, f"Skipped {len(problems)} row(s) with an unknown level, state, district or email: "
                                  + ', '.join(problems[:20]) + ('…' if len(problems) > 20 else ''))
    return redirect('report_recipients')


def _send_monthly_reports_background(period, triggered_by):
    result = monthly_reports.send_monthly_reports(period)
    AgentActivityLog.objects.create(
        trigger_reason=f'Monthly statistics sent manually (by {triggered_by})',
        summary=f"Monthly statistics for {pipeline_ops.month_label(period)}: {result['sent']} sent, "
                f"{result['already_sent']} already sent earlier, {len(result['failed'])} failed.",
        actions_taken=[{'tool': 'send_monthly_reports', 'input': {'period': period}, 'result': result}],
        status='error' if result['failed'] else 'success',
    )


@require_http_methods(["GET", "POST"])
@login_required
def monthly_reports_page(request):
    """Preview and send the monthly training statistics for one month (default: last month)."""
    period = request.GET.get('period') or request.POST.get('period') or monthly_reports.previous_period()
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
        period = monthly_reports.previous_period()

    if request.method == 'POST':
        threading.Thread(target=_send_monthly_reports_background,
                         args=(period, request.user.get_username()), daemon=True).start()
        messages.success(request, f"📨 Sending the {pipeline_ops.month_label(period)} reports now, in the background. "
                                  "Anyone who already received this month's report is skipped.")
        return redirect(f"{reverse('monthly_reports')}?period={period}")

    emails = monthly_reports.build_reports(period)
    logs = {(log.level, log.scope, log.email): log for log in MonthlyReportLog.objects.filter(period=period)}
    for email in emails:
        email['log'] = logs.get((email['level'], email['scope'][:255], email['email']))
    try:
        chosen = int(request.GET.get('preview', 0))
    except ValueError:
        chosen = 0
    gpgds, reports = monthly_reports.collect(period)
    programme = ProgrammeSettings.load()
    this_month = timezone.localdate().replace(day=1)
    periods = []
    for _ in range(13):  # this month and the 12 before it
        periods.append(this_month.strftime('%Y-%m'))
        this_month = (this_month - timedelta(days=1)).replace(day=1)
    context = {
        'period': period,
        'month': pipeline_ops.month_label(period),
        'periods': [(p, pipeline_ops.month_label(p)) for p in periods],
        'emails': emails,
        'preview': emails[chosen] if 0 <= chosen < len(emails) else None,
        'chosen': chosen,
        'totals': monthly_reports._totals(reports, gpgds),
        'missing': monthly_reports.missing_recipients(),
        'cohort_year': pipeline_ops.current_cohort_year(),
        'report_day': programme.report_day,
    }
    return render(request, 'monthly_reports.html', context)
