from django.shortcuts import redirect

# Paths a logged-in State Officer is allowed to reach. Everything else
# (upload, dashboard, manage/*, MoE review, admin, ...) redirects them
# back to their own review page — they only ever need that one screen.
STATE_OFFICER_ALLOWED_PREFIXES = ('/agents/login', '/agents/logout', '/agents/state/review')

# Same idea for MoE Officers: they only ever need their own review page.
MOE_OFFICER_ALLOWED_PREFIXES = ('/agents/login', '/agents/logout', '/agents/moe/review')


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
