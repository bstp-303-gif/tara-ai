from datetime import date, timedelta
from io import BytesIO, StringIO
from unittest import mock

import pandas as pd

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import ai_agent, monthly_reports, pipeline_ops, ranking
from .eligibility import eligible_tracks, save_to_database
from .forms import ActivityReportForm, ApplicationForm
from .models import (
    Application, CertificationRule, FileUpload, InvitationExclusion, InvitationRecord, OfficerProfile, ProgrammeSettings, Provider, Teacher,
    TrackLimit, ActivityReport, MonthlyReminderLog, ReportRecipient,
)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class InvitationApprovalGateTests(TestCase):
    """Invitations are sent only after the Admin approves them — never by the pipeline agent."""

    def setUp(self):
        settings_obj = ProgrammeSettings.load()
        settings_obj.submission_deadline = timezone.localdate() + timedelta(days=14)
        settings_obj.save()

    def _login(self, role=None):
        user = User.objects.create_user(username=f'user_{role}', password='test-pass-123')
        if role:
            OfficerProfile.objects.create(user=user, role=role)
        self.client.force_login(user)

    def test_pipeline_agent_has_no_invitation_tool(self):
        tool_names = {tool['name'] for tool in ai_agent.TOOLS}
        self.assertNotIn('send_invitation_emails', tool_names)
        self.assertNotIn('send_invitation_emails', ai_agent.TOOL_IMPLEMENTATIONS)

    @mock.patch('agents.views.threading.Thread')
    def test_moe_officer_cannot_send(self, thread):
        self._login('moe_officer')
        self.client.post(reverse('send_invitations'))
        thread.assert_not_called()

    @mock.patch('agents.views.threading.Thread')
    def test_state_officer_cannot_send(self, thread):
        self._login('state_officer')
        self.client.post(reverse('send_invitations'))
        thread.assert_not_called()

    @mock.patch('agents.views.threading.Thread')
    def test_admin_approval_sends(self, thread):
        self._login()
        response = self.client.post(reverse('send_invitations'))
        self.assertRedirects(response, reverse('agent1_dashboard'))
        thread.assert_called_once()

    def test_dashboard_shows_invitation_approval(self):
        self._login()
        response = self.client.get(reverse('agent1_dashboard'))
        self.assertContains(response, 'Approve &amp; Send Invitations')

    def _make_teacher(self, ic='900101-01-1234', name='Siti Aminah'):
        return Teacher.objects.create(
            ic_number=ic, full_name=name, email='siti@example.com', school='SK Taman', state='Selangor',
            provider='Google', cert_level='Level 1', cert_year=2025, eligibility_status='Eligible',
        )

    def test_admin_delete_removes_and_records_exclusion(self):
        self._login()
        teacher = self._make_teacher()
        response = self.client.post(reverse('remove_from_invitations'), {'teacher_ids': [teacher.id]})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Teacher.objects.filter(id=teacher.id).exists())
        self.assertTrue(InvitationExclusion.objects.filter(ic_number=teacher.ic_number).exists())

    def test_officer_cannot_delete(self):
        self._login('moe_officer')
        teacher = self._make_teacher()
        self.client.post(reverse('remove_from_invitations'), {'teacher_ids': [teacher.id]})
        self.assertTrue(Teacher.objects.filter(id=teacher.id).exists())

    def test_pipeline_rerun_keeps_removed_teacher_out(self):
        InvitationExclusion.objects.create(ic_number='900101-01-1234', full_name='Siti Aminah')
        rows = [
            {'ic': '900101-01-1234', 'name': 'Siti Aminah'},
            {'ic': '880202-02-5678', 'name': 'Ahmad Faiz'},
        ]
        df = pd.DataFrame([{
            **row, 'email': 'x@example.com', 'school': 'SK Taman', 'state': 'Selangor', 'provider': 'Google',
            'certification': 'Google Educator', 'cert_level': 'Level 1', 'cert_year': 2025,
            'multi_certified': False, 'eligibility_status': 'Eligible',
        } for row in rows])
        save_to_database(df)
        self.assertEqual(list(Teacher.objects.values_list('full_name', flat=True)), ['Ahmad Faiz'])

    def test_prune_forgets_exclusions_once_no_file_lists_them(self):
        InvitationExclusion.objects.create(ic_number='900101-01-1234', full_name='Siti Aminah')
        self.assertEqual(pipeline_ops.prune_stale_exclusions(), 1)
        self.assertFalse(InvitationExclusion.objects.exists())

    @mock.patch('agents.pipeline_ops.normalize_dataframe')
    @mock.patch('agents.pipeline_ops.pd.read_excel', return_value=pd.DataFrame())
    @mock.patch('agents.pipeline_ops.os.path.exists', return_value=True)
    def test_prune_keeps_exclusions_still_in_an_uploaded_file(self, _exists, _read, normalize):
        normalize.return_value = pd.DataFrame([{'ic': '900101-01-1234'}])
        FileUpload.objects.create(upload_id='abc', file_name='g.xlsx', provider='Google',
                                  file_path='g.xlsx', validation_status='valid')
        InvitationExclusion.objects.create(ic_number='900101-01-1234', full_name='Siti Aminah')
        InvitationExclusion.objects.create(ic_number='880202-02-5678', full_name='Ahmad Faiz')
        self.assertEqual(pipeline_ops.prune_stale_exclusions(), 1)
        self.assertEqual(list(InvitationExclusion.objects.values_list('full_name', flat=True)), ['Siti Aminah'])

    @mock.patch('agents.views.pipeline_ops.run_certification_pipeline', return_value={'ran': True})
    def test_restore_deletes_exclusion_and_rebuilds(self, pipeline):
        self._login()
        exclusion = InvitationExclusion.objects.create(ic_number='900101-01-1234', full_name='Siti Aminah')
        self.client.post(reverse('restore_to_invitations'), {'exclusion_ids': [exclusion.id]})
        self.assertFalse(InvitationExclusion.objects.exists())
        pipeline.assert_called_once()

    def test_admin_bulk_delete_selected(self):
        self._login()
        a = self._make_teacher('900101-01-1234', 'Siti Aminah')
        b = self._make_teacher('880202-02-5678', 'Ahmad Faiz')
        keep = self._make_teacher('770303-03-9012', 'Lim Wei')
        self.client.post(reverse('remove_from_invitations'), {'teacher_ids': [a.id, b.id]})
        self.assertEqual(list(Teacher.objects.values_list('full_name', flat=True)), [keep.full_name])
        self.assertEqual(InvitationExclusion.objects.count(), 2)

    @mock.patch('agents.pipeline_ops._send_mail_with_hard_timeout')
    def test_sent_invitation_is_recorded_and_not_resent(self, send):
        teacher = self._make_teacher()
        result = pipeline_ops.send_invitation_emails(sent_by='admin')
        self.assertEqual(result['sent'], 1)
        record = InvitationRecord.objects.get(ic_number=teacher.ic_number)
        self.assertEqual((record.status, record.sent_by), ('sent', 'admin'))

        result = pipeline_ops.send_invitation_emails(sent_by='admin')
        self.assertEqual(result['sent'], 0)
        self.assertEqual(send.call_count, 1)

    @mock.patch('agents.pipeline_ops._send_mail_with_hard_timeout', side_effect=OSError('SMTP down'))
    def test_failed_invitation_is_recorded(self, send):
        teacher = self._make_teacher()
        pipeline_ops.send_invitation_emails()
        record = InvitationRecord.objects.get(ic_number=teacher.ic_number)
        self.assertEqual(record.status, 'failed')
        self.assertIn('SMTP down', record.error_message)

    def test_dashboard_shows_email_sent_by_name(self):
        self._login()
        teacher = self._make_teacher()
        InvitationRecord.objects.create(ic_number=teacher.ic_number, email=teacher.email, status='sent')
        response = self.client.get(reverse('agent1_dashboard'))
        self.assertContains(response, 'Email sent')
        self.assertContains(response, '0 not yet sent')

    @mock.patch('agents.views.threading.Thread')
    def test_send_marks_teachers_sending_immediately(self, thread):
        self._login()
        teacher = self._make_teacher()
        self.client.post(reverse('send_invitations'))
        self.assertEqual(InvitationRecord.objects.get(ic_number=teacher.ic_number).status, 'sending')
        response = self.client.get(reverse('agent1_dashboard'))
        self.assertContains(response, 'Sending…')
        self.assertContains(response, 'location.reload()')

    def test_stale_sending_shows_not_sent(self):
        self._login()
        teacher = self._make_teacher()
        InvitationRecord.objects.create(ic_number=teacher.ic_number, email=teacher.email, status='sending')
        InvitationRecord.objects.filter(ic_number=teacher.ic_number).update(attempted_at=timezone.now() - timedelta(minutes=10))
        response = self.client.get(reverse('agent1_dashboard'))
        self.assertContains(response, 'Not sent yet')
        self.assertNotContains(response, 'location.reload()')


