"""Reporting Agent: the second TARA AI agent, alongside the pipeline agent (agents/ai_agent.py).

Each month it investigates the current GPGDs' training activity through read-only tools, then writes a
plain-language briefing for the BSTP Director (Malaysia) and for each State Director, flagging what
needs attention (districts with no activity, GPGDs who have stopped reporting, falling numbers).

Safeguards, so a briefing can't state something the data doesn't support:
- every figure the agent sees comes from its tools, computed here from the database;
- save_briefing rejects a summary containing a number that isn't in that scope's facts, and tells the
  agent which numbers to fix; the agent then corrects and saves again;
- briefings are saved as drafts: the Admin reviews, edits if needed and approves them on the Monthly
  Reports page, and only approved briefings go into the emails (agents/monthly_reports.py).

Every run is logged to AgentActivityLog with each tool call, like the pipeline agent.
"""
import json
import os
import re
import threading
from datetime import timedelta

import anthropic
from django.conf import settings
from django.utils import timezone

from . import monthly_reports, pipeline_ops
from .ai_agent import _format_anthropic_error
from .constants import MALAYSIA_STATES, PPD_BY_STATE
from .models import AgentActivityLog, MonthlyBriefing

MODEL = os.environ.get('ANTHROPIC_REPORTING_MODEL', 'claude-opus-5-5')
MAX_STEPS = 12
NATIONAL = 'Malaysia'

SYSTEM_PROMPT = """You are the Reporting Agent for MyGPGD J360, the Ministry of Education Malaysia's \
programme of Guru Peneraju Generasi Digital (GPGD): teachers recognised as digital leaders, who train \
other teachers, students and school leaders and report every activity monthly.

Your job: write a short briefing on one month's training activity for the BSTP Director (scope \
"Malaysia") and for each State Director (scope = the state's name).

How to work:
1. Call get_national_overview first.
2. Call get_state_detail for every state, in parallel (several tool calls in one turn).
3. Call save_briefing once for "Malaysia" and once for every state, in parallel where you can.

Each briefing: 2-4 plain sentences for a busy director. Lead with the most useful facts (teachers and \
students trained, activities, how many GPGDs reported), compare with the previous month when it \
helps, and name what needs attention. Put 0-4 short "needs attention" points in flags, naming \
districts or problems specifically (for example districts with no activity, or GPGDs who have not \
reported for two months). If a state had no current GPGDs, say so briefly.

Use only numbers that appear in the tool results for that scope, exactly as given (percentages are \
provided where useful). save_briefing checks this and returns an error listing any number it cannot \
match; fix the text and save again. Write in English, no markdown. When every briefing is saved, \
reply with one sentence saying what you prepared."""

TOOLS = [
    {
        'name': 'get_national_overview',
        'description': "The month's national figures and one row per state: current GPGDs, how many "
                       "reported, activities, hours, teachers/students/school leaders/others trained, "
                       "the same totals for the previous month, and the percentage change.",
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        'strict': True,
    },
    {
        'name': 'get_state_detail',
        'description': "One state's figures for the month: totals, previous month, one row per district "
                       "(PPD), districts with no activity, and how many GPGDs have not reported this month "
                       "or for two months running.",
        'input_schema': {
            'type': 'object',
            'properties': {'state': {'type': 'string', 'enum': MALAYSIA_STATES}},
            'required': ['state'],
            'additionalProperties': False,
        },
        'strict': True,
    },
    {
        'name': 'save_briefing',
        'description': 'Save the briefing for one scope ("Malaysia" or a state) as a draft for the Admin '
                       'to approve. Rejected with the reason if it contains a number not found in that '
                       "scope's facts.",
        'input_schema': {
            'type': 'object',
            'properties': {
                'scope': {'type': 'string', 'enum': [NATIONAL, *MALAYSIA_STATES]},
                'summary': {'type': 'string', 'description': '2-4 plain sentences.'},
                'flags': {'type': 'array', 'items': {'type': 'string'}, 'description': '0-4 short points.'},
            },
            'required': ['scope', 'summary', 'flags'],
            'additionalProperties': False,
        },
        'strict': True,
    },
]


# --- facts (computed from the database; the only numbers the agent ever sees) ------------------

def _percent_change(now, before):
    return None if not before else round(100 * (now - before) / before)


