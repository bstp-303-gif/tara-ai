from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Application

# Weighted blend used to score each compiled application (0-100 scale).
# Adjust these to retune ranking priorities; they should sum to 1.0.
WEIGHTS = {
    'lnpt': Decimal('0.35'),
    'certification': Decimal('0.25'),
    'experience': Decimal('0.20'),
    'awards': Decimal('0.10'),
    'multi_cert': Decimal('0.10'),
}

# States with no district subdivisions use a state-wide minimum instead of a
# per-district one.
NO_DISTRICT_STATES = {'Perlis', 'WP Putrajaya', 'WP Labuan'}
DISTRICT_MINIMUM_PER_TECH = 2
NO_DISTRICT_STATE_MINIMUM_PER_TECH = 5

PIPELINE_ENTRY_STATUSES = ['Application Submitted', 'Under Review']
COMPILED_PIPELINE_STATUSES = ['Compiled', 'State Approved', 'State Declined']


def _normalize(value):
    return str(value or '').strip().lower()


def _is_no_district_state(state):
    return _normalize(state) in {_normalize(s) for s in NO_DISTRICT_STATES}


def _lnpt_component(application):
    values = [v for v in (application.lnpt_current, application.lnpt_previous, application.lnpt_two_years) if v is not None]
    if not values:
        return Decimal('0')
    return sum(values) / len(values)


def _certification_component(application):
    teacher = application.teacher
    if not teacher or not teacher.cert_level:
        return Decimal('50')
    level = teacher.cert_level.strip().lower()
    if any(kw in level for kw in ('expert', 'master')):
        return Decimal('100')
    if any(kw in level for kw in ('educator', 'trainer', 'innovator')):
        return Decimal('75')
    return Decimal('50')


def _experience_component(application):
    score = Decimal('0')
    if application.previous_gpgd and application.previous_gpgd.strip():
        score += Decimal('50')
    if application.training_experience and application.training_experience.strip():
        score += Decimal('50')
    return score


def _awards_component(application):
    return Decimal('100') if application.awards and application.awards.strip() else Decimal('0')


def _multi_cert_component(application):
    teacher = application.teacher
    return Decimal('100') if teacher and teacher.multi_certified else Decimal('0')


def score_application(application):
    """Weighted 0-100 score for one Application, per WEIGHTS."""
    components = {
        'lnpt': _lnpt_component(application),
        'certification': _certification_component(application),
        'experience': _experience_component(application),
        'awards': _awards_component(application),
        'multi_cert': _multi_cert_component(application),
    }
    total = sum(components[key] * WEIGHTS[key] for key in WEIGHTS)
    return total.quantize(Decimal('0.01'))


def _quota_group_key(application):
    """Returns ((state, district_or_None, tech_track), floor) for quota grouping."""
    if _is_no_district_state(application.state):
        return (application.state, None, application.tech_track), NO_DISTRICT_STATE_MINIMUM_PER_TECH
    return (application.state, application.district, application.tech_track), DISTRICT_MINIMUM_PER_TECH


def compile_and_rank():
    """
    Scores and ranks every pending application, then auto-flags the
    top-ranked candidates needed to fill each district/technology (or
    no-district-state/technology) minimum quota as `is_recommended`.

    Returns (stats, shortfalls) where shortfalls lists quota groups that
    don't have enough applicants to reach their minimum.
    """
    with transaction.atomic():
        applications = list(
            Application.objects.filter(status__in=PIPELINE_ENTRY_STATUSES).select_related('teacher')
        )

        for application in applications:
            application.score = score_application(application)

        by_state = defaultdict(list)
        for application in applications:
            by_state[application.state].append(application)
        for state_applications in by_state.values():
            state_applications.sort(key=lambda a: a.score, reverse=True)
            for i, application in enumerate(state_applications, start=1):
                application.rank_in_state = i

        groups = defaultdict(list)
        group_floor = {}
        for application in applications:
            key, floor = _quota_group_key(application)
            groups[key].append(application)
            group_floor[key] = floor

        shortfalls = []
        for key, group_applications in groups.items():
            floor = group_floor[key]
            group_applications.sort(key=lambda a: a.score, reverse=True)
            recommended_count = min(floor, len(group_applications))
            for i, application in enumerate(group_applications):
                application.is_recommended = i < recommended_count
            if len(group_applications) < floor:
                state, district, tech_track = key
                shortfalls.append({
                    'state': state,
                    'district': district,
                    'tech_track': tech_track,
                    'have': len(group_applications),
                    'need': floor,
                })

        now = timezone.now()
        for application in applications:
            application.status = 'Compiled'
            application.compiled_at = now
            application.save(update_fields=['score', 'rank_in_state', 'is_recommended', 'status', 'compiled_at'])

    shortfalls.sort(key=lambda s: (s['state'], s['district'] or '', s['tech_track']))
    stats = {
        'total_compiled': len(applications),
        'recommended': sum(1 for a in applications if a.is_recommended),
        'shortfall_count': len(shortfalls),
    }
    return stats, shortfalls


def shortfalls_for_state(state):
    """Quota groups within `state` that don't have enough compiled applicants to reach their minimum."""
    applications = Application.objects.filter(state=state, status__in=COMPILED_PIPELINE_STATUSES)

    groups = defaultdict(list)
    group_floor = {}
    for application in applications:
        key, floor = _quota_group_key(application)
        groups[key].append(application)
        group_floor[key] = floor

    shortfalls = []
    for key, group_applications in groups.items():
        floor = group_floor[key]
        if len(group_applications) < floor:
            _, district, tech_track = key
            shortfalls.append({
                'district': district,
                'tech_track': tech_track,
                'have': len(group_applications),
                'need': floor,
            })

    shortfalls.sort(key=lambda s: (s['district'] or '', s['tech_track']))
    return shortfalls
