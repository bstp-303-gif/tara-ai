import re

from django import forms
from . import content_defaults
from .constants import ANNUAL_RECOGNITIONS, MALAYSIA_STATES, STATE_CHOICES, districts_for
from .models import ActivityReport, Application, CertificationRule, ProgrammeSettings, Provider, Teacher


class ApplicationForm(forms.ModelForm):
    # A fixed dropdown (not free text) so every application's state matches exactly one State Officer.
    state = forms.ChoiceField(
        choices=[('', '— Select state —')] + STATE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    # The official PPD list, grouped by state; the page shows only the chosen state's group.
    district = forms.ChoiceField(
        choices=[('', '— Select district (PPD) —')] + [(s, [(d, d) for d in districts_for(s)]) for s in MALAYSIA_STATES],
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    def __init__(self, *args, allowed_tracks=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Only offer the tracks the teacher is actually certified for. With more than one
        # (multi-certified), nothing is preselected so they make a deliberate choice.
        if allowed_tracks:
            valid = {value for value, _ in Application.TRACK_CHOICES}
            choices = [(t, t) for t in allowed_tracks if t in valid]
            if choices:
                field = self.fields['tech_track']
                field.choices = ([('', '— Choose your technology track —')] if len(choices) > 1 else []) + choices
                if len(choices) > 1:
                    field.help_text = (
                        'You are certified in ' + ' and '.join(t for t, _ in choices)
                        + ' — choose the technology track you want to apply for.'
                    )
        # Field labels are editable via the "Application Form" settings page — fall back to
        # the defaults below (which double as Meta.labels for anywhere the form is used
        # without going through the DB, e.g. the Django admin).
        settings_obj = ProgrammeSettings.load()
        labels = {**content_defaults.DEFAULT_APPLY_FIELD_LABELS, **(settings_obj.apply_form_field_labels or {})}
        for field_name, label in labels.items():
            if field_name in self.fields:
                self.fields[field_name].label = label

        # Annual national recognitions: a tick box and a "year(s)" box for each, plus "Other".
        years_widget = {'class': 'form-control form-control-sm', 'placeholder': 'Year(s), e.g. 2024, 2025'}
        for i, name in enumerate(ANNUAL_RECOGNITIONS):
            label = 'GPGD (previous years)' if name == 'GPGD' else name
            self.fields[f'rec_{i}'] = forms.BooleanField(required=False, label=label)
            self.fields[f'rec_{i}_years'] = forms.CharField(required=False, widget=forms.TextInput(attrs=years_widget))
        self.fields['rec_other'] = forms.CharField(
            required=False, label='Other national-level recognition',
            widget=forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Name of the recognition'}))
        self.fields['rec_other_years'] = forms.CharField(required=False, widget=forms.TextInput(attrs=years_widget))
        # Required here, though optional on the model (applications from before these fields existed).
        for field_name in ('school_leader_name', 'school_leader_email'):
            self.fields[field_name].required = True
        self.fields['school_leader_email'].help_text = (
            "Your school leader receives a monthly summary of the trainings you report as a GPGD.")
        self.fields['never_recognised'] = forms.BooleanField(
            required=False, label='I have never received any annual national-level recognition')

    def clean_whatsapp_number(self):
        """Accepts a Malaysian mobile number in any usual format (012-345 6789, +6012 345 6789, 60123456789)
        and stores it as +60123456789."""
        digits = re.sub(r'\D', '', self.cleaned_data.get('whatsapp_number') or '')
        if digits.startswith('60'):
            digits = digits[2:]
        if digits.startswith('0'):
            digits = digits[1:]
        if not re.fullmatch(r'1\d{8,9}', digits):
            raise forms.ValidationError('Enter a Malaysian mobile number for WhatsApp, e.g. 012-345 6789.')
        return f'+60{digits}'

    def recognition_rows(self):
        """(tick box, years box) pairs for the template, in the order of ANNUAL_RECOGNITIONS."""
        return [(self[f'rec_{i}'], self[f'rec_{i}_years']) for i in range(len(ANNUAL_RECOGNITIONS))]

    def clean(self):
        cleaned = super().clean()
        state, district = cleaned.get('state'), cleaned.get('district')
        if state and district and district not in districts_for(state):
            self.add_error('district', f'{district} is not in {state}. Choose a district in {state}.')
        recognitions = []
        held = [(name, f'rec_{i}', f'rec_{i}_years') for i, name in enumerate(ANNUAL_RECOGNITIONS) if cleaned.get(f'rec_{i}')]
        other = (cleaned.get('rec_other') or '').strip()
        if other:
            held.append((other, 'rec_other', 'rec_other_years'))
        for name, field, years_field in held:
            years = sorted(set(re.findall(r'\b(?:19|20)\d{2}\b', cleaned.get(years_field) or '')))
            if years:
                recognitions.append({'name': name, 'years': ', '.join(years), 'source': 'declared'})
            else:
                self.add_error(years_field, 'Enter the year(s) you received this recognition, e.g. 2024, 2025.')

        if held and cleaned.get('never_recognised'):
            self.add_error('never_recognised', 'You ticked a recognition above. Untick it, or untick this box.')
        elif not held and not cleaned.get('never_recognised'):
            self.add_error('never_recognised', 'Tick the recognitions you have received, or tick this box if you have never received any.')
        cleaned['recognitions'] = recognitions
        return cleaned

    def save(self, commit=True):
        self.instance.recognitions = self.cleaned_data['recognitions']
        self.instance.never_recognised = self.cleaned_data['never_recognised']
        return super().save(commit=commit)

    class Meta:
        model = Application
        fields = [
            'full_name', 'ic_number', 'email', 'whatsapp_number', 'current_grade',
            'school_name', 'school_leader_name', 'school_leader_email', 'district', 'state', 'tech_track',
            'certifications', 'previous_gpgd',
            'lnpt_current', 'lnpt_previous', 'lnpt_two_years',
            'training_experience', 'awards', 'additional_info',
        ]
        widgets = {
            'full_name':           forms.TextInput(attrs={'class': 'form-control'}),
            'ic_number':           forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 850101-01-1234'}),
            'email':               forms.EmailInput(attrs={'class': 'form-control'}),
            'whatsapp_number':     forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 012-345 6789',
                                                          'inputmode': 'tel', 'autocomplete': 'tel'}),
            'current_grade':       forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. DG48'}),
            'school_name':         forms.TextInput(attrs={'class': 'form-control'}),
            'school_leader_name':  forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Pengetua / Guru Besar'}),
            'school_leader_email': forms.EmailInput(attrs={'class': 'form-control'}),
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
                                       'placeholder': 'List awards you have received, e.g. Anugerah Guru Inovatif 2024. Annual recognitions go in section 6.'}),
            'additional_info':     forms.Textarea(attrs={'class': 'form-control', 'rows': 3,
                                       'placeholder': 'Any additional information that supports your application (optional).'}),
        }
        labels = {
            'full_name':           'Full Name',
            'ic_number':           'Identity Card (IC) Number',
            'email':               'Email Address',
            'whatsapp_number':     'WhatsApp Number',
            'current_grade':       'Current Grade',
            'school_name':         'School Name',
            'school_leader_name':  "School Leader's Name",
            'school_leader_email': "School Leader's Email",
            'district':            'District',
            'state':               'State',
            'tech_track':          'Preferred Technology Track',
            'certifications':      'Professional Certifications',
            'previous_gpgd':       'Previous GPGD Experience (if applicable)',
            'lnpt_current':        'Latest LNPT Score',
            'lnpt_previous':       'LNPT Score (Previous Year)',
            'lnpt_two_years':      'LNPT Score (Two Years Ago)',
            'training_experience': 'Training / Coaching / Mentoring Experience',
            'awards':              'Awards (one-time achievements)',
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
    COUNT_FIELDS = ['num_teachers', 'num_students', 'num_school_leaders', 'num_others']

    class Meta:
        model = ActivityReport
        fields = [
            'training_title', 'training_date', 'start_time', 'end_time', 'hours',
            'num_teachers', 'num_students', 'num_school_leaders', 'num_others', 'training_mode', 'venue_platform',
            'description', 'photo_1', 'photo_2', 'supporting_document',
        ]
        widgets = {
            'training_title':   forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Digital Storytelling with Google Slides'}),
            'training_date':    forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'start_time':       forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'end_time':         forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'hours':            forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5', 'min': 0, 'max': 24}),
            'num_teachers':     forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'num_students':     forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'num_school_leaders': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'num_others':       forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
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
            'num_teachers':        'Teachers',
            'num_students':        'Students',
            'num_school_leaders':  'School Leaders',
            'num_others':          'Others',
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
        if not any(cleaned_data.get(f) for f in self.COUNT_FIELDS):
            self.add_error('num_teachers', 'Enter how many people attended (at least one group).')
        return cleaned_data
