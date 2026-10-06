"""Fills the demo database with synthetic data for an end-to-end walkthrough (see demo/README.md).

Only runs under mygpgd.settings_demo, so it can never write to the shared database:

    python manage.py seed_demo --settings=mygpgd.settings_demo              # ready to demo
    python manage.py seed_demo --fresh --settings=mygpgd.settings_demo      # empty, upload files live
    python manage.py seed_demo --applications --settings=mygpgd.settings_demo  # after a live upload

All names, IC numbers, emails and schools are fictitious.
"""
import io
import itertools
import os
import random
import shutil
import uuid
from datetime import date, time, timedelta
from decimal import Decimal

import pandas as pd
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from PIL import Image, ImageDraw

from agents import pipeline_ops
from agents.collector import highlight_missing_values, normalize_dataframe, save_normalized_file, validate_file
from agents.constants import MALAYSIA_STATES, districts_for
from agents.models import (
    ActivityReport, Application, FileUpload, InvitationRecord, OfficerProfile, ProgrammeSettings, ReportRecipient, Teacher,
)

DEMO_PASSWORD = 'Demo#2026'  # local demo database only — also listed in demo/README.md

# IC birthplace code per state. Every state takes part, and every one of its districts (the 143 PPDs;
# Perlis, Labuan and Putrajaya have none, so the state itself stands in for the district).
IC_CODES = {
    'Johor': '01', 'Kedah': '02', 'Kelantan': '03', 'Melaka': '04', 'Negeri Sembilan': '05', 'Pahang': '06',
    'Pulau Pinang': '07', 'Perak': '08', 'Perlis': '09', 'Selangor': '10', 'Terengganu': '11', 'Sabah': '12',
    'Sarawak': '13', 'W.P. Kuala Lumpur': '14', 'W.P. Labuan': '15', 'W.P. Putrajaya': '16',
}
TRACKS = ['Google', 'Microsoft', 'Apple']
# Certified teachers per district and track: varied, and sometimes none, so the minimum of 2 per
# track shows gaps in places. States without districts get more, as their minimum is 5 state-wide.
PER_DISTRICT_TRACK = [0] * 12 + [1] * 13 + [2] * 25 + [3] * 25 + [4] * 15 + [5] * 10
PER_STATE_TRACK = [3, 4, 5, 6, 7, 8]
NO_DISTRICT_TOWNS = {'Perlis': 'Kangar', 'W.P. Labuan': 'Labuan', 'W.P. Putrajaya': 'Putrajaya'}

# The presentation's story: Puan Aisyah, 18 years in a rural school, never recognised. She is not
# invited when the demo starts (the presenter's "Approve & Send" emails her) and never applies by
# herself (the presenter applies as her). Her group, PPD Gua Musang / Google, holds only her and three
# well-known, already-recognised teachers with higher scores, so Compile & Rank gives them the top
# place and her the place kept for teachers never recognised.
SPOTLIGHT_STATE, SPOTLIGHT_DISTRICT, SPOTLIGHT_TRACK = 'Kelantan', 'PPD Gua Musang', 'Google'
AISYAH = {'name': 'Aisyah binti Hassan', 'ic': '830514-03-5262', 'email': 'aisyah.hassan@moe-school.edu.my',
          'school': 'SK Pos Harmoni, Gua Musang', 'cert': ('Google Certified Educator', 'Level 2')}
WELL_KNOWN = [
    {'name': 'Mohd Faizal bin Yusof', 'ic': '790211-03-5117', 'email': 'faizal.yusof.gm@moe-school.edu.my',
     'school': 'SMK Gua Musang Jaya', 'cert': ('Google Certified Trainer', 'Trainer'), 'lnpt': 97,
     'recognitions': [{'name': 'Edufluencer KPM', 'years': '2024, 2025', 'source': 'declared'}]},
    {'name': 'Tan Mei Ling', 'ic': '810930-03-5390', 'email': 'tan.meiling.gm@moe-school.edu.my',
     'school': 'SMK Taman Gua Musang', 'cert': ('Google Certified Coach', 'Coach'), 'lnpt': 96,
     'recognitions': [{'name': 'Cikgu Juara Digital', 'years': '2025', 'source': 'declared'}]},
    {'name': 'Siti Hajar binti Omar', 'ic': '850707-03-5524', 'email': 'sitihajar.omar.gm@moe-school.edu.my',
     'school': 'SK Bandar Gua Musang', 'cert': ('Google Certified Trainer', 'Trainer'), 'lnpt': 95,
     'recognitions': [{'name': 'Pakar Jauhari Digital', 'years': '2023, 2024', 'source': 'declared'},
                      {'name': 'Edufluencer KPM', 'years': '2025', 'source': 'declared'}]},
]
WELL_KNOWN_BY_IC = {t['ic']: t for t in WELL_KNOWN}


