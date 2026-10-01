from django import forms
from . import content_defaults
from .constants import STATE_CHOICES
from .models import ActivityReport, Application, CertificationRule, ProgrammeSettings, Provider, Teacher


class ApplicationForm(forms.ModelForm):
    # A fixed dropdown (not free text) so every application's state matches exactly one State Officer.
    state = forms.ChoiceField(
        choices=[('', '— Select state —')] + STATE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Field labels are editable via the "Application Form" settings page — fall back to
        # the defaults below (which double as Meta.labels for anywhere the form is used
        # without going through the DB, e.g. the Django admin).
        settings_obj = ProgrammeSettings.load()
        labels = {**content_defaults.DEFAULT_APPLY_FIELD_LABELS, **(settings_obj.apply_form_field_labels or {})}
        for field_name, label in labels.items():
            if field_name in self.fields:
                self.fields[field_name].label = label

    class Meta:
        model = Application
        fields = [
            'full_name', 'ic_number', 'email', 'current_grade',
            'school_name', 'district', 'state', 'tech_track',
            'certifications', 'previous_gpgd',
            'lnpt_current', 'lnpt_previous', 'lnpt_two_years',
            'training_experience', 'awards', 'additional_info',
        ]
        widgets = {
            'full_name':           forms.TextInput(attrs={'class': 'form-control'}),
            'ic_number':           forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 850101-01-1234'}),
            'email':               forms.EmailInput(attrs={'class': 'form-control'}),
            'current_grade':       forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. DG48'}),
            'school_name':         forms.TextInput(attrs={'class': 'form-control'}),
            'district':            forms.TextInput(attrs={'class': 'form-control'}),
            'tech_track':          forms.Select(attrs={'class': 'form-select'}),
            'certifications':      forms.Textarea(attrs={'class': 'form-control', 'rows': 4,
                                       'placeholder': 'e.g. Microsoft Innovative Educator Expert (MIEE) — Microsoft, 2023'}),
            'previous_gpgd':       forms.Textarea(attrs={'class': 'form-control', 'rows': 3,
                                       'placeholder': 'Describe any previous GPGD experience, or leave blank.'}),
            'lnpt_current':        forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': 0, 'max': 100}),
            'lnpt_previous':       forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': 0, 'max': 100}),
            'lnpt_two_years':      forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': 0, 'max': 100}),
            'training_experience': forms.Textarea(attrs={'class': 'form-control', 'rows': 4,
                                       'placeholder': 'Describe your experience conducting training, coaching, mentoring, or sharing best practices.'}),
            'awards':              forms.Textarea(attrs={'class': 'form-control', 'rows': 3,
                                       'placeholder': 'List any relevant awards, recognitions, or achievements.'}),
            'additional_info':     forms.Textarea(attrs={'class': 'form-control', 'rows': 3,
                                       'placeholder': 'Any additional information that supports your application (optional).'}),
        }
        labels = {
            'full_name':           'Full Name',
            'ic_number':           'Identity Card (IC) Number',
            'email':               'Email Address',
            'current_grade':       'Current Grade',
            'school_name':         'School Name',
            'district':            'District',
            'state':               'State',
            'tech_track':          'Preferred Technology Track',
            'certifications':      'Professional Certifications',
            'previous_gpgd':       'Previous GPGD Experience (if applicable)',
            'lnpt_current':        'Latest LNPT Score',
            'lnpt_previous':       'LNPT Score (Previous Year)',
            'lnpt_two_years':      'LNPT Score (Two Years Ago)',
            'training_experience': 'Training / Coaching / Mentoring Experience',
            'awards':              'Awards & Recognitions',
            'additional_info':     'Additional Information (Optional)',
        }


class ProviderForm(forms.ModelForm):
    class Meta:
        model = Provider
        fields = ['name', 'display_name', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Google'}),
            'display_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Google Certified Teachers'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class CertificationRuleForm(forms.ModelForm):
    required_keywords_text = forms.CharField(
        label='Required Keywords',
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'comma-separated, e.g. innovative educator, expert'}),
        help_text='All of these must appear in the programme+level text (lowercase).',
    )
    exclude_keywords_text = forms.CharField(
        label='Exclude Keywords',
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'comma-separated, e.g. trainer'}),
        help_text='If any of these appear, the rule does NOT match.',
    )

    class Meta:
        model = CertificationRule
        fields = ['canonical_name', 'is_eligible', 'priority']
        widgets = {
            'canonical_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Microsoft Innovative Educator Expert (MIEE)'}),
            'is_eligible': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'priority': forms.NumberInput(attrs={'class': 'form-control', 'style': 'width:5rem;'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['required_keywords_text'].initial = ', '.join(self.instance.required_keywords or [])
            self.fields['exclude_keywords_text'].initial = ', '.join(self.instance.exclude_keywords or [])

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.required_keywords = [
            kw.strip().lower() for kw in self.cleaned_data['required_keywords_text'].split(',') if kw.strip()
        ]
        instance.exclude_keywords = [
            kw.strip().lower() for kw in self.cleaned_data['exclude_keywords_text'].split(',') if kw.strip()
        ]
        if commit:
            instance.save()
        return instance


class TeacherForm(forms.ModelForm):
    class Meta:
        model = Teacher
        fields = [
            'full_name', 'ic_number', 'email', 'school', 'state',
            'provider', 'certification', 'cert_level', 'cert_year',
            'multi_certified', 'eligibility_status',
        ]
        widgets = {
            'full_name':         forms.TextInput(attrs={'class': 'form-control'}),
            'ic_number':         forms.TextInput(attrs={'class': 'form-control'}),
            'email':             forms.EmailInput(attrs={'class': 'form-control'}),
            'school':            forms.TextInput(attrs={'class': 'form-control'}),
            'state':             forms.TextInput(attrs={'class': 'form-control'}),
            'provider':          forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Google, Microsoft'}),
            'certification':     forms.TextInput(attrs={'class': 'form-control'}),
            'cert_level':        forms.TextInput(attrs={'class': 'form-control'}),
            'cert_year':         forms.NumberInput(attrs={'class': 'form-control'}),
            'multi_certified':   forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'eligibility_status': forms.Select(attrs={'class': 'form-select'}, choices=[
                ('Eligible', 'Eligible'),
                ('Not Eligible', 'Not Eligible'),
                ('pending', 'Pending'),
                ('Application Submitted', 'Application Submitted'),
            ]),
        }


class CertificationFileUploadForm(forms.Form):
    PROVIDER_CHOICES = [
        ('Google', 'Google Certified Teachers'),
        ('Microsoft', 'Microsoft Certified Teachers'),
        ('Apple', 'Apple Certified Teachers'),
    ]

    file_upload = forms.FileField(
        label='Upload Excel File (.xlsx)',
        widget=forms.FileInput(attrs={
            'accept': '.xlsx',
            'class': 'form-control'
        }),
        help_text='Upload an Excel file containing certified teacher data'
    )

    provider = forms.ChoiceField(
        label='Provider',
        choices=PROVIDER_CHOICES,
        widget=forms.Select(attrs={
            'class': 'form-control'
        })
    )


class ActivityReportForm(forms.ModelForm):
    target_audience = forms.MultipleChoiceField(
        label='Target Audience',
        choices=ActivityReport.AUDIENCE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = ActivityReport
        fields = [
            'training_title', 'training_date', 'start_time', 'end_time', 'hours',
            'target_audience', 'num_participants', 'training_mode', 'venue_platform',
            'description', 'photo_1', 'photo_2', 'supporting_document',
        ]
        widgets = {
            'training_title':   forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Digital Storytelling with Google Slides'}),
            'training_date':    forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'start_time':       forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'end_time':         forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'hours':            forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5', 'min': 0, 'max': 24}),
            'num_participants': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'training_mode':    forms.Select(attrs={'class': 'form-select'}),
            'venue_platform':   forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Dewan SK Taman Setia, or Google Meet'}),
            'description':      forms.Textarea(attrs={'class': 'form-control', 'rows': 4,
                                    'placeholder': 'Briefly describe what was covered and its impact.'}),
            'photo_1':          forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'photo_2':          forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'supporting_document': forms.ClearableFileInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'training_title':      'Training Title',
            'training_date':       'Date',
            'start_time':          'Start Time',
            'end_time':             'End Time',
            'hours':                'Number of Hours',
            'num_participants':    'Number of Participants',
            'training_mode':       'Training Mode',
            'venue_platform':      'Venue / Platform',
            'description':         'Brief Description',
            'photo_1':              'Evidence Photo 1',
            'photo_2':              'Evidence Photo 2',
            'supporting_document': 'Supporting Document (optional)',
        }

    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get('start_time')
        end_time = cleaned_data.get('end_time')
        if start_time and end_time and end_time <= start_time:
            self.add_error('end_time', 'End time must be after the start time.')
        return cleaned_data
