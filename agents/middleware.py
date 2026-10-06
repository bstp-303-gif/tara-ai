import logging
import os
import time

from django.shortcuts import redirect

# Paths a logged-in State Officer is allowed to reach. Everything else
# (upload, dashboard, manage/*, MoE review, admin, ...) redirects them
# back to their own review page — they only need their own /agents/state/ screens.
STATE_OFFICER_ALLOWED_PREFIXES = ('/agents/login', '/agents/logout', '/agents/state/')

# Same idea for MoE Officers: their own /agents/moe/ pages, the Compile & Rank action, and the
# (view-only) Recognition Dashboard of everyone they have recognised.
MOE_OFFICER_ALLOWED_PREFIXES = ('/agents/login', '/agents/logout', '/agents/moe/', '/agents/compile-applications',
                                '/agents/recognition-dashboard')


class StateOfficerRestrictMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            profile = getattr(user, 'officerprofile', None)
            if profile and profile.role == 'state_officer':
                if not request.path.startswith(STATE_OFFICER_ALLOWED_PREFIXES):
                    return redirect('state_review')
        return self.get_response(request)


class MoeOfficerRestrictMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            profile = getattr(user, 'officerprofile', None)
            if profile and profile.role == 'moe_officer':
                if not request.path.startswith(MOE_OFFICER_ALLOWED_PREFIXES):
                    return redirect('moe_review')
        return self.get_response(request)


logger = logging.getLogger('agents.latency')

# Requests slower than this are logged as warnings so they're easy to spot
# in logs/latency.log. Override with the SLOW_REQUEST_THRESHOLD_MS env var
# (e.g. in .env) if 2 seconds is too strict or too lenient for your network.
SLOW_REQUEST_THRESHOLD_MS = int(os.environ.get('SLOW_REQUEST_THRESHOLD_MS', '2000'))


class RequestLatencyLoggingMiddleware:
    """
    Times every request and logs how long it took — nothing else.

    This only *watches* requests; it never changes what a view does or
    what gets returned, so it can't affect any existing behaviour. Every
    request is logged at INFO level to logs/latency.log; anything slower
    than SLOW_REQUEST_THRESHOLD_MS is also logged at WARNING level, which
    is what to grep for when checking real-world performance over
    rural/low-bandwidth connections.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        duration_ms = (time.perf_counter() - start) * 1000

        message = '%s %s -> %s in %.0fms' % (
            request.method, request.path, response.status_code, duration_ms,
        )
        if duration_ms >= SLOW_REQUEST_THRESHOLD_MS:
            logger.warning('SLOW ' + message)
        else:
            logger.info(message)

        # Also surfaced as a response header, handy for spot-checking
        # timing from a browser's network tab without opening the log file.
        response['X-Response-Time-ms'] = f'{duration_ms:.0f}'
        return response