def _town(district):
    """The place name used in school names: "PPD Batu Pahat" -> "Batu Pahat", "Perlis" -> "Kangar"."""
    return NO_DISTRICT_TOWNS.get(district) or district.removeprefix('PPD ').split(' / ')[0].split('/')[0]


MALAY_M = ['Ahmad', 'Mohd Faizal', 'Muhammad Hafiz', 'Azman', 'Khairul', 'Syafiq', 'Amirul', 'Zulkifli', 'Hakim', 'Iskandar',
           'Rizal', 'Firdaus', 'Faiz', 'Hazwan', 'Shahrul', 'Nazri', 'Ridzuan', 'Fadzil', 'Aiman', 'Hairi']
MALAY_F = ['Nurul Aisyah', 'Siti Khadijah', 'Nor Azlina', 'Farah Nadia', 'Aminah', 'Zarina', 'Hidayah', 'Liyana', 'Syazwani',
           'Rohana', 'Haslinda', 'Nadia', 'Aida', 'Suhaila', 'Fatimah', 'Nor Hayati', 'Marlina', 'Izzati', 'Wahida', 'Salmah']
MALAY_FATHERS = ['Ismail', 'Abdullah', 'Osman', 'Hassan', 'Yusof', 'Ibrahim', 'Rahman', 'Salleh', 'Ali', 'Hamzah', 'Kassim',
                 'Mahmud', 'Ahmad', 'Razak', 'Jusoh', 'Mamat', 'Idris', 'Zakaria', 'Omar', 'Daud']
# Sabah and Sarawak naming (Kadazan-Dusun, Iban, Bidayuh and others).
BORNEO_GIVEN = ['Jenny', 'Melvin', 'Clarence', 'Roselyn', 'Juliana', 'Benedict', 'Agnes', 'Felix', 'Doris', 'Rudy',
                'Magdalene', 'Jimmy', 'Florence', 'Wilfred', 'Lucy', 'Edwin']
BORNEO_FATHERS = ['Belawan', 'Gimbang', 'Majanil', 'Sipaun', 'Lajim', 'Jugah', 'Ngumbang', 'Lansing', 'Nyanggau', 'Sulang',
                  'Jabu', 'Masing', 'Entri', 'Mojigoh', 'Gantang', 'Linggi']
CHINESE = ['Tan Wei Ming', 'Lee Mei Ling', 'Lim Chee Keong', 'Wong Siew Lan', 'Ng Kok Wai', 'Chong Li Ying', 'Goh Boon Hock', 'Teh Hui Min', 'Ong Jia Hui', 'Yap Kar Mun',
            'Chan Mei Yee', 'Lau Kah Seng', 'Koh Pei Shan', 'Ho Wai Kit', 'Loh Siew Ping', 'Cheah Kim Leng', 'Foo Jun Wei', 'Liew Shu Fen', 'Soh Chin Huat', 'Kang Yee Ling']
INDIAN_M = ['Rajesh', 'Suresh', 'Kumar', 'Ganesh', 'Prakash', 'Vijay']
INDIAN_F = ['Kavitha', 'Priya', 'Devi', 'Lakshmi', 'Anitha', 'Shalini']
INDIAN_FATHERS = ['Subramaniam', 'Maniam', 'Ramasamy', 'Krishnan', 'Muthu', 'Raju']
SCHOOL_PREFIXES = ['SMK', 'SK', 'SMK Taman', 'SJK(C)', 'SK Taman', 'SMK Seri']
SCHOOL_NAMES = ['Indah', 'Jaya', 'Permai', 'Bestari', 'Mutiara', 'Cemerlang', 'Harmoni', 'Saujana', 'Melati', 'Delima', 'Bukit', 'Sentosa']

