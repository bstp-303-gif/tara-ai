from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def _require_role(role):
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapped(request, *args, **kwargs):
            profile = getattr(request.user, 'officerprofile', None)
            if not profile or profile.role != role:
                messages.error(request, 'You do not have access to that page.')
                return redirect('officer_home')
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator


state_officer_required = _require_role('state_officer')
moe_officer_required = _require_role('moe_officer')


def admin_or_moe_officer_required(view_func):
    """The Admin (no officer role) or an MoE Officer — for steps the same team may do from either login."""
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        profile = getattr(request.user, 'officerprofile', None)
        if profile and profile.role != 'moe_officer':
            messages.error(request, 'You do not have access to that page.')
            return redirect('officer_home')
        return view_func(request, *args, **kwargs)
    return wrapped


def admin_required(view_func):
    """The Admin is any logged-in user without an officer role (the dashboard user)."""
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        if getattr(request.user, 'officerprofile', None):
            messages.error(request, 'Only the Admin can do that.')
            return redirect('officer_home')
        return view_func(request, *args, **kwargs)
    return wrapped