class TechTrackChoiceTests(TestCase):
    """A multi-certified teacher chooses between the tracks they hold recognised certifications for."""

    def setUp(self):
        for provider_name, cert in [('Apple', 'Apple Teacher (test)'), ('Google', 'Google Educator L2 (test)')]:
            provider, _ = Provider.objects.get_or_create(name=provider_name, defaults={'display_name': provider_name})
            CertificationRule.objects.create(provider=provider, canonical_name=cert, required_keywords=['x'])
        settings_obj = ProgrammeSettings.load()
        settings_obj.submission_deadline = timezone.localdate() + timedelta(days=14)
        settings_obj.save()

    def _teacher(self, certification, provider):
        return Teacher.objects.create(
            ic_number='850610-01-6124', full_name='Jayanthi', email='j@example.com', school='BSTP', state='Johor',
            provider=provider, certification=certification, cert_level='Level 2', cert_year=2026,
            eligibility_status='Eligible',
        )

    def _apply_url(self, teacher):
        return reverse('apply', args=[pipeline_ops._make_apply_token(teacher.ic_number)])

    def test_multi_certified_must_choose_between_their_tracks(self):
        teacher = self._teacher('Apple Teacher (test), Google Educator L2 (test)', 'Apple, Google')
        self.assertEqual(eligible_tracks(teacher), ['Apple', 'Google'])
        form = self.client.get(self._apply_url(teacher)).context['form']
        self.assertEqual([v for v, _ in form.fields['tech_track'].choices], ['', 'Apple', 'Google'])
        self.assertEqual(form.initial['tech_track'], '')
        self.assertIn('Apple Teacher (test)', form.initial['certifications'])
        self.assertIn('Google Educator L2 (test)', form.initial['certifications'])

    def test_single_track_is_preselected(self):
        teacher = self._teacher('Apple Teacher (test)', 'Apple, Google')
        form = self.client.get(self._apply_url(teacher)).context['form']
        self.assertEqual([v for v, _ in form.fields['tech_track'].choices], ['Apple'])
        self.assertEqual(form.initial['tech_track'], 'Apple')

    def test_track_outside_certifications_is_rejected(self):
        form = ApplicationForm(data={'tech_track': 'Microsoft'}, allowed_tracks=['Apple', 'Google'])
        form.is_valid()
        self.assertIn('tech_track', form.errors)


class StateDecisionsPageTests(TestCase):
    """State Officers see pending candidates on State Review and decided ones on Approved & Declined."""

    def setUp(self):
        user = User.objects.create_user(username='johor', password='test-pass-123')
        OfficerProfile.objects.create(user=user, role='state_officer', state='Johor')
        self.client.force_login(user)

    def _application(self, name, status, state='Johor', decided=True):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district='Johor Bahru', state=state, tech_track='Google',
            certifications='Google Educator', lnpt_current=90, lnpt_previous=90, lnpt_two_years=90,
            training_experience='Mentoring', status=status, rank_in_state=1,
            state_decision_at=timezone.now() if decided else None,
        )

    def test_pending_and_decided_are_split(self):
        self._application('Pending Person', 'Compiled', decided=False)
        self._application('Approved Person', 'State Approved')
        self._application('Declined Person', 'State Declined')
        self._application('Other State', 'State Approved', state='Perlis')

        review = self.client.get(reverse('state_review'))
        self.assertContains(review, 'Pending Person')
        self.assertNotContains(review, 'Approved Person')

        decisions = self.client.get(reverse('state_decisions'))
        self.assertContains(decisions, 'Approved Person')
        self.assertContains(decisions, 'Declined Person')
        self.assertNotContains(decisions, 'Pending Person')
        self.assertNotContains(decisions, 'Other State')

        declined_only = self.client.get(reverse('state_decisions'), {'show': 'declined'})
        self.assertContains(declined_only, 'Declined Person')
        self.assertNotContains(declined_only, 'Approved Person')

    def test_change_decision_returns_to_decisions_page(self):
        application = self._application('Declined Person', 'State Declined')
        response = self.client.post(reverse('state_review'), {
            'application_id': application.id, 'decision': 'approve', 'next': 'state_decisions',
        })
        self.assertRedirects(response, reverse('state_decisions'))
        application.refresh_from_db()
        self.assertEqual(application.status, 'State Approved')

    def test_decision_locked_after_moe_acts(self):
        application = self._application('Final Person', 'Approved')
        response = self.client.post(reverse('state_review'), {'application_id': application.id, 'decision': 'decline'})
        self.assertEqual(response.status_code, 404)
        application.refresh_from_db()
        self.assertEqual(application.status, 'Approved')


