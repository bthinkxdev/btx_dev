"""Meta Conversions API for the webinar funnel (the server-side half of the Meta pixel).

Why both: the browser pixel is blocked by many ad blockers and iOS settings, and the payment is confirmed by a Cashfree
webhook when the buyer's browser is often already closed. So Lead, InitiateCheckout and Purchase are also sent from here.
Meta merges the two copies of an event when `event_name` and `event_id` match; the browser uses the Cashfree order id as
its `eventID`, and so does this module.

Each event is stored first (`MetaEvent`), then sent in the background, then retried by the `webinars.meta_retry` job when
Meta or the network fails. Personal data is hashed with SHA-256 as Meta requires; nothing here ever blocks a visitor.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

import requests
from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from .models import MetaEvent, MetaTracking, WebinarRegistration

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_AGE = timedelta(days=6)                  # Meta rejects events older than 7 days
TIMEOUT = 6
FBP_RE = re.compile(r'^fb\.\d\.\d{10,13}\.\d{4,20}$')
FBC_RE = re.compile(r'^fb\.\d\.\d{10,13}\.[\w-]{6,300}$')


@dataclass(frozen=True)
class Config:
    pixel_id: str
    token: str
    test_code: str
    version: str
    pixel_on: bool          # the browser pixel runs
    capi_on: bool           # events are also sent from the server
    source: str             # where the pixel id comes from: 'crm', 'environment' or 'none'


def config() -> Config:
    """The settings in force: what an administrator saved in the CRM, with the server environment (META_* variables) filling
    anything left empty. Read on every use, so a change in the CRM applies immediately."""
    from crm.core import crypto

    try:
        row = MetaTracking.objects.filter(pk=1).first()
    except Exception:                                   # e.g. before the migration has run
        row = None
    env_id = getattr(settings, 'META_PIXEL_ID', '')
    pixel = (row.pixel_id if row and row.pixel_id else env_id).strip()
    token = (crypto.decrypt(row.capi_token_enc) if row and row.capi_token_enc else '') or getattr(settings, 'META_CAPI_TOKEN', '')
    test_code = (row.test_event_code if row and row.test_event_code else getattr(settings, 'META_TEST_EVENT_CODE', ''))
    version = (row.graph_version if row and row.graph_version else getattr(settings, 'META_GRAPH_VERSION', 'v21.0'))
    pixel_flag = row.pixel_enabled if row else True
    capi_flag = row.capi_enabled if row else True
    return Config(pixel_id=pixel, token=token, test_code=test_code, version=version,
                  pixel_on=bool(pixel and pixel_flag), capi_on=bool(pixel and token and capi_flag),
                  source='crm' if row and row.pixel_id else ('environment' if env_id else 'none'))


def page_pixel_id() -> str:
    """The pixel id to put on public pages, or '' when the pixel is off."""
    cfg = config()
    return cfg.pixel_id if cfg.pixel_on else ''


def enabled() -> bool:
    """Server-side events need the pixel (dataset) id, an access token, and the switch on."""
    return config().capi_on


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _clean(value: str) -> str:
    return re.sub(r'[^\w]', '', value.strip().lower())


def capture_browser(request, reg: WebinarRegistration) -> None:
    """Remember what Meta needs to match this person to the ad click. Called while the visitor's browser is talking to us."""
    from crm.core.middleware import client_ip

    fbp = (request.COOKIES.get('_fbp') or '').strip()
    fbc = (request.COOKIES.get('_fbc') or '').strip()
    if not FBC_RE.match(fbc) and reg.ad_click_id:      # no cookie yet: build it from the ad click id, as Meta documents
        fbc = f'fb.1.{int(time.time() * 1000)}.{reg.ad_click_id}'
    reg.fbp = fbp if FBP_RE.match(fbp) else ''
    reg.fbc = fbc if FBC_RE.match(fbc) else ''
    reg.client_ip = client_ip(request) or None
    reg.user_agent = (request.META.get('HTTP_USER_AGENT') or '')[:400]
    reg.source_url = (request.META.get('HTTP_REFERER') or request.build_absolute_uri())[:500]


def _user_data(reg: WebinarRegistration) -> dict:
    parts = [_clean(p) for p in reg.name.split() if _clean(p)]
    data = {
        'em': [_hash(reg.email.strip().lower())] if reg.email else None,
        'ph': [_hash('91' + ''.join(ch for ch in reg.phone if ch.isdigit())[-10:])] if reg.phone else None,
        'fn': [_hash(parts[0])] if parts else None,
        'ln': [_hash(parts[-1])] if len(parts) > 1 else None,
        'country': [_hash('in')],
        'external_id': [_hash(f'webinar-registration-{reg.pk}')],
        'client_ip_address': reg.client_ip or None,
        'client_user_agent': reg.user_agent or None,
        'fbp': reg.fbp or None,
        'fbc': reg.fbc or None,
    }
    return {k: v for k, v in data.items() if v}


def build_event(name: str, reg: WebinarRegistration, when) -> dict:
    w = reg.webinar
    custom = {
        'currency': 'INR', 'value': float(reg.amount), 'content_name': w.title, 'content_ids': [w.slug],
        'content_type': 'product', 'num_items': 1,
    }
    if name == 'Purchase':
        custom['order_id'] = reg.cashfree_order_id
    return {
        'event_name': name, 'event_time': int(when.timestamp()), 'event_id': reg.cashfree_order_id,
        'action_source': 'website', 'event_source_url': reg.source_url or None,
        'user_data': _user_data(reg), 'custom_data': custom,
    }


