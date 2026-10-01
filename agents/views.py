import json
import os
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

from . import ai_agent, content_defaults, pipeline_ops, ranking
from .collector import highlight_missing_values, normalize_dataframe, save_normalized_file, validate_file
from .constants import MALAYSIA_STATES, canonical_state
from .decorators import admin_required, moe_officer_required, state_officer_required
from .forms import ActivityReportForm, ApplicationForm, CertificationFileUploadForm, CertificationRuleForm, ProviderForm, TeacherForm
from .models import (
    ActivityReport, AgentActivityLog, Application, CertificationRule, ErrorLog, FileUpload,
    InvitationExclusion, InvitationRecord, MonthlyReminderLog, ProgrammeSettings, Provider, Teacher,
)

TEMP_DIR = os.path.join(settings.BASE_DIR, 'temp_uploads')
os.makedirs(TEMP_DIR, exist_ok=True)

PROVIDERS = ['Google', 'Microsoft', 'Apple']


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
    eligible = teachers.filter(eligibility_status='Eligible').count()
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
        'submission_deadline': deadline,
        'deadline_passed': bool(deadline) and timezone.localdate() > deadline,
        'invitation_block_reason': pipeline_ops.invitation_block_reason(deadline),
        'awaiting_invitation': awaiting_invitation,
        'invite_pending_count': invite_pending_count,
        'invite_sending_count': invite_sending_count,
        'invite_sent_count': sum(1 for t in awaiting_invitation if t.invitation and t.invitation.status == 'sent'),
        'invitation_exclusions': InvitationExclusion.objects.all(),
        'pending_compile_count': Application.objects.filter(
            status__in=ranking.PIPELINE_ENTRY_STATUSES
        ).count(),
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
    if teacher:
        initial = {
            'full_name':   teacher.full_name,
            'ic_number':   teacher.ic_number,
            'email':       teacher.email if teacher.email.lower() != 'nan' else '',
            'school_name': teacher.school,
            'state':       canonical_state(teacher.state),
            'tech_track':  teacher.provider.split(',')[0].strip() if teacher.provider else '',
        }

    if request.method == 'POST':
        form = ApplicationForm(request.POST)
        if form.is_valid():
            application = form.save(commit=False)
            if teacher:
                application.teacher = teacher
                teacher.eligibility_status = 'Application Submitted'
                teacher.save()
            application.save()

            # Send acknowledgement email
            _send_acknowledgement(application)

            ai_agent.run_pipeline_agent_async(
                f"Teacher application submitted (reference {application.reference_number})."
            )

            return redirect('apply_success', ref=application.reference_number)
    else:
        form = ApplicationForm(initial=initial)

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
@moe_officer_required
def compile_applications(request):
    """Score, rank, and quota-flag every pending application, ready for State Officer review."""
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
        lines = [
            f"{s['state']}{' / ' + s['district'] if s['district'] else ''} ({s['tech_track']}): {s['have']}/{s['need']}"
            for s in shortfalls
        ]
        messages.warning(request, f"⚠️ {len(shortfalls)} quota shortfall(s) — not enough applicants: " + '; '.join(lines))

    return redirect('moe_review')


@require_http_methods(["GET", "POST"])
@state_officer_required
def state_review(request):
    """State Officer: approve/decline the compiled, ranked shortlist for their own state."""
    profile = request.user.officerprofile

    if request.method == 'POST':
        application = get_object_or_404(Application, id=request.POST.get('application_id'), state=profile.state)
        decision = request.POST.get('decision')
        if decision == 'approve':
            application.status = 'State Approved'
        elif decision == 'decline':
            application.status = 'State Declined'
        else:
            messages.error(request, 'Invalid decision.')
            return redirect('state_review')

        application.state_decision_by = request.user
        application.state_decision_at = timezone.now()
        application.state_remarks = request.POST.get('remarks', '').strip()
        application.save()
        messages.success(request, f'✅ {application.full_name} ({application.reference_number}) marked as "{application.status}".')
        return redirect('state_review')

    applications = Application.objects.filter(
        state=profile.state,
        status__in=ranking.COMPILED_PIPELINE_STATUSES,
    ).order_by('rank_in_state')

    context = {
        'applications': applications,
        'state': profile.state,
        'shortfalls': ranking.shortfalls_for_state(profile.state),
    }
    return render(request, 'state_review.html', context)


@require_http_methods(["GET", "POST"])
@moe_officer_required
def moe_review(request):
    """MoE Officer: final approve/decline of state-approved candidates; triggers the recognition letter."""
    if request.method == 'POST':
        application = get_object_or_404(Application, id=request.POST.get('application_id'), status='State Approved')
        decision = request.POST.get('decision')
        if decision == 'approve':
            application.status = 'Approved'
            application.recognized_year = timezone.now().year
            application.award_category = request.POST.get('award_category', '').strip()
        elif decision == 'decline':
            application.status = 'Rejected'
        else:
            messages.error(request, 'Invalid decision.')
            return redirect('moe_review')

        application.moe_decision_by = request.user
        application.moe_decision_at = timezone.now()
        application.moe_remarks = request.POST.get('remarks', '').strip()
        application.save()

        if application.status == 'Approved':
            _send_recognition_letter(application)

        messages.success(request, f'✅ {application.full_name} ({application.reference_number}) marked as "{application.status}".')
        return redirect('moe_review')

    applications = Application.objects.filter(status='State Approved').order_by('state', 'rank_in_state')
    deadline = ProgrammeSettings.load().submission_deadline
    context = {
        'applications': applications,
        'pending_compile_count': Application.objects.filter(status__in=ranking.PIPELINE_ENTRY_STATUSES).count(),
        'submission_deadline': deadline,
        'deadline_passed': bool(deadline) and timezone.localdate() > deadline,
    }
    return render(request, 'moe_review.html', context)


def _send_recognition_letter(application):
    programme_settings = ProgrammeSettings.load()
    subject = programme_settings.recognition_letter_subject or content_defaults.DEFAULT_RECOGNITION_SUBJECT
    body_template = programme_settings.recognition_letter_body or content_defaults.DEFAULT_RECOGNITION_BODY
    body = content_defaults.render_placeholders(
        body_template, full_name=application.full_name, reference_number=application.reference_number
    )
    pipeline_ops.send_single_email_async(
        subject, body, application.email,
        context_label=f"Recognition letter — {application.full_name} <{application.email}> ({application.reference_number})",
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

    # Best-effort: the form captures one participant total plus a multi-select audience,
    # not a per-audience breakdown, so a report targeting both teachers and students counts
    # toward both totals. Computed in Python rather than a JSONField `contains` lookup, which
    # isn't reliably supported across every backend (notably SQLite).
    total_teachers_trained = 0
    total_students_trained = 0
    for audience, n in reports.values_list('target_audience', 'num_participants'):
        audience = audience or []
        if 'teachers' in audience:
            total_teachers_trained += n
        if 'students' in audience:
            total_students_trained += n

    by_month = list(
        reports.annotate(month=TruncMonth('training_date'))
        .values('month').annotate(count=Count('id')).order_by('month')
    )
    by_state = list(reports.values('gpgd__state').annotate(count=Count('id')).order_by('-count'))
    by_tech_track = list(reports.values('gpgd__tech_track').annotate(count=Count('id')).order_by('-count'))

    top_active = (
        Application.objects.filter(status='Approved')
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
