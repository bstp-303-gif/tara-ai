from collections import defaultdict
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from .constants import MALAYSIA_STATES, PPD_BY_STATE
from .models import Application, TrackLimit

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
NO_DISTRICT_STATES = {'Perlis', 'W.P. Putrajaya', 'W.P. Labuan'}
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


def canonical_district(state, district):
    """The official PPD name for `district` in `state` if it matches one (ignoring case and spacing),
    else the district as typed (applications from before the PPD dropdown)."""
    key = _normalize(district)
    return next((d for d in PPD_BY_STATE.get(state, []) if _normalize(d) == key), (district or '').strip())


def minimum_groups(state):
    """Every (district, track, minimum) the minimum quota applies to in `state`: each PPD x track, or
    (None, track, 5) for states without districts (Perlis, Putrajaya, Labuan)."""
    tracks = [t for t, _ in Application.TRACK_CHOICES]
    if _is_no_district_state(state) or not PPD_BY_STATE.get(state):
        return [(None, t, NO_DISTRICT_STATE_MINIMUM_PER_TECH) for t in tracks]
    return [(d, t, DISTRICT_MINIMUM_PER_TECH) for d in PPD_BY_STATE[state] for t in tracks]


def _minimum_rows(state, applications):
    """One row per minimum group in `state` (every district, including ones nobody applied from), with
    the applications in it. Districts that aren't official PPD names get rows of their own."""
    rows = {}
    for district, track, floor in minimum_groups(state):
        rows[(_normalize(district), track)] = {'district': district, 'tech_track': track, 'need': floor, 'applications': []}
    for application in applications:
        (_, district, track), floor = _quota_group_key(application)
        rows.setdefault((_normalize(district), track), {
            'district': district, 'tech_track': track, 'need': floor, 'applications': [],
        })['applications'].append(application)
    return sorted(rows.values(), key=lambda r: (r['district'] or '', r['tech_track']))


def _quota_group_key(application):
    """Returns ((state, district_or_None, tech_track), floor) for quota grouping."""
    if _is_no_district_state(application.state):
        return (application.state, None, application.tech_track), NO_DISTRICT_STATE_MINIMUM_PER_TECH
    return (application.state, canonical_district(application.state, application.district), application.tech_track), DISTRICT_MINIMUM_PER_TECH


def quota_group(application):
    """(district or None, track, minimum) of the minimum-quota group `application` belongs to."""
    (_, district, track), minimum = _quota_group_key(application)
    return district, track, minimum


def _add_past_gpgd_records(applications):
    """Adds "GPGD (year)" to the recognitions of anyone our own records show was recognised as a
    GPGD in an earlier year, whatever they declared on the form."""
    past = defaultdict(set)
    for ic, year in (Application.objects.filter(status='Approved', recognized_year__lt=timezone.localdate().year)
                     .values_list('ic_number', 'recognized_year')):
        past[ic.strip()].add(str(year))
    for application in applications:
        years = past.get(application.ic_number.strip())
        if years and not any(r.get('source') == 'records' for r in application.recognitions):
            application.recognitions = [*application.recognitions,
                                        {'name': 'GPGD', 'years': ', '.join(sorted(years)), 'source': 'records'}]


def _recommend(group_applications, floor):
    """Flags the top `floor` by score as Recommended, keeping at least one of those places for the
    best-scoring teacher who has never been recognised, when the top scorers don't include one.
    `group_applications` must be sorted by score, highest first."""
    recommended = group_applications[:floor]
    if recommended and not any(a.is_never_recognised for a in recommended):
        newcomer = next((a for a in group_applications[floor:] if a.is_never_recognised), None)
        if newcomer:
            recommended = recommended[:-1] + [newcomer]
    for application in group_applications:
        application.is_recommended = application in recommended
        application.reserved_place = application.is_recommended and application not in group_applications[:floor]