# (programme, level) pairs per provider — eligible ones match agents.management.commands.seed_providers.
ELIGIBLE_CERTS = {
    'Google': [('Google Certified Educator', 'Level 2'), ('Google Certified Trainer', 'Trainer'), ('Google Certified Coach', 'Coach')],
    'Microsoft': [('Microsoft Certified Educator', 'Educator'), ('Microsoft Innovative Educator Expert', 'Expert')],
    'Apple': [('Apple Teacher', 'Teacher'), ('Apple Learning Coach', 'Coach')],
}
INELIGIBLE_CERTS = {
    'Google': ('Google Certified Educator', 'Level 1'),
    'Microsoft': ('Microsoft Office Specialist', 'Specialist'),
    'Apple': ('Apple Professional Learning', 'Participant'),
}

TRAINING = [
    'Conducted Google Classroom workshops for 40 teachers in my district.',
    'In-house training on Microsoft Teams and OneNote for my school.',
    'Mentored 5 schools on iPad-based lessons as part of the PdPc programme.',
    'Shared best practices on digital assessment at the state-level carnival.',
    'Coached new teachers on blended learning every term since 2022.',
]
AWARDS = [
    'Anugerah Guru Inovatif peringkat negeri 2024',
    'Champion, Digital Teaching Innovation Competition 2023',
    'Excellent Service Award (APC) 2024',
    'Best Digital Lesson, district level 2023',
]
PREVIOUS_GPGD = ['Assisted the state GPGD team with training in 2024.', 'Facilitator for the district Digital Day 2023.']
AWARD_CATEGORIES = ['Excellence in Digital Leadership', 'Innovative Pedagogy', 'Community Impact']


