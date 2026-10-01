from datetime import timedelta
from unittest import mock

import pandas as pd

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import ai_agent
from .eligibility import save_to_database
from .models import InvitationExclusion, OfficerProfile, ProgrammeSettings, Teacher


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