def _numbers(totals):
    """The headline numbers of monthly_reports._totals, as plain ints/floats."""
    keys = ['gpgds', 'gpgds_reporting', 'activities', 'num_teachers', 'num_students', 'num_school_leaders',
            'num_others', 'participants']
    numbers = {k: totals[k] for k in keys}
    numbers['hours'] = float(totals['hours'])
    return numbers


def _with_change(current, previous):
    row = {**current, 'previous_month': previous}
    for key in ('num_teachers', 'num_students', 'activities'):
        row[f'{key}_change_pct'] = _percent_change(current[key], previous[key])
    return row


def national_facts(period):
    gpgds, reports = monthly_reports.collect(period)
    _, previous = monthly_reports.collect(_previous(period))
    state_rows = []
    for state in MALAYSIA_STATES:
        in_state = [g for g in gpgds if g.state == state]
        current = _numbers(monthly_reports._totals([r for r in reports if r.gpgd.state == state], in_state))
        before = _numbers(monthly_reports._totals([r for r in previous if r.gpgd.state == state], in_state))
        state_rows.append({'state': state, **_with_change(current, before)})
    return {
        'month': pipeline_ops.month_label(period),
        'recognition_year': pipeline_ops.current_cohort_year(),
        'national': _with_change(_numbers(monthly_reports._totals(reports, gpgds)),
                                 _numbers(monthly_reports._totals(previous, gpgds))),
        'states': state_rows,
    }


def state_facts(period, state):
    gpgds, reports = monthly_reports.collect(period)
    _, previous = monthly_reports.collect(_previous(period))
    in_state = [g for g in gpgds if g.state == state]
    state_reports = [r for r in reports if r.gpgd.state == state]
    reported_now = {r.gpgd_id for r in state_reports}
    reported_before = {r.gpgd_id for r in previous if r.gpgd.state == state}
    districts = []
    for row in monthly_reports._breakdown(state_reports, in_state, monthly_reports._district, PPD_BY_STATE.get(state, [])):
        districts.append({'district': row['name'], **_numbers(row)})
    return {
        'state': state,
        'month': pipeline_ops.month_label(period),
        **_with_change(_numbers(monthly_reports._totals(state_reports, in_state)),
                       _numbers(monthly_reports._totals([r for r in previous if r.gpgd.state == state], in_state))),
        'districts': districts,
        'districts_with_gpgds_but_no_activity': [d['district'] for d in districts if d['gpgds'] and not d['activities']],
        'districts_without_gpgds': [d['district'] for d in districts if not d['gpgds']],
        'gpgds_not_reporting_this_month': len([g for g in in_state if g.id not in reported_now]),
        'gpgds_not_reporting_two_months': len([g for g in in_state if g.id not in reported_now | reported_before]),
    }


def _previous(period):
    year, month = (int(p) for p in period.split('-'))
    return f'{year - 1}-12' if month == 1 else f'{year}-{month - 1:02d}'


def _all_numbers(value, found=None):
    """Every number anywhere in a facts structure, normalised to strings ("1,234" -> "1234", 36.0 -> "36")."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for v in value.values():
            _all_numbers(v, found)
    elif isinstance(value, list):
        for v in value:
            _all_numbers(v, found)
    elif isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, (int, float)):
        found.add(_normal(str(value)))
        found.add(_normal(str(abs(value))))  # "down 12%" for a change of -12
    elif isinstance(value, str):
        found.update(_normal(n) for n in re.findall(r'\d[\d,]*(?:\.\d+)?', value))
    return found


def _normal(number):
    number = number.replace(',', '')
    return number[:-2] if number.endswith('.0') else number


def unsupported_numbers(text, facts):
    """Numbers in `text` that don't appear in `facts` (the year, and 1 and 2, are always allowed)."""
    allowed = _all_numbers(facts) | {'1', '2'}
    year = facts.get('month', '').split()[-1] if facts.get('month') else ''
    allowed |= {year, str(int(year) - 1)} if year.isdigit() else set()
    return sorted({_normal(n) for n in re.findall(r'\d[\d,]*(?:\.\d+)?', text)} - allowed)


# --- the agent loop --------------------------------------------------------------------------

