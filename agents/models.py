from django.conf import settings
from django.db import models


class OfficerProfile(models.Model):
    ROLE_CHOICES = [
        ('state_officer', 'State Officer'),
        ('moe_officer', 'MoE Officer'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='officerprofile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    state = models.CharField(max_length=100, blank=True, help_text='Required for State Officers; leave blank for MoE Officer.')

    def __str__(self):
        return f"{self.user.get_username()} ({self.get_role_display()}{f' — {self.state}' if self.state else ''})"


class Provider(models.Model):
    name = models.CharField(max_length=100, unique=True)
    display_name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.display_name

    class Meta:
        ordering = ['name']


class CertificationRule(models.Model):
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name='certification_rules')
    canonical_name = models.CharField(max_length=255, help_text='e.g. Microsoft Innovative Educator Expert (MIEE)')
    required_keywords = models.JSONField(
        default=list,
        help_text='All keywords must appear in the combined programme+level text (lowercase). '
                  'e.g. ["innovative educator", "expert"]'
    )
    exclude_keywords = models.JSONField(
        default=list,
        help_text='If any of these appear in the text, this rule does NOT match. e.g. ["trainer"]'
    )
    is_eligible = models.BooleanField(default=True, help_text='Count toward GPGD eligibility?')
    priority = models.IntegerField(default=0, help_text='Higher number = checked first within the same provider.')

    def __str__(self):
        return f"{self.provider.name} — {self.canonical_name}"

    def matches(self, programme: str, level: str) -> bool:
        blob = f"{str(programme or '').strip().lower()} {str(level or '').strip().lower()}".strip()
        if any(kw in blob for kw in self.exclude_keywords):
            return False
        return all(kw in blob for kw in self.required_keywords)

    class Meta:
        ordering = ['provider', '-priority', 'canonical_name']


class FileUpload(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('valid', 'Valid'),
        ('invalid', 'Invalid'),
    ]

    upload_id = models.CharField(max_length=100, unique=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    file_name = models.CharField(max_length=255)
    provider = models.CharField(max_length=50, choices=[
        ('Google', 'Google'),
        ('Microsoft', 'Microsoft'),
        ('Apple', 'Apple'),
    ])
    file_path = models.CharField(max_length=500)
    validation_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    record_count = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    missing_values_map = models.JSONField(default=dict, blank=True)  # {row_num: [column_names]}
    normalized_file_path = models.CharField(max_length=500, blank=True)

    def __str__(self):
        return f"{self.provider} - {self.uploaded_at}"

    class Meta:
        ordering = ['-uploaded_at']


class Teacher(models.Model):
    ic_number = models.CharField(max_length=20, unique=True)
    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    school = models.CharField(max_length=255)
    state = models.CharField(max_length=100)
    provider = models.CharField(max_length=255)  # Can be "Google, Microsoft" for multi-certified
    certification = models.CharField(max_length=255, blank=True, default='')  # Recognised cert(s) held
    cert_level = models.CharField(max_length=100)
    cert_year = models.IntegerField()
    multi_certified = models.BooleanField(default=False)
    eligibility_status = models.CharField(max_length=50, default='pending')

    def __str__(self):
        return f"{self.full_name} ({self.ic_number})"

    class Meta:
        ordering = ['state', 'full_name']


class ProgrammeSettings(models.Model):
    """Singleton row holding programme-wide settings, e.g. the application submission deadline,
    and the user-editable content templates (invitation email, recognition letter, application
    form copy) — see agents/content_defaults.py for the fallback text used when a field is blank."""

    submission_deadline = models.DateField(null=True, blank=True)

    # --- Invitation email (sent to eligible teachers who haven't applied yet) ---
    invitation_email_subject = models.CharField(max_length=255, blank=True)
    invitation_email_body = models.TextField(blank=True)

    # --- Recognition letter (sent when the MoE Officer approves an application) ---
    recognition_letter_subject = models.CharField(max_length=255, blank=True)
    recognition_letter_body = models.TextField(blank=True)

    # --- Monthly reminder (sent to the current year's GPGDs in the 3rd week of each month) ---
    monthly_reminder_subject = models.CharField(max_length=255, blank=True)
    monthly_reminder_body = models.TextField(blank=True)
    reminder_day = models.PositiveSmallIntegerField(
        default=15, help_text='Day of the month the reminder goes out (15 = start of the 3rd week).')
    report_day = models.PositiveSmallIntegerField(
        default=7, help_text="Day of the month last month's statistics go to directors and leaders (7 = end of the 1st week).")

    # --- Application form copy (apply.html): section headings, field labels, help text ---
    apply_form_section_labels = models.JSONField(default=dict, blank=True)
    apply_form_field_labels = models.JSONField(default=dict, blank=True)
    apply_form_help_texts = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Programme settings (deadline: {self.submission_deadline or 'not set'})"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Application(models.Model):
    STATUS_CHOICES = [
        ('Application Submitted', 'Application Submitted'),
        ('Under Review', 'Under Review'),
        ('Compiled', 'Compiled — Awaiting State Officer'),
        ('State Approved', 'State Approved — Awaiting MoE Officer'),
        ('State Declined', 'State Declined'),
        ('Approved', 'Approved — Recognised'),
        ('Rejected', 'Rejected (MoE)'),
    ]

    TRACK_CHOICES = [
        ('Microsoft', 'Microsoft'),
        ('Google', 'Google'),
        ('Apple', 'Apple'),
    ]

    teacher = models.OneToOneField(Teacher, null=True, blank=True, on_delete=models.SET_NULL, related_name='application')
    reference_number = models.CharField(max_length=20, unique=True, editable=False)

    # Personal Information
    full_name = models.CharField(max_length=255)
    ic_number = models.CharField(max_length=20)
    email = models.EmailField()
    whatsapp_number = models.CharField(max_length=20, blank=True, help_text='Malaysian mobile number, stored as +60…')
    current_grade = models.CharField(max_length=50)
    school_name = models.CharField(max_length=255)
    school_leader_name = models.CharField(max_length=255, blank=True, help_text='Principal / headmaster (Pengetua / Guru Besar).')
    school_leader_email = models.EmailField(blank=True, help_text="Receives the monthly report on this GPGD's activities.")
    district = models.CharField(max_length=100)
    state = models.CharField(max_length=100)

    # Technology Track
    tech_track = models.CharField(max_length=50, choices=TRACK_CHOICES)

    # Professional Qualifications
    certifications = models.TextField(help_text='List all certifications with issuing organisation and year.')
    previous_gpgd = models.TextField(blank=True, help_text='Previous GPGD experience, if any.')

    # Performance Records
    lnpt_current = models.DecimalField(max_digits=5, decimal_places=2, help_text='Latest LNPT score.')
    lnpt_previous = models.DecimalField(max_digits=5, decimal_places=2, help_text='Previous year LNPT score.')
    lnpt_two_years = models.DecimalField(max_digits=5, decimal_places=2, help_text='Two years ago LNPT score.')

    # Professional Contributions
    training_experience = models.TextField(help_text='Training, coaching, mentoring, or sharing experience.')
    awards = models.TextField(blank=True, help_text='Awards, recognitions, or achievements.')

    # Annual national recognitions (see constants.ANNUAL_RECOGNITIONS): [{"name", "years", "source"}],
    # where source is "declared" (from the form) or "records" (a past GPGD found by ranking.compile_and_rank).
    recognitions = models.JSONField(default=list, blank=True)
    never_recognised = models.BooleanField(
        default=False, help_text='Declared on the form: has never held an annual national recognition.')

    # Additional
    additional_info = models.TextField(blank=True)

    submitted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Application Submitted')

    # Compilation / ranking (set by ranking.compile_and_rank)
    score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rank_in_state = models.PositiveIntegerField(null=True, blank=True)
    is_recommended = models.BooleanField(default=False, help_text='Auto-flagged to meet the district/state minimum quota for its technology track.')
    reserved_place = models.BooleanField(
        default=False, help_text='Recommended through the place reserved for a teacher never recognised before.')
    compiled_at = models.DateTimeField(null=True, blank=True)

    # State Officer decision
    state_decision_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    state_decision_at = models.DateTimeField(null=True, blank=True)
    state_remarks = models.TextField(blank=True)

    # MoE Officer decision
    moe_decision_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    moe_decision_at = models.DateTimeField(null=True, blank=True)
    moe_remarks = models.TextField(blank=True)

    # Recognition dashboard metadata — set once, at the moment MoE approves (see moe_review()).
    recognized_year = models.PositiveIntegerField(null=True, blank=True, help_text='Calendar year this GPGD was officially recognised.')
    award_category = models.CharField(max_length=100, blank=True, default='', help_text='Optional award/recognition category, e.g. "Excellence in Digital Leadership".')
    recognition_letter_sent_at = models.DateTimeField(null=True, blank=True, help_text='When the Letter of Recognition was emailed.')
    recognition_letter_error = models.TextField(blank=True, help_text='Why the last attempt to email the letter failed.')

    def save(self, *args, **kwargs):
        if not self.reference_number:
            import uuid
            self.reference_number = f"GPGD-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    @property
    def is_never_recognised(self):
        """Declared "never recognised" on the form, and no recognition found in our own records either."""
        return self.never_recognised and not self.recognitions

    def recognitions_display(self):
        """e.g. "Edufluencer KPM (2024, 2025); GPGD (2025, from records)"."""
        return '; '.join(
            f"{r['name']} ({r['years']}{', from records' if r.get('source') == 'records' else ''})"
            for r in self.recognitions
        )

    def __str__(self):
        return f"{self.reference_number} — {self.full_name}"

    class Meta:
        ordering = ['-submitted_at']


class MonthlyBriefing(models.Model):
    """A plain-language summary of one month's training activity, written by the Reporting Agent
    (agents/reporting_agent.py) for the BSTP Director (scope "Malaysia") or one State Director (scope =
    state). Only an Admin-approved briefing goes into the monthly report email."""
    STATUS_CHOICES = [('draft', 'Draft — awaiting Admin approval'), ('approved', 'Approved')]

    period = models.CharField(max_length=7, help_text='"YYYY-MM": the month it covers.')
    scope = models.CharField(max_length=100, help_text='"Malaysia" or a state.')
    summary = models.TextField()
    flags = models.JSONField(default=list, blank=True, help_text='Short "needs attention" points.')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    edited = models.BooleanField(default=False, help_text='Changed by the Admin after the agent wrote it.')
    agent_run = models.ForeignKey('AgentActivityLog', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    generated_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('period', 'scope')

    def __str__(self):
        return f"{self.period} {self.scope} ({self.status})"


class ReportRecipient(models.Model):
    """Who receives the monthly statistics email: the BSTP Director (national), a State Director (one
    state) or a District Education Lead (one PPD). School leaders come from each GPGD's application."""
    LEVEL_CHOICES = [('bstp', 'BSTP Director'), ('state', 'State Director'), ('district', 'District Education Lead')]

    level = models.CharField(max_length=10, choices=LEVEL_CHOICES)
    state = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField()

    class Meta:
        unique_together = ('level', 'state', 'district')

    def __str__(self):
        return f"{self.get_level_display()} {self.district or self.state} <{self.email}>"


class MonthlyReportLog(models.Model):
    """One row per monthly statistics email: makes sending idempotent, so a scheduled run that fires
    twice (or a manual "Send now") never emails the same person the same report twice."""
    period = models.CharField(max_length=7, help_text='"YYYY-MM": the month the report covers.')
    level = models.CharField(max_length=15)  # bstp, state, district, school_leader
    scope = models.CharField(max_length=255, blank=True, help_text='The state, district or school the report covers.')
    email = models.EmailField()
    success = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)
    sent_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('period', 'level', 'scope', 'email')
        ordering = ['-sent_at']


class TrackLimit(models.Model):
    """The most teachers that may be recognised for one technology track this year, in a whole state
    (district blank) or in one district (PPD) of it, e.g. a limit set by the provider. Set by the Admin
    or MoE Officer; no row means no limit. An approval must fit both the state and the district maximum
    (agents/ranking.py:limit_reached)."""

    state = models.CharField(max_length=100)
    district = models.CharField(max_length=100, blank=True, default='', help_text='Blank = the whole state.')
    tech_track = models.CharField(max_length=50, choices=Application.TRACK_CHOICES)
    maximum = models.PositiveIntegerField()
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('state', 'district', 'tech_track')

    def __str__(self):
        return f"{self.district or self.state} — {self.tech_track}: max {self.maximum}"


class AgentActivityLog(models.Model):
    """Audit trail for the AI agents (pipeline and reporting) — one row per run."""

    STATUS_CHOICES = [
        ('running', 'Running'),
        ('success', 'Success'),
        ('error', 'Error'),
    ]

    triggered_at = models.DateTimeField(auto_now_add=True)
    trigger_reason = models.CharField(max_length=255)
    summary = models.TextField(blank=True, help_text="The agent's own plain-language account of what it did.")
    actions_taken = models.JSONField(default=list, blank=True, help_text='Ordered list of {tool, input, result}.')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='success')
    error_message = models.TextField(blank=True)

    def __str__(self):
        return f"{self.triggered_at:%Y-%m-%d %H:%M} — {self.trigger_reason}"

    class Meta:
        ordering = ['-triggered_at']


class ErrorLog(models.Model):
    """Actionable error log shown on its own dashboard tab.

    Distinct from AgentActivityLog (which is a per-run summary for the AI pipeline
    agent): this is one row per concrete failure, classified so a non-technical
    programme officer can tell at a glance what broke and what to check — starting
    with email delivery failures, which is where this has bitten us so far.
    """

    CATEGORY_CHOICES = [
        ('email_auth', 'Email — Authentication Failed'),
        ('email_timeout', 'Email — Connection Timed Out'),
        ('email_connection', 'Email — Could Not Connect'),
        ('email_invalid_recipient', 'Email — Invalid Recipient Address'),
        ('email_other', 'Email — Other Error'),
    ]

    occurred_at = models.DateTimeField(auto_now_add=True)
    category = models.CharField(max_length=40, choices=CATEGORY_CHOICES)
    context = models.CharField(max_length=255, blank=True, help_text='e.g. recipient name and email address.')
    technical_detail = models.TextField(help_text='The raw exception message, for diagnosis.')
    suggested_action = models.TextField(help_text='Plain-language guidance on what to check or fix.')

    def __str__(self):
        return f"{self.occurred_at:%Y-%m-%d %H:%M} — {self.get_category_display()}"

    class Meta:
        ordering = ['-occurred_at']


class ActivityReport(models.Model):
    """One professional-development activity/training reported by a recognized GPGD.

    Submitted through the public monthly reporting form (agents/views.py:submit_activity_report),
    reached via a signed link emailed on the 1st of every month.
    """

    AUDIENCE_CHOICES = [
        ('teachers', 'Teachers'),
        ('students', 'Students'),
        ('school_leaders', 'School Leaders'),
        ('others', 'Others'),
    ]

    MODE_CHOICES = [
        ('physical', 'Physical'),
        ('online', 'Online'),
        ('hybrid', 'Hybrid'),
    ]

    gpgd = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name='activity_reports',
        limit_choices_to={'status': 'Approved'},
    )

    training_title = models.CharField(max_length=255)
    training_date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    hours = models.DecimalField(max_digits=5, decimal_places=2)
    target_audience = models.JSONField(default=list, help_text='Subset of: teachers, students, school_leaders, others.')
    num_participants = models.PositiveIntegerField(help_text='Total of the four counts below; set on save.')
    num_teachers = models.PositiveIntegerField(default=0)
    num_students = models.PositiveIntegerField(default=0)
    num_school_leaders = models.PositiveIntegerField(default=0)
    num_others = models.PositiveIntegerField(default=0)
    training_mode = models.CharField(max_length=20, choices=MODE_CHOICES)
    venue_platform = models.CharField(max_length=255, help_text='Venue name, or online platform used.')
    description = models.TextField(blank=True)

    photo_1 = models.ImageField(upload_to='activity_evidence/photos/%Y/%m/')
    photo_2 = models.ImageField(upload_to='activity_evidence/photos/%Y/%m/')
    supporting_document = models.FileField(upload_to='activity_evidence/documents/%Y/%m/', blank=True, null=True)

    submitted_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # The total and audience list follow from the per-audience counts.
        counts = {'teachers': self.num_teachers, 'students': self.num_students,
                  'school_leaders': self.num_school_leaders, 'others': self.num_others}
        self.num_participants = sum(counts.values())
        self.target_audience = [a for a, n in counts.items() if n]
        super().save(*args, **kwargs)

    def audience_display(self):
        counts = {'teachers': self.num_teachers, 'students': self.num_students,
                  'school_leaders': self.num_school_leaders, 'others': self.num_others}
        labels = dict(self.AUDIENCE_CHOICES)
        return ', '.join(f'{labels[a]} {n}' for a, n in counts.items() if n)

    def __str__(self):
        return f"{self.gpgd.full_name} — {self.training_title} ({self.training_date})"

    class Meta:
        ordering = ['-training_date', '-submitted_at']


