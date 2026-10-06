from django.contrib.auth import views as auth_views
from django.urls import path
from . import demo_views, views

urlpatterns = [
    path('login/', auth_views.LoginView.as_view(template_name='login.html', redirect_authenticated_user=True), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
    path('officer/', views.officer_home, name='officer_home'),
    path('compile-applications/', views.compile_applications, name='compile_applications'),
    path('state/review/', views.state_review, name='state_review'),
    path('state/decisions/', views.state_decisions, name='state_decisions'),
    path('state/statistics/', views.state_statistics, name='state_statistics'),
    path('state/by-district/', views.state_by_district, name='state_by_district'),
    path('moe/review/', views.moe_review, name='moe_review'),
    path('moe/by-district/', views.moe_by_district, name='moe_by_district'),
    path('moe/limits/', views.track_limits, name='track_limits'),
    path('moe/decisions/', views.moe_decisions, name='moe_decisions'),

    path('upload-certifications/', views.upload_certifications, name='upload_certifications'),
    path('agent1-results/', views.agent1_results, name='agent1_results'),
    path('agent1-dashboard/', views.agent1_dashboard, name='agent1_dashboard'),
    path('error-log/', views.error_log, name='error_log'),
    path('download-file/<int:file_id>/<str:file_type>/', views.download_file, name='download_file'),
    path('process-files/', views.process_all_files, name='process_files'),
    path('delete-file/<int:file_id>/', views.delete_file, name='delete_file'),
    path('download-combined/', views.download_combined_normalized, name='download_combined'),
    path('send-invitations/', views.send_invitations, name='send_invitations'),
    path('invitations/remove/', views.remove_from_invitations, name='remove_from_invitations'),
    path('invitations/restore/', views.restore_to_invitations, name='restore_to_invitations'),
    path('update-submission-deadline/', views.update_submission_deadline, name='update_submission_deadline'),
    path('settings/invitation-email/', views.edit_invitation_email, name='edit_invitation_email'),
    path('settings/recognition-letter/', views.edit_recognition_letter, name='edit_recognition_letter'),
    path('settings/application-form/', views.edit_application_form, name='edit_application_form'),
    path('settings/monthly-reminder/', views.edit_monthly_reminder, name='edit_monthly_reminder'),
    path('manage/report-recipients/', views.report_recipients, name='report_recipients'),
    path('monthly-reports/', views.monthly_reports_page, name='monthly_reports'),
    path('demo/inbox/', demo_views.inbox, name='demo_inbox'),  # demo settings only; 404 elsewhere
    path('apply/<str:token>/', views.apply, name='apply'),
    path('apply-success/<str:ref>/', views.apply_success, name='apply_success'),
    path('download-dedup/', views.download_deduplicated, name='download_dedup'),
    path('download-eligible/', views.download_eligible_candidates, name='download_eligible'),

    # In-system management (replaces day-to-day use of Django admin)
    path('manage/providers/', views.manage_providers, name='manage_providers'),
    path('manage/providers/add/', views.add_provider, name='add_provider'),
    path('manage/providers/<int:provider_id>/edit/', views.edit_provider, name='edit_provider'),
    path('manage/providers/<int:provider_id>/delete/', views.delete_provider, name='delete_provider'),
    path('manage/providers/<int:provider_id>/rules/add/', views.add_rule, name='add_rule'),
    path('manage/rules/<int:rule_id>/edit/', views.edit_rule, name='edit_rule'),
    path('manage/rules/<int:rule_id>/delete/', views.delete_rule, name='delete_rule'),

    path('manage/applications/', views.manage_applications, name='manage_applications'),
    path('manage/applications/<int:application_id>/status/', views.update_application_status, name='update_application_status'),
    path('manage/applications/<int:application_id>/delete/', views.delete_application, name='delete_application'),

    path('manage/teachers/', views.manage_teachers, name='manage_teachers'),
    path('manage/teachers/<int:teacher_id>/edit/', views.edit_teacher, name='edit_teacher'),
    path('manage/teachers/<int:teacher_id>/delete/', views.delete_teacher, name='delete_teacher'),

    # GPGD Recognition & Activity Dashboard
    path('recognition-dashboard/', views.recognition_dashboard, name='recognition_dashboard'),
    path('activity-dashboard/', views.activity_dashboard, name='activity_dashboard'),
    path('send-monthly-reminders/', views.send_monthly_reminders, name='send_monthly_reminders'),
    path('report/<str:token>/', views.submit_activity_report, name='submit_activity_report'),
    path('report-success/<int:report_id>/', views.activity_report_success, name='activity_report_success'),
]
