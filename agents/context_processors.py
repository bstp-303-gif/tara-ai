from django.conf import settings


def demo_mode(request):
    """Lets every page show the DEMO banner when running under mygpgd.settings_demo."""
    return {'GPGD_DEMO': getattr(settings, 'GPGD_DEMO', False)}
