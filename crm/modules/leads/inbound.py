"""
Public inbound lead endpoints (Part 4.2, 14.4). Store first, process second:
the raw payload is saved to LeadInbox before any parsing, then processed after
commit. A failure leaves the payload in the inbox for retry and review.

Auth (any one):
  * X-Signature: hex HMAC-SHA256 of the raw body with the source secret
  * X-Source-Secret: the source secret (for simple website forms posting server-side)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from crm.core import audit, ratelimit
from crm.core.middleware import client_ip
from crm.models import LeadInbox, LeadSource

from . import services as svc

logger = logging.getLogger('crm.inbound')
KEEP_HEADERS = ('CONTENT_TYPE', 'HTTP_USER_AGENT', 'HTTP_X_FORWARDED_FOR', 'HTTP_REFERER', 'HTTP_ORIGIN')


def _parse_body(request) -> dict:
    ctype = request.META.get('CONTENT_TYPE', '')
    if 'application/json' in ctype:
        try:
            data = json.loads(request.body or b'{}')
            return data if isinstance(data, dict) else {'items': data}
        except (ValueError, UnicodeDecodeError):
            return {}
    return {k: v for k, v in request.POST.items()}


def _verify(source: LeadSource, request) -> bool:
    sig = request.headers.get('X-Signature', '')
    if sig:
        expected = hmac.new(source.secret.encode(), request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig.lower().removeprefix('sha256='))
    secret = request.headers.get('X-Source-Secret', '') or request.POST.get('_secret', '')
    return bool(secret) and hmac.compare_digest(secret, source.secret)


def _external_id(payload: dict) -> str:
    for k in ('external_id', 'lead_id', 'leadgen_id', 'submission_id', 'id'):
        v = payload.get(k)
        if v not in (None, ''):
            return str(v)[:120]
    return ''


def store(source: LeadSource, request, payload: dict, signature_valid: bool) -> tuple[LeadInbox, bool]:
    ext = _external_id(payload)
    headers = {k: request.META.get(k, '')[:300] for k in KEEP_HEADERS if request.META.get(k)}
    try:
        with transaction.atomic():
            item = LeadInbox.objects.create(
                source=source, external_id=ext, raw_body=request.body.decode('utf-8', errors='replace')[:200000],
                payload=payload, headers=headers, content_type=request.META.get('CONTENT_TYPE', '')[:100],
                remote_ip=client_ip(request), signature_valid=signature_valid)
        return item, True
    except IntegrityError:
        # Same external id from the same source: idempotent repeat.
        return LeadInbox.objects.get(source=source, external_id=ext), False


@csrf_exempt
@require_http_methods(['POST'])
def inbound_lead(request, source_key):
    ip = client_ip(request) or ''
    if not ratelimit.hit('inbound', ip, limit=60, window_seconds=60):
        audit.security_event('rate_limited', request=request, detail='inbound lead')
        return JsonResponse({'ok': False, 'error': 'Too many requests'}, status=429)
    source = LeadSource.objects.filter(source_key=source_key, is_active=True).first()
    if source is None:
        return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)
    if not _verify(source, request):
        audit.security_event('invalid_signature', request=request, detail=f'lead source {source.pk}')
        return JsonResponse({'ok': False, 'error': 'Invalid signature'}, status=401)
    payload = _parse_body(request)
    payload.pop('_secret', None)
    item, created = store(source, request, payload, True)
    if created:
        transaction.on_commit(lambda: _process(item.pk))
    return JsonResponse({'ok': True, 'inbox_id': item.pk, 'duplicate': not created}, status=202 if created else 200)


def _process(inbox_id: int):
    try:
        svc.ingest(LeadInbox.objects.get(pk=inbox_id))
    except Exception:  # stays in the inbox for the retry job
        logger.exception('Inbound processing failed for %s', inbox_id)


# ---------------------------------------------------------------- Meta lead ads

def _meta_source():
    return LeadSource.objects.filter(channel='meta', is_active=True).order_by('id').first()


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def meta_webhook(request):
    verify_token = getattr(settings, 'META_VERIFY_TOKEN', '') or ''
    if request.method == 'GET':
        if verify_token and request.GET.get('hub.verify_token') == verify_token and request.GET.get('hub.mode') == 'subscribe':
            return HttpResponse(request.GET.get('hub.challenge', ''), content_type='text/plain')
        return HttpResponse(status=403)
    secret = getattr(settings, 'META_APP_SECRET', '') or ''
    sig = request.headers.get('X-Hub-Signature-256', '')
    expected = 'sha256=' + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest() if secret else ''
    if not secret or not hmac.compare_digest(expected, sig):
        audit.security_event('invalid_signature', request=request, detail='meta webhook')
        return HttpResponse(status=401)
    source = _meta_source()
    if source is None:
        return HttpResponse(status=404)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    stored = []
    for entry in body.get('entry', []) or []:
        for change in entry.get('changes', []) or []:
            value = change.get('value', {}) or {}
            if not value.get('leadgen_id'):
                continue
            payload = {'leadgen_id': str(value['leadgen_id']), 'form_id': value.get('form_id'),
                       'page_id': value.get('page_id'), 'ad_id': value.get('ad_id'), 'created_time': value.get('created_time')}
            item, created = store(source, request, payload, True)
            if created:
                stored.append(item.pk)
    for pk in stored:
        transaction.on_commit(lambda pk=pk: _meta_fetch_and_process(pk))
    return HttpResponse('OK')


def meta_fetch(leadgen_id: str) -> dict:
    token = getattr(settings, 'META_PAGE_ACCESS_TOKEN', '') or ''
    if not token:
        raise RuntimeError('Meta page access token is not configured (META_PAGE_ACCESS_TOKEN).')
    resp = requests.get(f'https://graph.facebook.com/v19.0/{leadgen_id}',
                        params={'access_token': token, 'fields': 'field_data,created_time,ad_name,form_id'}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _meta_fetch_and_process(inbox_id: int):
    item = LeadInbox.objects.get(pk=inbox_id)
    try:
        if 'field_data' not in item.payload:
            details = meta_fetch(item.payload['leadgen_id'])
            item.payload = {**item.payload, **details}
            item.save(update_fields=['payload'])
    except Exception as exc:
        item.last_error = f'Could not fetch lead details from Meta: {exc}'[:2000]
        item.status = 'failed'
        item.save(update_fields=['last_error', 'status'])
        return
    _process(item.pk)


# ---------------------------------------------------------------- telephony (missed calls)

@csrf_exempt
@require_http_methods(['POST'])
def telephony_event(request):
    """Missed call creates a lead with source Missed call and an immediate follow-up (Part 4.2)."""
    source = LeadSource.objects.filter(channel='missed_call', is_active=True).order_by('id').first()
    if source is None:
        return JsonResponse({'ok': False, 'error': 'Not configured'}, status=404)
    if not _verify(source, request):
        audit.security_event('invalid_signature', request=request, detail='telephony')
        return JsonResponse({'ok': False, 'error': 'Invalid signature'}, status=401)
    payload = _parse_body(request)
    caller = payload.get('caller') or payload.get('from') or payload.get('caller_number') or payload.get('phone') or ''
    event = (payload.get('event') or payload.get('status') or 'missed').lower()
    payload = {**payload, 'phone': caller, 'name': payload.get('name') or f'Caller {str(caller)[-4:]}',
               'message': f'{"Missed" if "miss" in event else "Completed"} call from {caller}'}
    item, created = store(source, request, payload, True)
    if created:
        transaction.on_commit(lambda: _process(item.pk))
    return JsonResponse({'ok': True, 'inbox_id': item.pk}, status=202 if created else 200)
