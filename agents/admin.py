from django import forms
from django.contrib import admin
from .constants import STATE_CHOICES
from .models import (
    ActivityReport, AgentActivityLog, Application, CertificationRule, FileUpload,
    MonthlyReminderLog, OfficerProfile, ProgrammeSettings, Provider, Teacher,
)


class OfficerProfileAdminForm(forms.ModelForm):
    state = forms.ChoiceField(choices=[('', '— None (MoE Officer) —')] + STATE_CHOICES, required=False)

    class Meta:
        model = OfficerProfile
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('role') == 'state_officer' and not cleaned_data.get('state'):
            self.add_error('state', 'A State Officer must be assigned a state.')
        return cleaned_data


@admin.register(OfficerProfile)
class OfficerProfileAdmin(admin.ModelAdmin):
    form = OfficerProfileAdminForm
    list_display = ('user', 'role', 'state')
    list_filter = ('role', 'state')
    search_fields = ('user__username', 'user__email', 'state')


class CertificationRuleInline(admin.TabularInline):
    model = CertificationRule
    extra = 1
    fields = ('canonical_name', 'required_keywords', 'exclude_keywords', 'is_eligible', 'priority')


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ('name', 'display_name', 'is_active', 'rule_count')
    list_editable = ('is_active',)
    inlines = [CertificationRuleInline]

    def rule_count(self, obj):
        return obj.certification_rules.count()
    rule_count.short_description = 'Rules'


@admin.register(CertificationRule)
class CertificationRuleAdmin(admin.ModelAdmin):
    list_display = ('canonical_name', 'provider', 'required_keywords', 'exclude_keywords', 'is_eligible', 'priority')
    list_filter = ('provider', 'is_eligible')
    list_editable = ('is_eligible', 'priority')
    ordering = ('provider', '-priority', 'canonical_name')


@admin.register(FileUpload)
class FileUploadAdmin(admin.ModelAdmin):
    list_display = ('file_name', 'provider', 'validation_status', 'record_count', 'uploaded_at')
    list_filter = ('provider', 'validation_status')


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'ic_number', 'provider', 'certification', 'eligibility_status', 'state')
    list_filter = ('eligibility_status', 'provider', 'state', 'multi_certified')
    search_fields = ('full_name', 'ic_number', 'email')


@admin.register(ProgrammeSettings)
class ProgrammeSettingsAdmin(admin.ModelAdmin):
    list_display = ('submission_deadline',)

    def has_add_permission(self, request):
        return not ProgrammeSettings.objects.exists()


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = (
        'reference_number', 'full_name', 'ic_number', 'tech_track', 'state', 'district',
        'status', 'score', 'rank_in_state', 'is_recommended', 'submitted_at',
    )
    list_filter = ('status', 'tech_track', 'state', 'is_recommended')
    search_fields = ('full_name', 'ic_number', 'email', 'reference_number')
    readonly_fields = ('reference_number', 'submitted_at', 'score', 'rank_in_state', 'compiled_at')
    list_editable = ('status',)
    ordering = ('-submitted_at',)


@admin.register(AgentActivityLog)
class AgentActivityLogAdmin(admin.ModelAdmin):
    list_display = ('triggered_at', 'trigger_reason', 'status', 'summary')
    list_filter = ('status',)
    search_fields = ('trigger_reason', 'summary', 'error_message')
    readonly_fields = ('triggered_at', 'trigger_reason', 'summary', 'actions_taken', 'status', 'error_message')
    ordering = ('-triggered_at',)

    def has_add_permission(self, request):
        return False


@admin.register(ActivityReport)
class ActivityReportAdmin(admin.ModelAdmin):
    list_display = (
        'training_title', 'gpgd', 'training_date', 'hours', 'num_participants',
        'training_mode', 'submitted_at',
    )
    list_filter = ('training_mode', 'training_date')
    search_fields = ('training_title', 'gpgd__full_name', 'gpgd__reference_number', 'venue_platform')
    readonly_fields = ('submitted_at',)
    ordering = ('-training_date',)


@admin.register(MonthlyReminderLog)
class MonthlyReminderLogAdmin(admin.ModelAdmin):
    list_display = ('application', 'period', 'success', 'sent_at')
    list_filter = ('success', 'period')
    search_fields = ('application__full_name', 'application__reference_number')
    readonly_fields = ('application', 'period', 'sent_at', 'success', 'error_message')
    ordering = ('-sent_at',)

    def has_add_permission(self, request):
        return False
