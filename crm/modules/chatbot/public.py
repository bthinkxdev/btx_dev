"""Public JSON endpoints behind the website chat widget (no login).

CSRF-exempt on purpose: a visitor has no account, the widget holds an unguessable chat token, and
a stale CSRF cookie must never break a conversation. Abuse is limited by an Origin check and
per-IP / per-chat rate limits instead.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from crm.modules.leads import services as lead_svc

from . import knowledge, services

logger = logging.getLogger('crm.chatbot')

LIMITS = {  # (max calls, window seconds)
    'start': (12, 3600),
    'message': (40, 600),
    'lead': (6, 3600),
}


def _error(message, status=400):
    return JsonResponse({'ok': False, 'error': message}, status=status)


def _json(request):
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _same_origin(request) -> bool:
    origin = request.META.get('HTTP_ORIGIN')
    if not origin or origin == 'null':
        return True   # some in-app browsers send none; the token and rate limits still apply
    return urlparse(origin).netloc == request.get_host()


def _limited(request, kind: str) -> bool:
    max_calls, window = LIMITS[kind]
    key = f'chat-rl:{kind}:{services._ip(request)}'
    try:
        cache.add(key, 0, window)
        return cache.incr(key) > max_calls
    except Exception:
        return False


def _guard(request, kind):
    if not _same_origin(request):
        return _error('Not allowed.', 403)
    if _limited(request, kind):
        return _error('You are sending messages very quickly. Please wait a moment.', 429)
    return None


def _payload(session, **extra):
    return {'ok': True, 'token': str(session.token), 'messages': services.history(session),
            'quick_replies': knowledge.QUICK_REPLIES if session.message_count <= 1 else [],
            'lead_captured': bool(session.lead_id), 'whatsapp': knowledge.WHATSAPP_URL, **extra}


@csrf_exempt
@never_cache
@require_POST
def start(request):
    data = _json(request)
    session = services.get_session(data.get('token'))
    if session is None:
        if (blocked := _guard(request, 'start')):
            return blocked
        session = services.start_session(request, str(data.get('page', '')))
    return JsonResponse(_payload(session))


@csrf_exempt
@never_cache
@require_POST
def message(request):
    if (blocked := _guard(request, 'message')):
        return blocked
    data = _json(request)
    session = services.get_session(data.get('token'))
    if session is None:
        return _error('This chat has expired. Please reopen it.', 404)
    try:
        result = services.reply_to(session, str(data.get('text', '')))
    except services.ChatError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception('chat message failed')
        return JsonResponse({'ok': True, 'reply': knowledge.rule_based_reply(str(data.get('text', ''))), 'captured': False,
                             'lead_captured': bool(session.lead_id), 'whatsapp': knowledge.WHATSAPP_URL})
    return JsonResponse({'ok': True, 'reply': result['reply'], 'captured': result['captured'],
                         'lead_captured': bool(session.lead_id) or result['captured'], 'whatsapp': knowledge.WHATSAPP_URL,
                         'pdf_url': _pdf_for(session)})


def _pdf_for(session):
    from django.urls import reverse
    return reverse('pages:quote_pdf', args=[session.interest]) if session.interest in knowledge.PACKAGES else ''


@csrf_exempt
@never_cache
@require_POST
def lead(request):
    if (blocked := _guard(request, 'lead')):
        return blocked
    data = _json(request)
    if data.get('website'):   # honeypot: real visitors never fill this
        return JsonResponse({'ok': True})
    session = services.get_session(data.get('token'))
    if session is None:
        return _error('This chat has expired. Please reopen it.', 404)
    name = str(data.get('name', '')).strip()
    if len(name) < 2:
        return _error('Please enter your name.')
    try:
        services.capture_lead(session, name=name, phone=str(data.get('phone', '')), email=str(data.get('email', '')),
                              business=str(data.get('business', '')).strip())
    except lead_svc.LeadError:
        return _error('Please enter a valid 10-digit mobile number (or an email address).')
    except Exception:
        logger.exception('chat lead capture failed')
        return _error('Sorry, something went wrong. Please try WhatsApp instead.', 500)
    thanks = f'Thank you, {name.split()[0]}! Our team will contact you within 24 hours on business days.'
    from crm.models import ChatMessage
    ChatMessage.objects.create(session=session, role='bot', content=thanks, engine='rules')
    return JsonResponse({'ok': True, 'reply': thanks, 'lead_captured': True, 'pdf_url': _pdf_for(session),
                         'whatsapp': knowledge.WHATSAPP_URL})