class PublicPagesHideOfficerMenuTests(TestCase):
    """Candidate-facing pages never show the officer/Admin sidebar, even in a browser logged in as Admin."""

    def test_apply_success_has_no_sidebar_even_for_admin(self):
        application = Application.objects.create(
            full_name='Siti Aminah', ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district='Johor Bahru', state='Johor', tech_track='Google',
            certifications='x', lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x',
            status='State Approved',
        )
        self.client.force_login(User.objects.create_user(username='admin_user', password='test-pass-123'))
        response = self.client.get(reverse('apply_success', args=[application.reference_number]))
        self.assertContains(response, application.reference_number)
        self.assertNotContains(response, 'class="sidebar"')
        # Only "submitted" + the reference number — no personal details or internal workflow status.
        for hidden in ['900101-01-1234', 'Siti Aminah', 't@example.com', 'SK Taman', application.status]:
            self.assertNotContains(response, hidden)

    def test_admin_pages_keep_sidebar(self):
        self.client.force_login(User.objects.create_user(username='admin_user', password='test-pass-123'))
        self.assertContains(self.client.get(reverse('manage_providers')), 'class="sidebar"')


class StateStatisticsPageTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username='johor', password='test-pass-123')
        OfficerProfile.objects.create(user=user, role='state_officer', state='Johor')
        self.client.force_login(user)

    def _application(self, status, district='PPD Kulai', track='Google', state='Johor'):
        return Application.objects.create(
            full_name='X', ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district=district, state=state, tech_track=track, certifications='x',
            lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x', status=status,
        )

    def test_counts_and_quota(self):
        self._application('State Approved', district='ppd kulai')  # same district, different case
        self._application('Approved')
        self._application('State Declined')
        self._application('Compiled', district='PPD Segamat')
        self._application('Application Submitted')
        self._application('State Approved', state='Perlis')  # other state ignored

        response = self.client.get(reverse('state_statistics'))
        self.assertEqual(response.status_code, 200)
        funnel = response.context['funnel']
        self.assertEqual(funnel['applied'], 5)
        self.assertEqual(funnel['approved'], 2)
        self.assertEqual(funnel['declined'], 1)
        self.assertEqual(funnel['awaiting_you'], 1)
        self.assertEqual(funnel['awaiting_compile'], 1)

        # Every Johor district (11 PPDs) x 3 tracks is listed, 2 places each, applicants or not.
        rows = {(r['district'], r['tech_track']): r for r in response.context['quota_rows']}
        self.assertEqual(len(rows), 33)
        self.assertEqual(rows[('PPD Kulai', 'Google')]['filled'], 2)
        self.assertEqual(rows[('PPD Kulai', 'Google')]['remaining'], 0)
        self.assertEqual(rows[('PPD Segamat', 'Google')]['remaining'], 2)
        self.assertEqual(rows[('PPD Tangkak', 'Apple')]['applied'], 0)
        self.assertEqual(response.context['quota_need'], 66)
        self.assertEqual(response.context['quota_filled'], 2)

    def test_past_years_gpgds_are_left_out(self):
        past = self._application('Approved')
        past.recognized_year = timezone.localdate().year - 1
        past.save()
        this_year = self._application('Approved')
        this_year.recognized_year = timezone.localdate().year
        this_year.save()

        response = self.client.get(reverse('state_statistics'))
        self.assertEqual(response.context['funnel']['applied'], 1)
        self.assertEqual(response.context['funnel']['moe_approved'], 1)

    def test_empty_state_renders(self):
        response = self.client.get(reverse('state_statistics'))
        self.assertContains(response, 'No applicants yet')  # every district shows, even with nobody applied


class StateByDistrictPageTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username='johor', password='test-pass-123')
        OfficerProfile.objects.create(user=user, role='state_officer', state='Johor')
        self.client.force_login(user)

    def _application(self, name, district, track, status='Compiled', state='Johor'):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district=district, state=state, tech_track=track, certifications='x',
            lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x', status=status,
        )

    def test_grid_counts_and_cell_list(self):
        self._application('Aina', 'PPD Kulai', 'Google', 'State Approved')
        self._application('Badrul', 'ppd kulai', 'Google', 'State Declined')
        self._application('Chong', 'PPD Kulai', 'Apple')
        self._application('Devi', 'PPD Segamat', 'Google', 'Application Submitted')
        self._application('Elsewhere', 'Kangar', 'Google', state='Perlis')

        response = self.client.get(reverse('state_by_district'))
        rows = {r['name']: r for r in response.context['rows']}
        self.assertEqual(len(rows), 11)  # every Johor PPD, including ones nobody applied from
        kulai_google = next(c for c in rows['PPD Kulai']['cells'] if c['track'] == 'Google')
        self.assertEqual((kulai_google['total'], kulai_google['approved'], kulai_google['declined']), (2, 1, 1))
        self.assertFalse(kulai_google['below_min'])
        self.assertTrue(next(c for c in rows['PPD Tangkak']['cells'] if c['track'] == 'Apple')['below_min'])
        self.assertEqual(rows['PPD Kulai']['total']['total'], 3)
        self.assertEqual(response.context['grand_total']['total'], 4)

        listed = self.client.get(reverse('state_by_district'), {'row': 'ppd kulai', 'track': 'Google'})
        self.assertContains(listed, 'Aina')
        self.assertContains(listed, 'Badrul')
        self.assertNotContains(listed, 'Chong')
        self.assertContains(listed, 'Declined by you')


class MoeByDistrictPageTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username='moe', password='test-pass-123')
        OfficerProfile.objects.create(user=user, role='moe_officer')
        self.client.force_login(user)

    def _application(self, name, state, district, track, status='Compiled'):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district=district, state=state, tech_track=track, certifications='x',
            lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x', status=status,
        )

    def test_all_states_lists_every_state(self):
        self._application('Aina', 'Johor', 'Kulai', 'Google', 'State Approved')
        self._application('Badrul', 'Perlis', 'Kangar', 'Apple')
        response = self.client.get(reverse('moe_by_district'))
        rows = {r['name']: r for r in response.context['rows']}
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows['Johor']['total']['total'], 1)
        self.assertEqual(rows['Selangor']['total']['total'], 0)
        self.assertEqual(response.context['grand_total']['total'], 2)

        listed = self.client.get(reverse('moe_by_district'), {'row': 'Johor'})
        self.assertContains(listed, 'Aina')
        self.assertContains(listed, 'Waiting for your final decision')
        self.assertNotContains(listed, 'Badrul')

    def test_one_state_shows_its_districts(self):
        self._application('Aina', 'Johor', 'PPD Kulai', 'Google')
        self._application('Chong', 'Johor', 'PPD Segamat', 'Apple')
        self._application('Badrul', 'Perlis', 'Perlis', 'Apple')
        response = self.client.get(reverse('moe_by_district'), {'state': 'Johor'})
        names = [r['name'] for r in response.context['rows']]
        self.assertEqual(len(names), 11)
        self.assertIn('PPD Tangkak', names)

        listed = self.client.get(reverse('moe_by_district'), {'state': 'Johor', 'row': 'ppd segamat'})
        self.assertContains(listed, 'Chong')
        self.assertNotContains(listed, 'Aina')

    def test_state_officer_cannot_open_it(self):
        self.client.force_login(User.objects.create_user(username='johor', password='test-pass-123'))
        OfficerProfile.objects.create(user=User.objects.get(username='johor'), role='state_officer', state='Johor')
        self.assertEqual(self.client.get(reverse('moe_by_district')).status_code, 302)


class CompileAfterDeadlineTests(TestCase):
    """Applications are compiled and ranked once, by the MoE Officer, after the submission deadline."""

    def setUp(self):
        user = User.objects.create_user(username='moe', password='test-pass-123')
        OfficerProfile.objects.create(user=user, role='moe_officer')
        self.client.force_login(user)

    def _set_deadline(self, days_from_today):
        settings_obj = ProgrammeSettings.load()
        settings_obj.submission_deadline = None if days_from_today is None else timezone.localdate() + timedelta(days=days_from_today)
        settings_obj.save()

    def _application(self, name, lnpt):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district='Kulai', state='Johor', tech_track='Google', certifications='x',
            lnpt_current=lnpt, lnpt_previous=lnpt, lnpt_two_years=lnpt, training_experience='x',
        )

    def test_blocked_while_applications_are_open(self):
        self._set_deadline(0)  # the deadline day itself is still open
        self._application('Aina', 90)
        response = self.client.post(reverse('compile_applications'), follow=True)
        self.assertContains(response, 'Applications are still open')
        self.assertEqual(Application.objects.get().status, 'Application Submitted')

    def test_blocked_without_a_deadline(self):
        self._set_deadline(None)
        self._application('Aina', 90)
        self.client.post(reverse('compile_applications'))
        self.assertEqual(Application.objects.get().status, 'Application Submitted')

    def test_ranks_everyone_together_after_the_deadline(self):
        self._set_deadline(-1)
        self._application('Aina', 70)
        self._application('Badrul', 95)
        self.client.post(reverse('compile_applications'))
        ranks = dict(Application.objects.values_list('full_name', 'rank_in_state'))
        self.assertEqual(ranks, {'Badrul': 1, 'Aina': 2})
        self.assertFalse(Application.objects.exclude(status='Compiled').exists())

    def test_pipeline_agent_cannot_compile(self):
        self.assertNotIn('compile_and_rank_applications', ai_agent.TOOL_IMPLEMENTATIONS)

    def test_admin_can_compile_too(self):
        self._set_deadline(-1)
        self._application('Aina', 90)
        self.client.force_login(User.objects.create_user(username='admin', password='test-pass-123'))
        response = self.client.post(reverse('compile_applications'))
        self.assertRedirects(response, reverse('agent1_dashboard'), fetch_redirect_response=False)
        self.assertEqual(Application.objects.get().status, 'Compiled')

    def test_state_officer_cannot_compile(self):
        self._set_deadline(-1)
        self._application('Aina', 90)
        user = User.objects.create_user(username='johor', password='test-pass-123')
        OfficerProfile.objects.create(user=user, role='state_officer', state='Johor')
        self.client.force_login(user)
        self.client.post(reverse('compile_applications'))
        self.assertEqual(Application.objects.get().status, 'Application Submitted')


class AnnualRecognitionTests(TestCase):
    """Annual national recognitions on the form, and the Recommended place kept for never-recognised teachers."""

    FORM_DATA = {
        'full_name': 'Aina', 'ic_number': '900101-01-1234', 'email': 'a@example.com', 'current_grade': 'DG44',
        'whatsapp_number': '012-345 6789',
        'school_leader_name': 'Puan Rosnah', 'school_leader_email': 'gb@example.com',
        'school_name': 'SK Taman', 'district': 'PPD Kulai', 'state': 'Johor', 'tech_track': 'Google',
        'certifications': 'GCE L2', 'lnpt_current': 90, 'lnpt_previous': 90, 'lnpt_two_years': 90,
        'training_experience': 'x',
    }

    def _form(self, **extra):
        form = ApplicationForm(data={**self.FORM_DATA, **extra})
        form.is_valid()
        return form

    def test_must_tick_a_recognition_or_never(self):
        self.assertIn('never_recognised', self._form().errors)
        self.assertNotIn('never_recognised', self._form(never_recognised='on').errors)

    def test_recognition_needs_its_years(self):
        self.assertIn('rec_0_years', self._form(rec_0='on').errors)
        form = self._form(rec_0='on', rec_0_years='2025 and 2024', rec_other='Anugerah X', rec_other_years='2023')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['recognitions'], [
            {'name': 'Edufluencer KPM', 'years': '2024, 2025', 'source': 'declared'},
            {'name': 'Anugerah X', 'years': '2023', 'source': 'declared'},
        ])

    def test_cannot_tick_both(self):
        self.assertIn('never_recognised', self._form(rec_1='on', rec_1_years='2025', never_recognised='on').errors)

    def _application(self, name, lnpt, never=False, recognitions=(), ic=None):
        return Application.objects.create(
            full_name=name, ic_number=ic or f'9001{lnpt:02d}-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district='Kulai', state='Johor', tech_track='Google', certifications='x',
            lnpt_current=lnpt, lnpt_previous=lnpt, lnpt_two_years=lnpt, training_experience='x',
            never_recognised=never, recognitions=list(recognitions),
        )

    def test_one_recommended_place_is_kept_for_a_never_recognised_teacher(self):
        recognised = [{'name': 'Edufluencer KPM', 'years': '2025', 'source': 'declared'}]
        self._application('Top', 95, recognitions=recognised)
        self._application('Second', 90, recognitions=recognised)
        self._application('Third', 85, recognitions=recognised)
        self._application('Newcomer', 80, never=True)
        ranking.compile_and_rank()
        recommended = dict(Application.objects.values_list('full_name', 'is_recommended'))
        self.assertEqual(recommended, {'Top': True, 'Second': False, 'Third': False, 'Newcomer': True})
        self.assertTrue(Application.objects.get(full_name='Newcomer').reserved_place)

    def test_no_reservation_when_a_newcomer_already_makes_the_top(self):
        self._application('Top', 95, never=True)
        self._application('Second', 90, recognitions=[{'name': 'GPGD', 'years': '2025', 'source': 'declared'}])
        self._application('Third', 85, never=True)
        ranking.compile_and_rank()
        self.assertEqual(list(Application.objects.filter(is_recommended=True).values_list('full_name', flat=True).order_by('full_name')),
                         ['Second', 'Top'])
        self.assertFalse(Application.objects.filter(reserved_place=True).exists())

    def test_past_gpgd_found_in_records_is_not_never_recognised(self):
        past = self._application('Aina 2025', 90, ic='880101-01-5555')
        past.status, past.recognized_year = 'Approved', timezone.localdate().year - 1
        past.save()
        self._application('Aina', 90, never=True, ic='880101-01-5555')
        ranking.compile_and_rank()
        aina = Application.objects.get(full_name='Aina')
        self.assertFalse(aina.is_never_recognised)
        self.assertEqual(aina.recognitions[0]['source'], 'records')


