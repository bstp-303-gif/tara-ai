"""Default text for the user-editable content templates: invitation email,
recognition letter, and application form copy (see ProgrammeSettings +
agents/views.py edit_invitation_email / edit_recognition_letter / edit_application_form).

Editing those settings pages overrides these; leaving a field blank falls back
to the default here, so nothing breaks if something is left un-edited.
"""

DEFAULT_INVITATION_SUBJECT = "Invitation to Apply — Guru Peneraju Generasi Digital (GPGD) Programme"

DEFAULT_INVITATION_BODY = """Dear {{full_name}},

Congratulations! We are pleased to inform you that, based on the selection criteria set by the Ministry of Education Malaysia (MOE), you have been shortlisted as an eligible candidate for the Guru Peneraju Generasi Digital (GPGD) Programme.

Your dedication to integrating digital technology into teaching and learning has been recognised, and we believe you have the potential to become a digital leader who inspires and supports fellow educators.

What is Guru Peneraju Generasi Digital (GPGD)?
Guru Peneraju Generasi Digital (GPGD) is a Ministry of Education initiative that develops a network of highly competent teachers to lead and promote the effective integration of digital technology in education. GPGD members serve as digital ambassadors who support schools, districts, and states in strengthening digital competencies and transforming teaching and learning practices.

Responsibilities of a GPGD
As a GPGD, you will be expected to:
• Participate in professional development and certification programmes organised by MOE and its strategic technology partners.
• Conduct training, mentoring, coaching, and knowledge-sharing sessions for fellow educators (a minimum of 3 times a year) and record it in the given dashboard.
• Promote the effective and responsible use of digital technologies in teaching, learning, and school management.
• Share innovative teaching practices and contribute to the digital education community.
• Support MOE's digital education initiatives and programmes at the school, district, state, and national levels.
• Continuously enhance your digital competencies and serve as a role model for other educators.

If you are interested in becoming a Guru Peneraju Generasi Digital (GPGD), kindly complete the online application form using the secure link below on or before {{deadline}}:

Apply here: {{apply_url}}

Please complete your application before {{deadline}}. Only complete submissions received before the closing date will be considered for the next stage of the selection process.

We look forward to welcoming passionate educators like you into the GPGD community as we continue to empower teachers and transform digital education together.

Thank you.

Yours sincerely,
Sektor Pengintegrasian Teknologi Pendidikan (SPTP)
Bahagian Sumber dan Teknologi Pendidikan (BSTP)
Kementerian Pendidikan Malaysia
"""

INVITATION_PLACEHOLDERS = ['full_name', 'apply_url', 'deadline']

DEFAULT_RECOGNITION_SUBJECT = "Letter of Recognition — Guru Peneraju Generasi Digital (GPGD) Programme"

DEFAULT_RECOGNITION_BODY = """Dear {{full_name}},

Congratulations! We are pleased to inform you that your application for the Guru Peneraju Generasi Digital (GPGD) Programme, Reference Number {{reference_number}}, has been approved by the Ministry of Education Malaysia (MOE).

You have been officially recognised as a Guru Peneraju Generasi Digital (GPGD). This recognition reflects your commitment to digital excellence in teaching and learning, and your readiness to lead and support fellow educators in your school, district, and state.

Further details on your responsibilities and upcoming engagements as a GPGD will be communicated to you separately.

Congratulations once again, and thank you for your dedication to digital education.

Yours sincerely,
Sektor Pengintegrasian Teknologi Pendidikan (SPTP)
Bahagian Sumber dan Teknologi Pendidikan (BSTP)
Kementerian Pendidikan Malaysia
"""

RECOGNITION_PLACEHOLDERS = ['full_name', 'reference_number']

DEFAULT_MONTHLY_REMINDER_SUBJECT = "Reminder: Report Your GPGD Activities for {{month}}"

DEFAULT_MONTHLY_REMINDER_BODY = """Dear {{full_name}},

This is your monthly reminder to report the activities you have conducted this month ({{month}}) as a Guru Peneraju Generasi Digital (GPGD): training, mentoring, coaching, or knowledge-sharing sessions.

Please submit one report for each activity using your secure link below, before the end of the month:

Report here: {{report_url}}

For each activity you will need: the title, date, time, number of hours, the number of teachers, students, school leaders and others who attended, the training mode and venue, a brief description, and two evidence photos (a supporting document is optional).

Your reports are summarised every month for the BSTP Director, your State Education Department, your District Education Office and your school leader, so please report every activity.

Thank you for your continued contribution to digital education in Malaysia.

Yours sincerely,
Sektor Pengintegrasian Teknologi Pendidikan (SPTP)
Bahagian Sumber dan Teknologi Pendidikan (BSTP)
Kementerian Pendidikan Malaysia
"""

MONTHLY_REMINDER_PLACEHOLDERS = ['full_name', 'month', 'report_url']

# Ordered so the settings page renders sections/fields in the same order as the form.
DEFAULT_APPLY_SECTION_LABELS = {
    'section_personal': '1. Personal Information',
    'section_tech_track': '2. Preferred Technology Track',
    'section_qualifications': '3. Professional Qualifications',
    'section_performance': '4. Performance Records',
    'section_contributions': '5. Professional Contributions',
    'section_recognitions': '6. Annual National Recognitions',
    'section_additional': '7. Additional Information',
}

DEFAULT_APPLY_FIELD_LABELS = {
    'full_name': 'Full Name',
    'ic_number': 'Identity Card (IC) Number',
    'email': 'Email Address',
    'whatsapp_number': 'WhatsApp Number',
    'school_leader_name': "School Leader's Name",
    'school_leader_email': "School Leader's Email",
    'current_grade': 'Current Grade',
    'school_name': 'School Name',
    'district': 'District',
    'state': 'State',
    'tech_track': 'Preferred Technology Track',
    'certifications': 'Professional Certifications',
    'previous_gpgd': 'Previous GPGD Experience (if applicable)',
    'lnpt_current': 'Latest LNPT Score',
    'lnpt_previous': 'LNPT Score (Previous Year)',
    'lnpt_two_years': 'LNPT Score (Two Years Ago)',
    'training_experience': 'Training / Coaching / Mentoring Experience',
    'awards': 'Awards (one-time achievements)',
    'additional_info': 'Additional Information (Optional)',
}

DEFAULT_APPLY_HELP_TEXTS = {
    'certifications_help': 'List all relevant certifications, including the issuing organisation and year obtained.',
    'submit_note': 'By submitting this form, you confirm that all information provided is accurate and complete.',
    'submit_button': 'Submit Application',
}


def render_placeholders(text, **kwargs):
    """Replace {{key}} tokens in text with the given values."""
    for key, value in kwargs.items():
        text = text.replace('{{' + key + '}}', str(value))
    return text
