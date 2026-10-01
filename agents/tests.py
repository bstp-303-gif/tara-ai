from datetime import timedelta
from unittest import mock

import pandas as pd

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import ai_agent, pipeline_ops
from .eligibility import eligible_tracks, save_to_database
from .forms import ApplicationForm
from .models import (
    CertificationRule, InvitationExclusion, InvitationRecord, OfficerProfile, ProgrammeSettings, Provider, Teacher,
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