class TrackLimitTests(TestCase):
    """The Admin / MoE Officer can cap how many GPGDs a state may have per technology track."""

    def setUp(self):
        self.johor = User.objects.create_user(username='johor', password='test-pass-123')
        OfficerProfile.objects.create(user=self.johor, role='state_officer', state='Johor')
        self.moe = User.objects.create_user(username='moe', password='test-pass-123')
        OfficerProfile.objects.create(user=self.moe, role='moe_officer')

    def _application(self, name, status='Compiled', track='Google'):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district='Kulai', state='Johor', tech_track=track, certifications='x',
            lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x', status=status,
        )

    def test_moe_officer_sets_maximums(self):
        self.client.force_login(self.moe)
        self.client.post(reverse('track_limits'), {'max__Johor__Google': '2', 'max__Johor__Apple': ''})
        self.assertEqual(TrackLimit.objects.get(state='Johor', tech_track='Google').maximum, 2)
        self.assertFalse(TrackLimit.objects.filter(tech_track='Apple').exists())

    def test_state_officer_cannot_open_the_page(self):
        self.client.force_login(self.johor)
        self.assertEqual(self.client.get(reverse('track_limits')).status_code, 302)

    def test_state_officer_cannot_approve_past_the_maximum(self):
        TrackLimit.objects.create(state='Johor', tech_track='Google', maximum=1)
        self._application('Aina', 'State Approved')
        badrul = self._application('Badrul')
        other_track = self._application('Chong', track='Apple')
        self.client.force_login(self.johor)
        for application in (badrul, other_track):
            self.client.post(reverse('state_review'), {'application_id': application.id, 'decision': 'approve'})
        badrul.refresh_from_db()
        other_track.refresh_from_db()
        self.assertEqual(badrul.status, 'Compiled')
        self.assertEqual(other_track.status, 'State Approved')  # no limit on Apple

    def test_declining_frees_a_place(self):
        TrackLimit.objects.create(state='Johor', tech_track='Google', maximum=1)
        aina = self._application('Aina', 'State Approved')
        badrul = self._application('Badrul')
        self.client.force_login(self.johor)
        self.client.post(reverse('state_review'), {'application_id': aina.id, 'decision': 'decline'})
        self.client.post(reverse('state_review'), {'application_id': badrul.id, 'decision': 'approve'})
        badrul.refresh_from_db()
        self.assertEqual(badrul.status, 'State Approved')

    def test_moe_cannot_approve_past_the_maximum(self):
        self._application('Aina', 'Approved')
        badrul = self._application('Badrul', 'State Approved')
        TrackLimit.objects.create(state='Johor', tech_track='Google', maximum=1)  # lowered after State approvals
        self.client.force_login(self.moe)
        self.client.post(reverse('moe_review'), {'application_id': badrul.id, 'decision': 'approve'})
        badrul.refresh_from_db()
        self.assertEqual(badrul.status, 'State Approved')


class DistrictTests(TestCase):
    """District is one of the 143 official PPDs, and maximums can be set per district as well as per state."""

    def test_district_must_be_in_the_chosen_state(self):
        data = {**AnnualRecognitionTests.FORM_DATA, 'never_recognised': 'on'}
        self.assertTrue(ApplicationForm(data=data).is_valid())
        form = ApplicationForm(data={**data, 'district': 'PPD Klang'})  # a Selangor district
        self.assertFalse(form.is_valid())
        self.assertIn('district', form.errors)

    def test_states_without_ppd_use_the_state_itself(self):
        data = {**AnnualRecognitionTests.FORM_DATA, 'never_recognised': 'on', 'state': 'Perlis', 'district': 'Perlis'}
        self.assertTrue(ApplicationForm(data=data).is_valid())

    def _application(self, name, district, status='Compiled'):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district=district, state='Johor', tech_track='Google', certifications='x',
            lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x', status=status,
        )

    def test_district_maximum_is_enforced_alongside_the_state_maximum(self):
        johor = User.objects.create_user(username='johor', password='test-pass-123')
        OfficerProfile.objects.create(user=johor, role='state_officer', state='Johor')
        TrackLimit.objects.create(state='Johor', district='PPD Kulai', tech_track='Google', maximum=1)
        self._application('Aina', 'PPD Kulai', 'State Approved')
        badrul = self._application('Badrul', 'PPD Kulai')
        chong = self._application('Chong', 'PPD Segamat')  # another district: no district limit
        self.client.force_login(johor)
        for application in (badrul, chong):
            self.client.post(reverse('state_review'), {'application_id': application.id, 'decision': 'approve'})
        badrul.refresh_from_db()
        chong.refresh_from_db()
        self.assertEqual(badrul.status, 'Compiled')
        self.assertEqual(chong.status, 'State Approved')

    def test_moe_sets_district_maximums(self):
        moe = User.objects.create_user(username='moe', password='test-pass-123')
        OfficerProfile.objects.create(user=moe, role='moe_officer')
        self.client.force_login(moe)
        response = self.client.get(reverse('track_limits'), {'state': 'Johor'})
        self.assertContains(response, 'PPD Tangkak')
        self.client.post(reverse('track_limits'), {'state': 'Johor', 'max__PPD Kulai__Google': '3'})
        limit = TrackLimit.objects.get()
        self.assertEqual((limit.state, limit.district, limit.maximum), ('Johor', 'PPD Kulai', 3))