def queue(name: str, reg: WebinarRegistration, when=None) -> MetaEvent | None:
    """Store an event for this registration (once per event name and order) and send it after the transaction commits.
    Returns None when server-side tracking is off. Never raises."""
    if not enabled():
        return None
    try:
        when = when or timezone.now()
        event, created = MetaEvent.objects.get_or_create(
            event_name=name, event_id=reg.cashfree_order_id,
            defaults={'registration': reg, 'event_time': when, 'payload': build_event(name, reg, when)})
        if created:
            transaction.on_commit(lambda: _dispatch(event.pk))
        return event
    except Exception:
        logger.exception('Could not queue Meta event %s for registration %s', name, reg.pk)
        return None


def _dispatch(pk: int) -> None:
    if not getattr(settings, 'META_CAPI_ASYNC', True):
        send(pk)
        return

    def run():
        close_old_connections()
        try:
            send(pk)
        finally:
            close_old_connections()

    threading.Thread(target=run, name=f'meta-capi-{pk}', daemon=True).start()


def _fail(pk: int, message: str) -> bool:
    MetaEvent.objects.filter(pk=pk).update(status=MetaEvent.Status.FAILED, last_error=message[:500])
    return False


def send(pk: int) -> bool:
    """Send one stored event. Safe to call twice: a sent event is never sent again. Returns True when Meta accepted it."""
    now = timezone.now()
    claimed = MetaEvent.objects.filter(pk=pk, status__in=[MetaEvent.Status.PENDING, MetaEvent.Status.FAILED],
                                       attempts__lt=MAX_ATTEMPTS).update(attempts=F('attempts') + 1, last_attempt_at=now)
    if not claimed:
        return False
    event = MetaEvent.objects.get(pk=pk)
    cfg = config()
    if not cfg.capi_on:
        return _fail(pk, 'Conversions API is not configured or is switched off.')
    if now - event.event_time > MAX_AGE:
        MetaEvent.objects.filter(pk=pk).update(attempts=MAX_ATTEMPTS)
        return _fail(pk, 'Too old: Meta only accepts events from the last 7 days.')

    body = {'data': [event.payload]}
    if cfg.test_code:
        body['test_event_code'] = cfg.test_code
    url = f'https://graph.facebook.com/{cfg.version}/{cfg.pixel_id}/events'
    try:
        # the token goes in a header so it can never appear in a logged URL or an exception message
        resp = requests.post(url, json=body, timeout=TIMEOUT, headers={'Authorization': f'Bearer {cfg.token}'})
    except requests.RequestException as exc:
        return _fail(pk, f'Network error: {exc.__class__.__name__}')
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 200 and data.get('events_received', 0) >= 1:
        MetaEvent.objects.filter(pk=pk).update(status=MetaEvent.Status.SENT, sent_at=timezone.now(), last_error='')
        return True
    err = (data.get('error') or {}) if isinstance(data, dict) else {}
    message = f'HTTP {resp.status_code}: {err.get("message") or "no event was accepted"}'
    if resp.status_code in (400, 401, 403) and err.get('code') in (100, 190, 200, 10):     # a retry cannot fix these
        MetaEvent.objects.filter(pk=pk).update(attempts=MAX_ATTEMPTS)
    logger.warning('Meta Conversions API rejected %s: %s', event.event_name, message)
    return _fail(pk, message)


def retry_pending() -> int:
    """Re-send events that are still queued or failed, with a growing wait between tries. Returns how many were sent."""
    if not enabled():
        return 0
    now = timezone.now()
    sent = 0
    due = MetaEvent.objects.filter(status__in=[MetaEvent.Status.PENDING, MetaEvent.Status.FAILED],
                                   attempts__lt=MAX_ATTEMPTS, created_at__gte=now - MAX_AGE).order_by('created_at')
    for event in due[:100]:
        wait = timedelta(minutes=2 ** event.attempts)
        if event.last_attempt_at and now - event.last_attempt_at < wait:
            continue
        if not event.last_attempt_at and now - event.created_at < timedelta(minutes=2):
            continue                      # the background send is probably still running
        sent += 1 if send(event.pk) else 0
    return sent


def send_test(request) -> tuple[bool, str]:
    """Send one harmless event to Meta's Test events tab, so an administrator can check the pixel id and token without
    making a registration. It needs the test event code, so it can never reach real ad reporting."""
    from crm.core.middleware import client_ip

    cfg = config()
    if not cfg.capi_on:
        return False, 'Add the pixel ID and the access token, and switch the Conversions API on, first.'
    if not cfg.test_code:
        return False, 'Enter the test event code from the Test events tab first. It keeps this event out of your ad reports.'
    event = {
        'event_name': 'PageView', 'event_time': int(time.time()), 'event_id': f'crm-test-{int(time.time())}',
        'action_source': 'website', 'event_source_url': request.build_absolute_uri('/webinar/'),
        'user_data': {'client_ip_address': client_ip(request) or '127.0.0.1',
                      'client_user_agent': (request.META.get('HTTP_USER_AGENT') or 'crm-test')[:400]},
    }
    url = f'https://graph.facebook.com/{cfg.version}/{cfg.pixel_id}/events'
    try:
        resp = requests.post(url, json={'data': [event], 'test_event_code': cfg.test_code}, timeout=TIMEOUT,
                             headers={'Authorization': f'Bearer {cfg.token}'})
    except requests.RequestException as exc:
        return False, f'Could not reach Meta ({exc.__class__.__name__}). Try again.'
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 200 and data.get('events_received', 0) >= 1:
        return True, 'Meta accepted the test event. Open the Test events tab in Events Manager to see it.'
    err = data.get('error') or {} if isinstance(data, dict) else {}
    return False, f'Meta refused it: {err.get("message") or "HTTP " + str(resp.status_code)}'