class Command(BaseCommand):
    help = 'Reset the demo database and fill it with synthetic data (demo settings only).'

    def add_arguments(self, parser):
        parser.add_argument('--fresh', action='store_true',
                            help='Logins, rules and certification files only — upload the files live in the demo.')
        parser.add_argument('--applications', action='store_true',
                            help='Keep the current data and have most invited teachers submit an application.')

    def handle(self, *args, **options):
        if not getattr(settings, 'GPGD_DEMO', False):
            raise CommandError('seed_demo only runs with the demo settings: add --settings=mygpgd.settings_demo')
        self.rng = random.Random(2026)
        self.demo_dir = settings.DEMO_DIR

        if options['applications']:
            self._invite_and_apply()
            if not Application.objects.filter(status='Approved').exists():
                self._previous_cohort()  # last year's GPGDs and their activity reports, for the monthly reports
        else:
            self._reset()
            files = self._write_certification_files()
            if not options['fresh']:
                for provider, path in files.items():
                    self._upload(provider, path)
                pipeline_ops.run_certification_pipeline()
                self._invite_and_apply()
                self._previous_cohort()
        self._write_links()
        self._summary()

    # --- setup -------------------------------------------------------------

    def _reset(self):
        call_command('flush', interactive=False, verbosity=0)
        for folder in ('uploads', 'sent_emails', 'media', 'files'):
            shutil.rmtree(self.demo_dir / folder, ignore_errors=True)
            os.makedirs(self.demo_dir / folder, exist_ok=True)
        call_command('seed_providers', verbosity=0, stdout=io.StringIO())

        User.objects.create_superuser('admin', 'admin@example.com', DEMO_PASSWORD)
        moe = User.objects.create_user('moe_officer', password=DEMO_PASSWORD, first_name='MoE Officer')
        OfficerProfile.objects.create(user=moe, role='moe_officer')
        for state in MALAYSIA_STATES:
            username = state.lower().replace('w.p. ', 'wp_').replace(' ', '_') + '_officer'
            user = User.objects.create_user(username, password=DEMO_PASSWORD, first_name=f'{state} Officer')
            OfficerProfile.objects.create(user=user, role='state_officer', state=state)

        programme = ProgrammeSettings.load()
        programme.submission_deadline = timezone.localdate() + timedelta(days=14)
        programme.save()

        # Every leader gets the monthly report: BSTP, all 16 State Directors and all 143 PPD leads.
        def slug(text):
            return ''.join(ch for ch in text.lower() if ch.isalnum())
        ReportRecipient.objects.create(level='bstp', name='Pengarah BSTP', email='pengarah.bstp@demo.example')
        for state in MALAYSIA_STATES:
            ReportRecipient.objects.create(level='state', state=state, name=f'Pengarah JPN {state}',
                                           email=f'pengarah.jpn.{slug(state)}@demo.example')
            for district in districts_for(state):
                if district.startswith('PPD '):
                    ReportRecipient.objects.create(level='district', state=state, district=district,
                                                   name=f'Pegawai {district}', email=f'ketua.{slug(district)}@demo.example')

    def _name(self, state=''):
        if state in ('Sabah', 'Sarawak') and self.rng.random() < 0.55:
            joiner = 'anak' if state == 'Sarawak' else self.rng.choice(['anak', 'binti', 'bin'])
            return f'{self.rng.choice(BORNEO_GIVEN)} {joiner} {self.rng.choice(BORNEO_FATHERS)}'
        roll = self.rng.random()
        if roll < 0.55:
            if self.rng.random() < 0.5:
                return f'{self.rng.choice(MALAY_M)} bin {self.rng.choice(MALAY_FATHERS)}'
            return f'{self.rng.choice(MALAY_F)} binti {self.rng.choice(MALAY_FATHERS)}'
        if roll < 0.8:
            return self.rng.choice(CHINESE)
        if self.rng.random() < 0.5:
            return f'{self.rng.choice(INDIAN_M)} a/l {self.rng.choice(INDIAN_FATHERS)}'
        return f'{self.rng.choice(INDIAN_F)} a/p {self.rng.choice(INDIAN_FATHERS)}'

    def _people(self):
        """Synthetic certified teachers for every district (PPD) of every state: name, IC, email, school,
        state, district, and the certifications they hold."""
        people, used_ics, used_emails = [], set(), set()

        def person(state, district, track):
            name = self._name(state)
            ic = None
            while ic is None or ic in used_ics:
                birth = date(1975, 1, 1) + timedelta(days=self.rng.randrange(0, 365 * 22))
                ic = f"{birth:%y%m%d}-{IC_CODES[state]}-{self.rng.randrange(1000, 9999)}"
            used_ics.add(ic)
            base = ''.join(ch for ch in name.lower().replace(' a/l ', '.').replace(' a/p ', '.').replace(' bin ', '.')
                           .replace(' binti ', '.').replace(' anak ', '.') if ch.isalnum() or ch == '.')
            email = next(f'{base}{n or ""}@moe-school.edu.my' for n in itertools.count() if f'{base}{n or ""}' not in used_emails)
            used_emails.add(email.split('@')[0])
            return {'name': name, 'ic': ic, 'email': email, 'state': state, 'district': district,
                    'school': f"{self.rng.choice(SCHOOL_PREFIXES)} {self.rng.choice(SCHOOL_NAMES)} {_town(district)}",
                    'certs': [(track, *self.rng.choice(ELIGIBLE_CERTS[track]))]}

        for state in MALAYSIA_STATES:
            counts = PER_STATE_TRACK if state in NO_DISTRICT_TOWNS else PER_DISTRICT_TRACK
            for district in districts_for(state):
                for track in TRACKS:
                    people += [person(state, district, track) for _ in range(self.rng.choice(counts))]
                # Some teachers in each district hold only a certification that doesn't qualify.
                for _ in range(self.rng.choice([0, 0, 1])):
                    track = self.rng.choice(TRACKS)
                    teacher = person(state, district, track)
                    teacher['certs'] = [(track, *INELIGIBLE_CERTS[track])]
                    people.append(teacher)

        for teacher in people:  # about 1 in 8 is also certified by a second provider
            provider = teacher['certs'][0][0]
            if teacher['certs'][0][1:] != INELIGIBLE_CERTS[provider] and self.rng.random() < 0.12:
                second = self.rng.choice([t for t in TRACKS if t != provider])
                if not (teacher['district'] == SPOTLIGHT_DISTRICT and second == SPOTLIGHT_TRACK):
                    teacher['certs'].append((second, *self.rng.choice(ELIGIBLE_CERTS[second])))

        fixed = [AISYAH, *WELL_KNOWN]
        fixed_ics = {t['ic'] for t in fixed}
        people = [p for p in people if p['ic'] not in fixed_ics and not (
            p['district'] == SPOTLIGHT_DISTRICT and any(c[0] == SPOTLIGHT_TRACK for c in p['certs']))]
        people += [{'name': t['name'], 'ic': t['ic'], 'email': t['email'], 'school': t['school'],
                    'state': SPOTLIGHT_STATE, 'district': SPOTLIGHT_DISTRICT, 'certs': [(SPOTLIGHT_TRACK, *t['cert'])]}
                   for t in fixed]
        self.district_by_ic = {p['ic']: p['district'] for p in people}
        return people

    def _write_certification_files(self):
        """Writes one Excel file per provider, in that provider's own column layout, to demo/files/."""
        self.people = self._people()
        columns = {
            'Google': ['Full Name', 'IC_Number', 'Email', 'School_Name', 'State', 'Certification', 'Cert_Level', 'Cert_Year'],
            'Microsoft': ['Teacher_Name', 'MyKad', 'Teacher_Email', 'Institution', 'State_Name', 'Cert_Programme', 'Cert_Type', 'Year_Achieved'],
            'Apple': ['Teacher_Full_Name', 'IC_Number', 'Email_Address', 'School', 'State', 'Apple_Programme', 'Level', 'Year'],
        }
        rows = {p: [] for p in columns}
        for person in self.people:
            for provider, programme, level in person['certs']:
                rows[provider].append([person['name'], person['ic'], person['email'], person['school'],
                                       person['state'], programme, level, self.rng.randint(2021, 2025)])
        # Realistic data-quality gaps, highlighted in red by the upload validator.
        rows['Google'][4][2] = None   # a missing email: eligible, but can't be invited
        rows['Microsoft'][6][7] = None  # a missing certification year

        paths = {}
        for provider, data in rows.items():
            path = self.demo_dir / 'files' / f'Demo_{provider}_Certifications.xlsx'
            pd.DataFrame(data, columns=columns[provider]).to_excel(path, index=False)
            paths[provider] = path
        return paths

    def _upload(self, provider, source):
        """Same steps as the upload page (agents/views.py:upload_certifications), without the browser."""
        upload_id = uuid.uuid4().hex[:8]
        file_path = os.path.join(pipeline_ops.TEMP_DIR, f'{provider}_{upload_id}.xlsx')
        os.makedirs(pipeline_ops.TEMP_DIR, exist_ok=True)
        shutil.copy(source, file_path)
        result = validate_file(file_path, provider)
        record = FileUpload.objects.create(
            upload_id=upload_id, file_name=os.path.basename(source), provider=provider, file_path=file_path,
            validation_status='valid' if result['is_valid'] else 'invalid', record_count=result['record_count'],
            error_message=result['error_message'] or '', missing_values_map=result['missing_values_map'],
        )
        if result['missing_values_map']:
            highlight_missing_values(file_path, os.path.join(pipeline_ops.TEMP_DIR, f'{provider}_highlighted_{upload_id}.xlsx'),
                                     result['missing_values_map'])
        normalized = normalize_dataframe(result['dataframe'], provider)
        record.normalized_file_path = os.path.join(pipeline_ops.TEMP_DIR, f'{provider}_normalized_{upload_id}.xlsx')
        save_normalized_file(normalized, record.normalized_file_path)
        record.save()

    # --- applications --------------------------------------------------------

    def _district_of(self, teacher):
        """The district the teacher was generated in, or (after a live upload) the one whose town ends
        their school's name."""
        known = getattr(self, 'district_by_ic', {}).get(teacher.ic_number)
        districts = districts_for(teacher.state) or ['Not stated']
        return known or next((d for d in districts if teacher.school.endswith(_town(d))), districts[0])

    @transaction.atomic
    def _invite_and_apply(self):
        """Marks most eligible teachers as invited and has most of those apply.

        Left for the live demo: a few teachers not yet invited (for "Approve & Send"), and a few
        invited teachers who haven't applied (open their link from demo/LINKS.md to apply live).
        """
        eligible = list(Teacher.objects.filter(eligibility_status='Eligible').order_by('ic_number'))
        if not eligible:
            raise CommandError('No eligible teachers yet — upload the three files in demo/files/ first.')
        with_email = [t for t in eligible if t.email and t.email.lower() != 'nan']
        others = [t for t in with_email if t.ic_number != AISYAH['ic']]
        # Puan Aisyah is invited live (Approve & Send) and applies live, so she is left out of both.
        not_invited = {t.ic_number for t in others[-3:]} | {AISYAH['ic']}
        # Keep one multi-certified teacher unapplied, to show the technology-track choice live.
        multi = next((t for t in others if t.multi_certified and t.ic_number not in not_invited), None)
        not_applied = {t.ic_number for t in others[:3]} | ({multi.ic_number} if multi else set()) | {AISYAH['ic']}


        for teacher in with_email:
            if teacher.ic_number in not_invited:
                continue
            InvitationRecord.objects.update_or_create(
                ic_number=teacher.ic_number, defaults={'status': 'sent', 'email': teacher.email, 'sent_by': 'admin'})
            if teacher.ic_number in not_applied or hasattr(teacher, 'application'):
                continue
            if teacher.ic_number in WELL_KNOWN_BY_IC:
                self._well_known_application(teacher, WELL_KNOWN_BY_IC[teacher.ic_number])
                continue
            tracks = [p.strip() for p in teacher.provider.split(',')]
            if self._district_of(teacher) == SPOTLIGHT_DISTRICT and len(tracks) > 1:
                tracks = [t for t in tracks if t != SPOTLIGHT_TRACK]  # keep Puan Aisyah's group to the four
            Application.objects.create(
                teacher=teacher, full_name=teacher.full_name, ic_number=teacher.ic_number, email=teacher.email,
                whatsapp_number=f"+601{self.rng.choice('0123456789')}{self.rng.randrange(1000000, 9999999)}",
                current_grade=self.rng.choice(['DG41', 'DG44', 'DG48', 'DG52']), school_name=teacher.school,
                **self._school_leader(teacher.school),
                district=self._district_of(teacher), state=teacher.state,
                tech_track=self.rng.choice(tracks),
                certifications=teacher.certification,
                previous_gpgd=self.rng.choice(PREVIOUS_GPGD) if self.rng.random() < 0.3 else '',
                lnpt_current=self._lnpt(), lnpt_previous=self._lnpt(), lnpt_two_years=self._lnpt(),
                training_experience=self.rng.choice(TRAINING) if self.rng.random() < 0.8 else '',
                awards=self.rng.choice(AWARDS) if self.rng.random() < 0.4 else '',
                **self._recognitions(),
            )
            teacher.eligibility_status = 'Application Submitted'
            teacher.save(update_fields=['eligibility_status'])

    def _well_known_application(self, teacher, profile):
        """One of the three visible, already-recognised teachers in Puan Aisyah's group: strong scores."""
        Application.objects.create(
            teacher=teacher, full_name=teacher.full_name, ic_number=teacher.ic_number, email=teacher.email,
            whatsapp_number=f"+6019{self.rng.randrange(1000000, 9999999)}", current_grade='DG52',
            school_name=teacher.school, **self._school_leader(teacher.school), district=SPOTLIGHT_DISTRICT,
            state=SPOTLIGHT_STATE, tech_track=SPOTLIGHT_TRACK, certifications=teacher.certification,
            previous_gpgd='State-level facilitator for digital learning programmes since 2022.',
            lnpt_current=profile['lnpt'], lnpt_previous=profile['lnpt'], lnpt_two_years=profile['lnpt'] - 1,
            training_experience=self.rng.choice(TRAINING), awards=self.rng.choice(AWARDS),
            never_recognised=False, recognitions=profile['recognitions'],
        )
        teacher.eligibility_status = 'Application Submitted'
        teacher.save(update_fields=['eligibility_status'])

    def _recognitions(self):
        """About a third have never held an annual national recognition; the rest hold one or two."""
        if self.rng.random() < 0.33:
            return {'never_recognised': True, 'recognitions': []}
        names = self.rng.sample(['Edufluencer KPM', 'Pakar Jauhari Digital', 'Cikgu Juara Digital'], self.rng.choice([1, 1, 2]))
        return {'never_recognised': False, 'recognitions': [
            {'name': n, 'years': ', '.join(sorted(self.rng.sample(['2023', '2024', '2025'], self.rng.choice([1, 2])))),
             'source': 'declared'} for n in names]}

    def _school_leader(self, school):
        """The same (fictitious) school leader for every GPGD at one school."""
        slug = ''.join(ch for ch in school.lower() if ch.isalnum())
        return {'school_leader_name': f'Pengetua {school}', 'school_leader_email': f'pengetua.{slug}@school.example'}

    def _lnpt(self):
        return Decimal(self.rng.randint(7400, 9800)) / 100

    @transaction.atomic
    def _previous_cohort(self):
        """Last year's recognised GPGDs, with activity reports, so the Recognition and Activity
        dashboards have something to show before this year's cohort is approved."""
        moe = User.objects.get(username='moe_officer')
        # Three of this year's applicants were GPGDs last year: compiling finds them in the records,
        # even though one of them ticked "never recognised" on the form.
        returning = list(Application.objects.filter(state='Johor', status='Application Submitted').order_by('ic_number')[:3])
        returning[0].never_recognised, returning[0].recognitions = True, []
        returning[0].save(update_fields=['never_recognised', 'recognitions'])
        for applicant in returning:
            Application.objects.create(
                full_name=applicant.full_name, ic_number=applicant.ic_number, email=applicant.email,
                current_grade=applicant.current_grade, school_name=applicant.school_name, district=applicant.district,
                **self._school_leader(applicant.school_name),
                state=applicant.state, tech_track=applicant.tech_track, certifications=applicant.certifications,
                lnpt_current=88, lnpt_previous=87, lnpt_two_years=86, training_experience=self.rng.choice(TRAINING),
                status='Approved', recognized_year=2025, award_category=self.rng.choice(AWARD_CATEGORIES),
                moe_decision_by=moe, moe_decision_at=timezone.now() - timedelta(days=300),
            )
        count = 0
        for state in MALAYSIA_STATES:
            for district in districts_for(state):
                for _ in range(self.rng.choice([0, 1, 1, 2])):
                    count += 1
                    school = f"{self.rng.choice(SCHOOL_PREFIXES)} {self.rng.choice(SCHOOL_NAMES)} {_town(district)}"
                    gpgd = Application.objects.create(
                        full_name=self._name(state), email=f"gpgd2025.{count}@moe-school.edu.my", current_grade='DG48',
                        ic_number=f"8{self.rng.randrange(10000, 99999)}-{IC_CODES[state]}-{self.rng.randrange(1000, 9999)}",
                        school_name=school, **self._school_leader(school), district=district, state=state,
                        tech_track=self.rng.choice(TRACKS), certifications='See 2025 cohort records',
                        lnpt_current=90, lnpt_previous=88, lnpt_two_years=87, training_experience=self.rng.choice(TRAINING),
                        status='Approved', recognized_year=2025, award_category=self.rng.choice(AWARD_CATEGORIES),
                        moe_decision_by=moe, moe_decision_at=timezone.now() - timedelta(days=300),
                    )
                    # Most report something; a few never do.
                    for month in range(self.rng.choice([0, 1, 2, 3, 4, 4])):
                        self._activity_report(gpgd, month)

    def _activity_report(self, gpgd, months_ago):
        day = timezone.localdate().replace(day=1) - timedelta(days=30 * months_ago + self.rng.randint(1, 20))
        report = ActivityReport(
            gpgd=gpgd, training_title=f"{gpgd.tech_track} for Teaching and Learning — {gpgd.district}",
            training_date=day, start_time=time(9), end_time=time(12), hours=Decimal('3.00'),
            num_teachers=self.rng.choice([0, self.rng.randint(8, 40)]), num_students=self.rng.choice([0, self.rng.randint(20, 120)]),
            num_school_leaders=self.rng.choice([0, 0, self.rng.randint(1, 6)]), num_others=self.rng.choice([0, 0, self.rng.randint(1, 10)]),
            training_mode=self.rng.choice(['physical', 'online', 'hybrid']),
            venue_platform=self.rng.choice([gpgd.school_name, 'Google Meet', 'Microsoft Teams', gpgd.district]),
            description='Hands-on session (synthetic demo record).',
        )
        if not (report.num_teachers or report.num_students):
            report.num_teachers = self.rng.randint(8, 40)
        report.photo_1.name = self._photo_path(gpgd.tech_track, 1)
        report.photo_2.name = self._photo_path(gpgd.tech_track, 2)
        report.save()

    def _photo_path(self, track, n):
        """One stored evidence photo per track and number, shared by every demo report."""
        if not hasattr(self, 'photo_paths'):
            self.photo_paths = {}
        if (track, n) not in self.photo_paths:
            self.photo_paths[(track, n)] = default_storage.save(
                f'activity_evidence/demo/{track.lower()}_{n}.png', self._photo(track, n))
        return self.photo_paths[(track, n)]

    def _photo(self, label, n):
        colours = {'Google': (66, 133, 244), 'Microsoft': (0, 120, 212), 'Apple': (85, 85, 85)}
        image = Image.new('RGB', (480, 320), colours.get(label, (100, 100, 100)))
        ImageDraw.Draw(image).text((20, 20), f'Demo evidence photo {n} — {label}', fill=(255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        return ContentFile(buffer.getvalue())

    # --- output ------------------------------------------------------------

    def _write_links(self):
        """demo/LINKS.md: application links for invited teachers who haven't applied yet."""
        site = settings.GPGD_SITE_URL
        lines = ['# Demo application links', '',
                 'Teachers who were invited but have not applied yet. Open a link to fill in the form as that teacher.',
                 'Regenerated each time `seed_demo` runs.', '']
        aisyah = Teacher.objects.filter(ic_number=AISYAH['ic'], eligibility_status='Eligible').first()
        if aisyah:
            lines += [
                '## Puan Aisyah (for the presentation)', '',
                f"**{aisyah.full_name}**, {AISYAH['school']}, {SPOTLIGHT_DISTRICT}, {SPOTLIGHT_STATE}. "
                'Her invitation arrives in the Demo Inbox when you click Approve & Send; this is the same link:  ',
                f"{site}/agents/apply/{pipeline_ops._make_apply_token(aisyah.ic_number)}/", '',
                'Suggested answers on the form:', '',
                '- WhatsApp: 013-456 7890 · Current grade: DG44',
                "- School leader: Guru Besar SK Pos Harmoni · gurubesar.posharmoni@school.example",
                f'- State: {SPOTLIGHT_STATE} · District: {SPOTLIGHT_DISTRICT} · Track: Google (preselected)',
                '- LNPT: 92 / 90 / 91',
                '- Training experience: 18 years in a rural school; coaches students after school, builds her own '
                'digital learning materials, and trains teachers across PPD Gua Musang.',
                '- Awards: leave blank',
                '- Annual national recognitions: tick "I have never received any annual national-level recognition"', '',
                'After Compile & Rank, log in as `kelantan_officer`: in State Review, filter to PPD Gua Musang. '
                'She holds the Recommended place kept for teachers never recognised, beside three well-known teachers.',
                '', '## Other invited teachers who have not applied', '',
            ]
        invited = set(InvitationRecord.objects.filter(status='sent').values_list('ic_number', flat=True))
        for teacher in Teacher.objects.filter(eligibility_status='Eligible', ic_number__in=invited).exclude(
                ic_number=AISYAH['ic']).order_by('state'):
            note = ' — multi-certified, must choose a technology track' if teacher.multi_certified else ''
            lines.append(f"- **{teacher.full_name}** ({teacher.state}, {teacher.provider}){note}  ")
            lines.append(f"  {site}/agents/apply/{pipeline_ops._make_apply_token(teacher.ic_number)}/")
        (self.demo_dir / 'LINKS.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def _summary(self):
        self.stdout.write(self.style.SUCCESS('Demo data ready.'))
        self.stdout.write(f"  Certification files: {FileUpload.objects.count()} uploaded (source files in demo/files/)")
        self.stdout.write(f"  Teachers: {Teacher.objects.count()} ({Teacher.objects.filter(eligibility_status='Not Eligible').count()} not eligible)")
        self.stdout.write(f"  Applications this year: {Application.objects.exclude(status='Approved').count()}")
        self.stdout.write(f"  Last year's GPGDs: {Application.objects.filter(status='Approved').count()}")
        self.stdout.write(f"  Logins and walkthrough: demo/README.md   Application links: demo/LINKS.md")