class WhatsAppNumberTests(TestCase):
    def _number(self, value):
        form = ApplicationForm(data={**AnnualRecognitionTests.FORM_DATA, 'never_recognised': 'on', 'whatsapp_number': value})
        form.is_valid()
        return form.cleaned_data.get('whatsapp_number'), form.errors.get('whatsapp_number')

    def test_usual_formats_are_stored_the_same_way(self):
        for value in ['012-345 6789', '+6012 345 6789', '60123456789', '0123456789']:
            self.assertEqual(self._number(value), ('+60123456789', None), value)
        self.assertEqual(self._number('011-1234 5678')[0], '+601112345678')

    def test_required_and_must_be_a_mobile_number(self):
        for value in ['', '03-1234 5678', '12345']:
            self.assertIsNotNone(self._number(value)[1], value)


class MonthlyReportingTests(TestCase):
    """Monthly reminder to the current GPGDs, and the monthly statistics emails to leaders."""

    def _gpgd(self, name, year, state='Johor', district='PPD Kulai', school='SK Kulai', leader='gb.kulai@example.com'):
        return Application.objects.create(
            full_name=name, ic_number=f'90{year}{len(name):02d}-01-1234', email=f'{name.lower()}@example.com',
            current_grade='DG41', school_name=school, school_leader_name='GB', school_leader_email=leader,
            district=district, state=state, tech_track='Google', certifications='x', lnpt_current=90,
            lnpt_previous=90, lnpt_two_years=90, training_experience='x', status='Approved', recognized_year=year,
        )

    def _report(self, gpgd, day, teachers=0, students=0):
        report = ActivityReport(gpgd=gpgd, training_title='Session', training_date=day, start_time='09:00',
                                end_time='11:00', hours=2, num_teachers=teachers, num_students=students,
                                training_mode='physical', venue_platform='Dewan', photo_1='p1.png', photo_2='p2.png')
        report.save()
        return report

    def test_counts_set_total_and_audience(self):
        report = self._report(self._gpgd('Aina', 2026), date(2026, 9, 3), teachers=12, students=30)
        self.assertEqual(report.num_participants, 42)
        self.assertEqual(report.target_audience, ['teachers', 'students'])

    def test_activity_form_needs_at_least_one_count(self):
        form = ActivityReportForm(data={'training_title': 'x', 'training_date': '2026-09-03', 'start_time': '09:00',
                                        'end_time': '10:00', 'hours': 1, 'training_mode': 'online', 'venue_platform': 'Meet',
                                        'num_teachers': 0, 'num_students': 0, 'num_school_leaders': 0, 'num_others': 0})
        form.is_valid()
        self.assertIn('num_teachers', form.errors)

    def test_reminder_goes_only_to_the_current_year(self):
        self._gpgd('Old', 2025)
        current = self._gpgd('New', 2026)
        result = pipeline_ops.send_monthly_report_reminders(period='2026-10')
        self.assertEqual(result['sent'], 1)
        self.assertEqual(list(MonthlyReminderLog.objects.values_list('application', flat=True)), [current.id])

    def test_reports_per_level_count_only_that_month_and_current_year(self):
        aina = self._gpgd('Aina', 2026)
        badrul = self._gpgd('Badrul', 2026, district='PPD Segamat', school='SK Segamat', leader='gb.segamat@example.com')
        old = self._gpgd('Old', 2025)
        self._report(aina, date(2026, 9, 3), teachers=10, students=20)
        self._report(badrul, date(2026, 9, 10), teachers=5)
        self._report(aina, date(2026, 10, 1), teachers=99)  # another month
        self._report(old, date(2026, 9, 3), teachers=99)    # an earlier year's GPGD
        ReportRecipient.objects.create(level='bstp', email='director@example.com')
        ReportRecipient.objects.create(level='state', state='Johor', email='jpn@example.com')
        ReportRecipient.objects.create(level='district', state='Johor', district='PPD Kulai', email='ppd@example.com')

        emails = {(e['level'], e['email']): e for e in monthly_reports.build_reports('2026-09')}
        self.assertEqual(emails[('bstp', 'director@example.com')]['totals']['num_teachers'], 15)
        self.assertEqual(emails[('state', 'jpn@example.com')]['totals']['num_students'], 20)
        self.assertEqual(emails[('district', 'ppd@example.com')]['totals']['num_teachers'], 10)
        self.assertEqual(emails[('school_leader', 'gb.segamat@example.com')]['totals']['num_teachers'], 5)
        self.assertIn('Johor', emails[('bstp', 'director@example.com')]['html'])

    def test_sending_twice_does_not_email_twice(self):
        ReportRecipient.objects.create(level='bstp', email='director@example.com')
        self._gpgd('Aina', 2026)
        first = monthly_reports.send_monthly_reports('2026-09')
        second = monthly_reports.send_monthly_reports('2026-09')
        # The director and the GPGD's school leader each get one email, once.
        self.assertEqual((first['sent'], second['sent'], second['already_sent']), (2, 0, 2))

    def test_previous_period(self):
        self.assertEqual(monthly_reports.previous_period(date(2026, 1, 7)), '2025-12')
        self.assertEqual(monthly_reports.previous_period(date(2026, 10, 7)), '2026-09')

    @mock.patch('agents.monthly_reports.send_monthly_reports')
    @mock.patch('agents.pipeline_ops.send_monthly_report_reminders')
    def test_scheduler_sends_only_what_is_due(self, reminders, reports):
        reminders.return_value = {'period': '2026-10', 'sent': 0, 'failed': []}
        reports.return_value = {'period': '2026-09', 'sent': 0, 'failed': []}
        for day, expect_reports, expect_reminders in [(3, False, False), (7, True, False), (15, True, True)]:
            reminders.reset_mock()
            reports.reset_mock()
            with mock.patch('django.utils.timezone.localdate', return_value=date(2026, 10, day)):
                call_command('run_scheduled_jobs', stdout=StringIO())
            self.assertEqual((reports.called, reminders.called), (expect_reports, expect_reminders), day)

    def test_recipients_upload(self):
        self.client.force_login(User.objects.create_user(username='admin', password='test-pass-123'))
        buffer = BytesIO()
        pd.DataFrame([
            {'Level': 'BSTP Director', 'State': '', 'District': '', 'Name': 'Dr A', 'Email': 'a@example.com'},
            {'Level': 'District Education Lead', 'State': 'Johor', 'District': 'ppd kulai', 'Name': 'B', 'Email': 'b@example.com'},
            {'Level': 'District Education Lead', 'State': 'Johor', 'District': 'Nowhere', 'Name': 'C', 'Email': 'c@example.com'},
        ]).to_excel(buffer, index=False)
        buffer.seek(0)
        buffer.name = 'r.xlsx'
        self.client.post(reverse('report_recipients'), {'upload': buffer})
        self.assertEqual(sorted(ReportRecipient.objects.values_list('district', 'email')),
                         [('', 'a@example.com'), ('PPD Kulai', 'b@example.com')])

    def test_activity_page_defaults_to_current_year_and_downloads(self):
        self.client.force_login(User.objects.create_user(username='admin', password='test-pass-123'))
        self._report(self._gpgd('Old', 2025), date(2025, 9, 3), teachers=7)
        self._report(self._gpgd('New', 2026), date(2026, 9, 3), teachers=3)
        response = self.client.get(reverse('activity_dashboard'))
        self.assertEqual((response.context['year'], response.context['total_teachers_trained']), (2026, 3))
        self.assertEqual(self.client.get(reverse('activity_dashboard'), {'year': 2025}).context['total_teachers_trained'], 7)
        download = self.client.get(reverse('activity_dashboard'), {'year': 2025, 'download': 1})
        self.assertIn('GPGD_Activity_Reports_2025.xlsx', download['Content-Disposition'])