def _run_tool(name, tool_input, period, run, facts_cache):
    if name == 'get_national_overview':
        facts_cache[NATIONAL] = national_facts(period)
        return facts_cache[NATIONAL]
    if name == 'get_state_detail':
        state = tool_input['state']
        facts_cache[state] = {**state_facts(period, state),
                              'national_row': next((r for r in national_facts(period)['states'] if r['state'] == state), {})}
        return facts_cache[state]
    if name == 'save_briefing':
        scope = tool_input['scope']
        facts = facts_cache.get(scope)
        if facts is None:
            return {'error': f'Look up the facts for {scope} first ({"get_national_overview" if scope == NATIONAL else "get_state_detail"}).'}
        text = ' '.join([tool_input['summary'], *tool_input.get('flags', [])])
        bad = unsupported_numbers(text, facts)
        if bad:
            return {'error': f'Not saved: these numbers are not in the facts for {scope}: {", ".join(bad)}. '
                             'Use only figures from the tool results, then save again.'}
        MonthlyBriefing.objects.update_or_create(period=period, scope=scope, defaults={
            'summary': tool_input['summary'].strip(), 'flags': [f.strip() for f in tool_input.get('flags', []) if f.strip()][:4],
            'status': 'draft', 'edited': False, 'agent_run': run, 'approved_by': None, 'approved_at': None,
        })
        return {'saved': scope}
    return {'error': f'Unknown tool: {name}'}


def _short(result):
    """Tool results kept in the activity log: small ones in full, big lookups summarised."""
    text = json.dumps(result, default=str)
    return result if len(text) <= 600 else {'summary': text[:600] + '…'}


def prepare_briefings(period, trigger='Monthly Reports page'):
    """Runs the Reporting Agent for `period` and returns its AgentActivityLog row. Never raises."""
    run = AgentActivityLog.objects.create(
        trigger_reason=f'Reporting Agent: briefings for {pipeline_ops.month_label(period)} ({trigger})', status='running')
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        run.status, run.error_message = 'error', 'ANTHROPIC_API_KEY is not configured — the Reporting Agent cannot run.'
        run.save()
        return run

    actions, facts_cache = [], {}
    messages = [{'role': 'user', 'content': f'Prepare the briefings for {pipeline_ops.month_label(period)} ({period}).'}]
    try:
        client = anthropic.Anthropic(api_key=api_key)
        for _ in range(MAX_STEPS):
            response = client.beta.messages.create(
                model=MODEL, max_tokens=16000, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
                output_config={'effort': 'medium'},
                betas=['server-side-fallback-2026-07-01'], fallbacks='default',  # re-run on a fallback model if declined
            )
            if response.stop_reason == 'refusal':
                raise RuntimeError('The model declined the request.')
            if response.stop_reason != 'tool_use':
                run.summary = next((b.text for b in response.content if b.type == 'text'), '')
                break
            messages.append({'role': 'assistant', 'content': response.content})
            results = []
            for block in response.content:
                if block.type != 'tool_use':
                    continue
                try:
                    result = _run_tool(block.name, block.input, period, run, facts_cache)
                except Exception as e:  # a tool failing shouldn't end the run; the agent sees the error
                    result = {'error': str(e)}
                actions.append({'tool': block.name, 'input': block.input, 'result': _short(result)})
                results.append({'type': 'tool_result', 'tool_use_id': block.id,
                                'content': json.dumps(result, default=str), 'is_error': 'error' in result})
            messages.append({'role': 'user', 'content': results})  # all results of one turn in one message
        else:
            run.error_message = 'Stopped after the maximum number of steps.'
        saved = MonthlyBriefing.objects.filter(period=period, agent_run=run).count()
        run.status = 'success' if saved and not run.error_message else 'error'
        if not saved and not run.error_message:
            run.error_message = 'The agent finished without saving any briefing.'
        run.summary = run.summary or f'Saved {saved} briefing(s).'
    except Exception as e:
        run.status, run.error_message = 'error', _format_anthropic_error(e)
    run.actions_taken = actions
    run.save()
    return run


def prepare_briefings_async(period, trigger='Monthly Reports page'):
    threading.Thread(target=prepare_briefings, args=(period, trigger), daemon=True).start()


def is_running(period):
    """True while a run for `period` started in the last 10 minutes hasn't finished."""
    recent = timezone.now() - timedelta(minutes=10)
    return AgentActivityLog.objects.filter(
        status='running', triggered_at__gte=recent,
        trigger_reason__startswith=f'Reporting Agent: briefings for {pipeline_ops.month_label(period)}').exists()
