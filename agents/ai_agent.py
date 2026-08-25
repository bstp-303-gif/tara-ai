"""
Autonomous pipeline agent for MyGPGD J360.

Everything between "a file was uploaded" / "a teacher applied" and the two
human approval gates (State Officer, MoE Officer) is this agent's job: run
the certification pipeline, send invitations, and compile & rank
applications, deciding via tool use what actually needs doing right now.

Invoked synchronously from agents/views.py right after the two events it
reacts to (a valid upload, a submitted application). Every run is logged to
AgentActivityLog regardless of outcome, so failures (most commonly: no
ANTHROPIC_API_KEY configured yet) never break the human-facing request —
they just show up in the dashboard's Agent Activity panel.
"""
import json
import os
import threading

import anthropic
from django.conf import settings

from . import pipeline_ops, ranking
from .models import AgentActivityLog, Application, FileUpload

MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-5')
MAX_STEPS = 6

SYSTEM_PROMPT = """You are the autonomous pipeline agent for MyGPGD J360, a Malaysian Ministry of \
Education programme that certifies teachers as digital leaders (GPGD). Staff upload certification \
files and human State and MoE Officers make the final approval decisions — those steps are never \
yours to take. Everything in between is your responsibility: turning uploaded certification files \
into a deduplicated, eligibility-classified teacher roster, inviting eligible teachers to apply, and \
compiling and ranking submitted applications so they're ready for State Officer review.

You'll be told what just happened. Check status with the read-only tools before acting, only run a \
step when there's actually new work for it to do, and use log_note for anything worth flagging that \
none of your tools can act on. When finished, give a short (2-4 sentence) plain-language summary of \
what you did and why — it's read by non-technical programme staff in an activity log, not a developer."""

TOOLS = [
    {
        'name': 'get_certification_upload_status',
        'description': "Check which certification providers (Google, Microsoft, Apple) currently have "
                       "at least one validly uploaded file, and how many records each holds.",
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'run_certification_pipeline',
        'description': "Normalize, deduplicate, and classify eligibility across every provider's latest "
                       "valid uploaded file, then persist the resulting teacher roster. Always safe to call, "
                       "including when zero providers currently have a valid file — in that case it clears "
                       "any stale roster left over from files that have since been deleted, rather than "
                       "leaving outdated teachers/eligible-candidates on the dashboard. Call it whenever the "
                       "set of valid files has changed (a new upload, or a delete), even if that set is now "
                       "empty — it never 'erases good data', it only ever reflects what's currently uploaded.",
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'send_invitation_emails',
        'description': "Email every eligible teacher who has not yet applied a secure, personalised "
                       "link to the GPGD application form.",
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'get_pending_applications',
        'description': "Count submitted teacher applications that have not yet been scored, ranked, "
                       "and compiled for State Officer review.",
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'compile_and_rank_applications',
        'description': "Score every pending application, rank it within its state, and flag top "
                       "candidates against district/technology quotas. Moves applications from "
                       "'Application Submitted'/'Under Review' to 'Compiled', ready for State Officer review.",
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'log_note',
        'description': "Record a short reasoning note or anomaly you noticed (e.g. a suspicious "
                       "duplicate, a data quality concern) without taking any pipeline action.",
        'input_schema': {
            'type': 'object',
            'properties': {'note': {'type': 'string', 'description': 'The note to record.'}},
            'required': ['note'],
            'additionalProperties': False,
        },
    },
]


def _tool_get_certification_upload_status(_tool_input):
    status = {}
    for provider in pipeline_ops.PROVIDERS:
        record = FileUpload.objects.filter(
            provider=provider, validation_status='valid'
        ).order_by('-uploaded_at').first()
        status[provider] = {
            'has_valid_file': record is not None,
            'record_count': record.record_count if record else 0,
        }
    return status


def _tool_run_certification_pipeline(_tool_input):
    return pipeline_ops.run_certification_pipeline()


def _tool_send_invitation_emails(_tool_input):
    return pipeline_ops.send_invitation_emails()


def _tool_get_pending_applications(_tool_input):
    return {'pending_count': Application.objects.filter(status__in=ranking.PIPELINE_ENTRY_STATUSES).count()}