class DemoInboxTests(TestCase):
    def test_not_available_outside_the_demo(self):
        self.assertEqual(self.client.get(reverse('demo_inbox')).status_code, 404)

    def test_shows_emails_the_demo_sent_with_working_links(self):
        import tempfile
        from django.core.mail import send_mail
        with tempfile.TemporaryDirectory() as folder, override_settings(
                GPGD_DEMO=True, EMAIL_FILE_PATH=folder,
                EMAIL_BACKEND='django.core.mail.backends.filebased.EmailBackend'):
            send_mail('Invitation to Apply', 'Apply here: http://localhost:8001/agents/apply/abc/',
                      'noreply@moe.gov.my', ['aina@example.com'])
            response = self.client.get(reverse('demo_inbox'), {'q': 'aina'})
            self.assertContains(response, 'aina@example.com')
            self.assertContains(response, '<a href="http://localhost:8001/agents/apply/abc/"')


class StateReviewGroupingTests(TestCase):
    """State Review groups waiting applications by district and technology track."""

    def setUp(self):
        user = User.objects.create_user(username='johor', password='test-pass-123')
        OfficerProfile.objects.create(user=user, role='state_officer', state='Johor')
        self.client.force_login(user)

    def _application(self, name, district, track, rank, status='Compiled'):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district=district, state='Johor', tech_track=track, certifications='x',
            lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x', status=status,
            rank_in_state=rank,
        )

    def test_groups_by_district_then_track_with_quota_progress(self):
        self._application('Aina', 'PPD Kulai', 'Google', 1)
        self._application('Badrul', 'PPD Kulai', 'Google', 3)
        self._application('Chong', 'PPD Kulai', 'Apple', 2)
        self._application('Devi', 'PPD Batu Pahat', 'Google', 4)
        self._application('Ehsan', 'PPD Kulai', 'Google', 5, status='State Approved')  # decided: counts toward quota
        groups = self.client.get(reverse('state_review')).context['groups']
        self.assertEqual([(g['district'], g['track']) for g in groups],
                         [('PPD Batu Pahat', 'Google'), ('PPD Kulai', 'Apple'), ('PPD Kulai', 'Google')])
        kulai_google = groups[2]
        self.assertEqual([a.full_name for a in kulai_google['applications']], ['Aina', 'Badrul'])
        self.assertEqual((kulai_google['need'], kulai_google['approved'], kulai_google['remaining']), (2, 1, 1))

    def test_filters_and_returns_to_the_same_group(self):
        aina = self._application('Aina', 'PPD Kulai', 'Google', 1)
        self._application('Devi', 'PPD Batu Pahat', 'Google', 2)
        response = self.client.get(reverse('state_review'), {'district': 'PPD Kulai'})
        self.assertEqual([g['district'] for g in response.context['groups']], ['PPD Kulai'])
        response = self.client.post(reverse('state_review'), {
            'application_id': aina.id, 'decision': 'approve', 'filters': 'district=PPD+Kulai'})
        self.assertEqual(response['Location'], reverse('state_review') + '?district=PPD+Kulai#ppd-kulai-google')


class MoeReviewGroupingTests(TestCase):
    """MoE Review groups state-approved candidates by state, district and technology track."""

    def setUp(self):
        user = User.objects.create_user(username='moe', password='test-pass-123')
        OfficerProfile.objects.create(user=user, role='moe_officer')
        self.client.force_login(user)

    def _application(self, name, state, district, track, status='State Approved', rank=1):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district=district, state=state, tech_track=track, certifications='x',
            lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x', status=status,
            rank_in_state=rank, recognized_year=timezone.localdate().year if status == 'Approved' else None,
        )

    def test_groups_by_state_district_track_with_moe_approvals(self):
        self._application('Aina', 'Johor', 'PPD Kulai', 'Google')
        self._application('Badrul', 'Johor', 'PPD Kulai', 'Google', status='Approved')
        self._application('Chong', 'Selangor', 'PPD Klang', 'Apple')
        self._application('Devi', 'Perlis', 'Perlis', 'Google')
        groups = self.client.get(reverse('moe_review')).context['groups']
        self.assertEqual([(g['state'], g['district'], g['track']) for g in groups],
                         [('Johor', 'PPD Kulai', 'Google'), ('Perlis', None, 'Google'), ('Selangor', 'PPD Klang', 'Apple')])
        self.assertEqual((groups[0]['approved'], groups[0]['remaining']), (1, 1))
        self.assertEqual(groups[1]['need'], 5)  # no districts: 5 state-wide

    def test_filter_by_state_and_return_to_the_group(self):
        aina = self._application('Aina', 'Johor', 'PPD Kulai', 'Google')
        self._application('Chong', 'Selangor', 'PPD Klang', 'Apple')
        response = self.client.get(reverse('moe_review'), {'state': 'Johor'})
        self.assertEqual([g['state'] for g in response.context['groups']], ['Johor'])
        with mock.patch('agents.views._send_recognition_letter'):
            response = self.client.post(reverse('moe_review'), {
                'application_id': aina.id, 'decision': 'approve', 'filters': 'state=Johor'})
        self.assertEqual(response['Location'], reverse('moe_review') + '?state=Johor#johor-ppd-kulai-google')