class MonthlyReminderLog(models.Model):
    """One row per (application, period) — makes the monthly reminder job idempotent and auditable."""

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name='monthly_reminders')
    period = models.CharField(max_length=7, help_text='"YYYY-MM" — the month this reminder was for.')
    sent_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)

    def __str__(self):
        return f"{self.application.full_name} — {self.period} ({'sent' if self.success else 'failed'})"

    class Meta:
        ordering = ['-sent_at']
        unique_together = ('application', 'period')


class InvitationExclusion(models.Model):
    """A teacher the Admin removed from the eligible list before invitations were sent.

    The pipeline rebuilds the whole Teacher roster on every upload/delete, so the removal is
    recorded by IC number here and save_to_database() skips it — otherwise the teacher would
    reappear (and be invited) the next time any certification file changes.
    """
    ic_number = models.CharField(max_length=20, unique=True)
    full_name = models.CharField(max_length=255)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    removed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} ({self.ic_number})"

    class Meta:
        ordering = ['-removed_at']


class InvitationRecord(models.Model):
    """The latest invitation email attempt for a teacher, shown next to their name on the dashboard.

    Keyed by IC number (not a Teacher FK) for the same reason as InvitationExclusion: the
    pipeline rebuilds the Teacher roster on every upload, and the record must survive that.
    """
    STATUS_CHOICES = [('sending', 'Sending'), ('sent', 'Sent'), ('failed', 'Failed')]

    ic_number = models.CharField(max_length=20, unique=True)
    email = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    error_message = models.TextField(blank=True)
    sent_by = models.CharField(max_length=150, blank=True)
    attempted_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.ic_number} — {self.get_status_display()} ({self.attempted_at:%d %b %Y %H:%M})"