def _tool_compile_and_rank_applications(_tool_input):
    stats, shortfalls = ranking.compile_and_rank()
    return {'stats': stats, 'shortfalls': shortfalls}


def _tool_log_note(tool_input):
    return {'logged': True, 'note': tool_input.get('note', '')}


def _format_anthropic_error(exc):
    """Convert Anthropic provider failures into plain app-safe messages."""
    message = str(exc)
    lower = message.lower()

    if 'credit balance' in lower or 'insufficient credits' in lower or 'billing' in lower:
        return (
            'Anthropic API is unavailable because the current account has insufficient credit balance. '
            'Add billing credits in the Anthropic dashboard or disable the AI pipeline agent until a '
            'working key is available.'
        )
    if 'invalid_request_error' in lower or 'model' in lower and 'not found' in lower:
        return (
            'Anthropic rejected the request. Check that the API key is valid and that the configured '
            'model name is supported by your Anthropic account.'
        )
    if 'api key' in lower or 'authentication' in lower or 'unauthorized' in lower:
        return 'Anthropic API authentication failed. Check that ANTHROPIC_API_KEY is valid.'
    return message


TOOL_IMPLEMENTATIONS = {
    'get_certification_upload_status': _tool_get_certification_upload_status,
    'run_certification_pipeline': _tool_run_certification_pipeline,
    'send_invitation_emails': _tool_send_invitation_emails,
    'get_pending_applications': _tool_get_pending_applications,
    'compile_and_rank_applications': _tool_compile_and_rank_applications,
    'log_note': _tool_log_note,
}


def run_pipeline_agent_async(trigger_reason):
    """Fire-and-forget entry point for request handlers.

    run_pipeline_agent's tool-use loop makes up to MAX_STEPS sequential LLM round-trips,
    which can take well over a minute — running it inline would leave the triggering
    upload/delete/apply request (and the user's browser) blocked waiting on it. This runs
    it on a background thread instead so the request returns immediately; the run still
    gets logged to AgentActivityLog for the dashboard's Agent Activity panel.
    """
    threading.Thread(target=run_pipeline_agent, args=(trigger_reason,), daemon=True).start()


def run_pipeline_agent(trigger_reason):
    """Runs an autonomous tool-calling loop that advances the GPGD pipeline in reaction to `trigger_reason`.

    Always returns (and logs) an AgentActivityLog row — never raises, so callers can fire this
    synchronously from a request without risking the human-facing page.
    """
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return AgentActivityLog.objects.create(
            trigger_reason=trigger_reason,
            status='error',
            error_message='ANTHROPIC_API_KEY is not configured — the pipeline agent cannot run.',
        )

    actions_taken = []
    try:
        client = anthropic.Anthropic(api_key=api_key)
        messages = [{'role': 'user', 'content': f'Trigger: {trigger_reason}'}]

        for _ in range(MAX_STEPS):
            response = client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason != 'tool_use':
                summary = next((block.text for block in response.content if block.type == 'text'), '')
                return AgentActivityLog.objects.create(
                    trigger_reason=trigger_reason,
                    summary=summary,
                    actions_taken=actions_taken,
                    status='success',
                )

            messages.append({'role': 'assistant', 'content': response.content})

            tool_results = []
            for block in response.content:
                if block.type != 'tool_use':
                    continue
                impl = TOOL_IMPLEMENTATIONS.get(block.name)
                is_error = False
                try:
                    result = impl(block.input) if impl else {'error': f'Unknown tool: {block.name}'}
                except Exception as e:
                    result = {'error': str(e)}
                    is_error = True
                actions_taken.append({'tool': block.name, 'input': block.input, 'result': result})
                tool_results.append({
                    'type': 'tool_result',
                    'tool_use_id': block.id,
                    'content': json.dumps(result, default=str),
                    'is_error': is_error,
                })
            messages.append({'role': 'user', 'content': tool_results})

        return AgentActivityLog.objects.create(
            trigger_reason=trigger_reason,
            summary='Stopped after reaching the maximum number of steps for a single run.',
            actions_taken=actions_taken,
            status='error',
            error_message='max_steps_reached',
        )
    except Exception as e:
        return AgentActivityLog.objects.create(
            trigger_reason=trigger_reason,
            actions_taken=actions_taken,
            status='error',
            error_message=_format_anthropic_error(e),
        )