class BulkDecisionTests(TestCase):
    """Approving or declining several ticked candidates at once, on State Review and MoE Review."""

    def _application(self, name, rank, state='Johor', status='Compiled'):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email='t@example.com', current_grade='DG41',
            school_name='SK Taman', district='PPD Kulai', state=state, tech_track='Google', certifications='x',
            lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x', status=status,
            rank_in_state=rank, is_recommended=rank <= 2,
        )

    def _login(self, role, state=''):
        user = User.objects.create_user(username=role, password='test-pass-123')
        OfficerProfile.objects.create(user=user, role=role, state=state)
        self.client.force_login(user)

    def test_state_officer_approves_ticked_in_own_state_only(self):
        self._login('state_officer', 'Johor')
        aina, badrul = self._application('Aina', 1), self._application('Badrul', 2)
        elsewhere = self._application('Chong', 1, state='Selangor')
        response = self.client.post(reverse('state_review'), {
            'bulk': 'approve', 'application_ids': [aina.id, badrul.id, elsewhere.id],
            'remarks': 'Strong candidates', 'filters': 'district=PPD+Kulai'})
        self.assertEqual(response['Location'], reverse('state_review') + '?district=PPD+Kulai')
        statuses = dict(Application.objects.values_list('full_name', 'status'))
        self.assertEqual(statuses, {'Aina': 'State Approved', 'Badrul': 'State Approved', 'Chong': 'Compiled'})
        self.assertEqual(Application.objects.get(full_name='Aina').state_remarks, 'Strong candidates')

    def test_a_maximum_keeps_the_best_ranked(self):
        self._login('state_officer', 'Johor')
        TrackLimit.objects.create(state='Johor', tech_track='Google', maximum=1)
        second, first = self._application('Second', 2), self._application('First', 1)
        self.client.post(reverse('state_review'), {'bulk': 'approve', 'application_ids': [second.id, first.id]})
        statuses = dict(Application.objects.values_list('full_name', 'status'))
        self.assertEqual(statuses, {'First': 'State Approved', 'Second': 'Compiled'})

    def test_moe_bulk_approval_sends_each_recognition_letter(self):
        self._login('moe_officer')
        aina = self._application('Aina', 1, status='State Approved')
        badrul = self._application('Badrul', 2, status='State Approved')
        with mock.patch('agents.views._send_recognition_letter') as letter:
            self.client.post(reverse('moe_review'), {'bulk': 'approve', 'application_ids': [aina.id, badrul.id],
                                                     'award_category': 'Innovative Pedagogy'})
        self.assertEqual(letter.call_count, 2)
        self.assertEqual(set(Application.objects.values_list('status', 'award_category')),
                         {('Approved', 'Innovative Pedagogy')})

    def test_bulk_decline(self):
        self._login('state_officer', 'Johor')
        aina = self._application('Aina', 1)
        self.client.post(reverse('state_review'), {'bulk': 'decline', 'application_ids': [aina.id]})
        self.assertEqual(Application.objects.get().status, 'State Declined')


class _RunNow:
    """Stands in for threading.Thread so a background send runs immediately in the test."""
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


class MoeDecisionsPageTests(TestCase):
    """After the MoE approves: the Recognised & Rejected page and the Letter of Recognition status."""

    def setUp(self):
        user = User.objects.create_user(username='moe', password='test-pass-123')
        OfficerProfile.objects.create(user=user, role='moe_officer')
        self.client.force_login(user)

    def _application(self, name, status='State Approved'):
        return Application.objects.create(
            full_name=name, ic_number='900101-01-1234', email=f'{name.lower()}@example.com', current_grade='DG41',
            school_name='SK Taman', district='PPD Kulai', state='Johor', tech_track='Google', certifications='x',
            lnpt_current=90, lnpt_previous=90, lnpt_two_years=90, training_experience='x', status=status, rank_in_state=1,
        )

    @mock.patch('agents.pipeline_ops.threading.Thread', _RunNow)
    def test_approval_shows_on_the_page_with_the_letter_emailed(self):
        aina = self._application('Aina')
        self._application('Badrul')
        response = self.client.post(reverse('moe_review'), {'application_id': aina.id, 'decision': 'approve'}, follow=True)
        self.assertContains(response, 'is now a recognised GPGD')
        aina.refresh_from_db()
        self.assertIsNotNone(aina.recognition_letter_sent_at)

        page = self.client.get(reverse('moe_decisions'))
        self.assertEqual([a.full_name for a in page.context['page']], ['Aina'])
        self.assertContains(page, 'Emailed')
        self.assertEqual(page.context['counts'], {'all': 1, 'approved': 1, 'rejected': 0})

    @mock.patch('agents.pipeline_ops.threading.Thread', _RunNow)
    @mock.patch('agents.pipeline_ops._send_mail_with_hard_timeout', side_effect=TimeoutError('no answer'))
    def test_failed_letter_is_shown_and_can_be_resent(self, send):
        aina = self._application('Aina')
        self.client.post(reverse('moe_review'), {'application_id': aina.id, 'decision': 'approve'})
        aina.refresh_from_db()
        self.assertEqual((aina.recognition_letter_sent_at, aina.recognition_letter_error), (None, 'no answer'))
        self.assertContains(self.client.get(reverse('moe_decisions')), 'Failed to send')

        send.side_effect = None
        self.client.post(reverse('moe_decisions'), {'application_id': aina.id})
        aina.refresh_from_db()
        self.assertIsNotNone(aina.recognition_letter_sent_at)

    def test_moe_can_open_the_recognition_dashboard(self):
        self.assertEqual(self.client.get(reverse('recognition_dashboard')).status_code, 200)
