"""Settings for running a self-contained demo with synthetic data (see demo/README.md).

    python manage.py migrate --settings=mygpgd.settings_demo
    python manage.py seed_demo --settings=mygpgd.settings_demo
    python manage.py runserver 8001 --settings=mygpgd.settings_demo

Everything lives under demo/: its own SQLite database, uploads and saved emails. It never
connects to the shared PostgreSQL database and never sends real email, whatever .env says.
"""
from .settings import *  # noqa: F401,F403
from .settings import BASE_DIR

GPGD_DEMO = True
DEMO_DIR = BASE_DIR / 'demo'
DEMO_DIR.mkdir(exist_ok=True)

DEBUG = True
ALLOWED_HOSTS = ['localhost', '127.0.0.1']
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
SECURE_PROXY_SSL_HEADER = None

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(DEMO_DIR / 'demo.sqlite3'),
    }
}

# Emails are written to demo/sent_emails/ (one file per send) instead of being delivered.
EMAIL_BACKEND = 'django.core.mail.backends.filebased.EmailBackend'
EMAIL_FILE_PATH = str(DEMO_DIR / 'sent_emails')
DEFAULT_FROM_EMAIL = 'GPGD Programme <noreply@moe.gov.my>'

GPGD_UPLOAD_DIR = str(DEMO_DIR / 'uploads')
MEDIA_ROOT = DEMO_DIR / 'media'
GPGD_SITE_URL = 'http://localhost:8001'

# Shows a "DEMO" banner on every page, so the demo can't be mistaken for the real system.
TEMPLATES[0]['OPTIONS']['context_processors'] = [
    *TEMPLATES[0]['OPTIONS']['context_processors'], 'agents.context_processors.demo_mode',
]