def compile_and_rank():
    """
    Scores and ranks every pending application, then auto-flags the
    top-ranked candidates needed to fill each district/technology (or
    no-district-state/technology) minimum quota as `is_recommended`,
    reserving one of those places for a never-recognised teacher (see _recommend).

    Returns (stats, shortfalls) where shortfalls lists quota groups that
    don't have enough applicants to reach their minimum.
    """
    with transaction.atomic():
        applications = list(
            Application.objects.filter(status__in=PIPELINE_ENTRY_STATUSES).select_related('teacher')
        )

        _add_past_gpgd_records(applications)
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

        for key, group_applications in groups.items():
            group_applications.sort(key=lambda a: a.score, reverse=True)
            _recommend(group_applications, group_floor[key])

        now = timezone.now()
        for application in applications:
            application.status = 'Compiled'
            application.compiled_at = now
            application.save(update_fields=['score', 'rank_in_state', 'is_recommended', 'reserved_place',
                                            'recognitions', 'status', 'compiled_at'])

    shortfalls = [{'state': state, **s} for state in MALAYSIA_STATES for s in shortfalls_for_state(state)]
    stats = {
        'total_compiled': len(applications),
        'recommended': sum(1 for a in applications if a.is_recommended),
        'shortfall_count': len(shortfalls),
    }
    return stats, shortfalls


def shortfalls_for_state(state):
    """Every district/track in `state` with fewer applicants this year than its minimum, including
    districts nobody has applied from (have = 0)."""
    return [
        {'district': row['district'], 'tech_track': row['tech_track'], 'have': len(row['applications']), 'need': row['need']}
        for row in _minimum_rows(state, this_years_applications().filter(state=state))
        if len(row['applications']) < row['need']
    ]


def this_years_applications():
    """Every application except GPGDs the MoE recognised in an earlier year. Those belong to a past
    intake, so they mustn't show up in this year's statistics, grids or quotas."""
    return Application.objects.exclude(recognized_year__lt=timezone.localdate().year)


# Statuses that count toward a quota: approved by the State Officer and not rejected by the MoE.
QUOTA_FILLING_STATUSES = ['State Approved', 'Approved']


def state_progress(state):
    """Minimum-quota progress for every district/track in `state` (every PPD, including ones nobody has
    applied from yet), counting this year's applications."""
    rows = []
    for row in _minimum_rows(state, this_years_applications().filter(state=state)):
        statuses = [a.status for a in row.pop('applications')]
        row.update({
            'applied': len(statuses),
            'approved': sum(s in QUOTA_FILLING_STATUSES for s in statuses),
            'declined': statuses.count('State Declined'),
            'pending': statuses.count('Compiled'),
        })
        row['filled'] = min(row['approved'], row['need'])
        row['remaining'] = row['need'] - row['filled']
        row['percent'] = round(100 * row['filled'] / row['need']) if row['need'] else 100
        rows.append(row)
    return rows


def limit_reached(application, statuses):
    """(where, used, maximum) for the first maximum `application` would exceed — its state's, then its
    district's — or None if it fits both. `where` is the state or district name.

    `statuses` are what count as a used place: QUOTA_FILLING_STATUSES when a State Officer approves
    (State Approved and MoE Approved), ['Approved'] for the MoE's final approval. The application
    itself is never counted, so re-saving an approval doesn't block itself.
    """
    same_track = (this_years_applications()
                  .filter(state=application.state, tech_track=application.tech_track, status__in=statuses)
                  .exclude(pk=application.pk))
    checks = [(application.state, '', same_track)]
    district = (application.district or '').strip()
    if district:
        checks.append((district, district, same_track.filter(district__iexact=district)))
    for where, limit_district, used in checks:
        limit = TrackLimit.objects.filter(
            state=application.state, district__iexact=limit_district, tech_track=application.tech_track).first()
        if limit:
            count = used.count()
            if count >= limit.maximum:
                return where, count, limit.maximum
    return None


def limit_usage(state, district=''):
    """Per track in `state` (or one district of it): places used this year (State or MoE approved) and the
    maximum (None = no limit)."""
    limits = dict(TrackLimit.objects.filter(state=state, district=district).values_list('tech_track', 'maximum'))
    applications = this_years_applications().filter(state=state, status__in=QUOTA_FILLING_STATUSES)
    if district:
        applications = applications.filter(district__iexact=district)
    used = dict(applications.values_list('tech_track').annotate(n=models.Count('id')))
    return [{'track': t, 'used': used.get(t, 0), 'maximum': limits.get(t)} for t, _ in Application.TRACK_CHOICES]
