"""Demo-only pages (mygpgd.settings_demo). Everywhere else they return 404.

The demo saves every outgoing email as a file in demo/sent_emails/ instead of sending it (Django's
file-based email backend). The Demo Inbox shows those files like a mailbox, so a presenter can show a
teacher receiving their invitation and clicking through to the application form.
"""
import email
import os
from email import policy
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

SEPARATOR = '-' * 79  # what the file-based backend writes between messages in one file


def _messages():
    """Every saved email, newest first, as dicts with id, to, subject, date, text and html."""
    folder = getattr(settings, 'EMAIL_FILE_PATH', '')
    if not folder or not os.path.isdir(folder):
        return []
    found = []
    for filename in os.listdir(folder):
        with open(os.path.join(folder, filename), encoding='utf-8', errors='replace') as f:
            chunks = [c.strip() for c in f.read().split(SEPARATOR) if c.strip()]
        for index, chunk in enumerate(chunks):
            message = email.message_from_string(chunk, policy=policy.default)
            text = message.get_body(preferencelist=('plain',))
            html = message.get_body(preferencelist=('html',))
            try:
                sent = timezone.localtime(parsedate_to_datetime(message['Date']))
            except (TypeError, ValueError):
                sent = None
            found.append({
                'id': f'{filename}:{index}', 'to': str(message['To'] or ''), 'subject': str(message['Subject'] or ''),
                'date': sent, 'text': text.get_content() if text else '', 'html': html.get_content() if html else '',
            })
    return sorted(found, key=lambda m: (m['date'] is not None, m['date']), reverse=True)


def _kind(subject):
    """A short label for the inbox list."""
    lowered = subject.lower()
    for words, label in [('invitation', 'Invitation'), ('received', 'Acknowledgement'), ('recognition', 'Recognition letter'),
                         ('reminder', 'Monthly reminder'), ('monthly training report', 'Monthly report')]:
        if words in lowered:
            return label
    return 'Email'


def inbox(request):
    """Demo Inbox: every email the demo has "sent", searchable by recipient or subject."""
    if not getattr(settings, 'GPGD_DEMO', False):
        raise Http404
    query = request.GET.get('q', '').strip().lower()
    messages = _messages()
    for message in messages:
        message['kind'] = _kind(message['subject'])
    if query:
        messages = [m for m in messages if query in m['to'].lower() or query in m['subject'].lower()]
    selected = next((m for m in messages if m['id'] == request.GET.get('id')), messages[0] if messages else None)
    return render(request, 'demo_inbox.html', {'emails': messages[:300], 'total': len(messages),
                                               'selected': selected, 'q': request.GET.get('q', '')})
