"""
Customer-facing payment page and gateway webhooks (Part 6.7, 6.8). No login.

* The page is found only by an unguessable token; anything else is a generic 404.
* It never shows internal notes, staff names, margin or other clients.
* Webhooks verify the signature on the raw body, store the event, then answer 200.
"""
from __future__ import annotations

import json
import logging

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from crm.core import audit, pdf, ratelimit
from crm.core.middleware import client_ip
from crm.models import CompanySettings, PaymentAttempt
from crm.models.payments import P_COUNTED, PL_CANCELLED, PL_EXPIRED, PL_PAID, PL_PART_REFUNDED, PL_REFUNDED

from . import gateway
from . import services as svc

logger = logging.getLogger('crm.payments')


def _limited(request, bucket: str, limit: int = 60) -> bool:
    return not ratelimit.hit(bucket, client_ip(request) or '', limit=limit, window_seconds=60)


def _get_link(token):
    link = svc.by_token(token)
    if link is None or link.status in ('draft', 'pending_approval'):
        raise Http404
    return link


def _state(link, attempt=None) -> str:
    if link.status == PL_CANCELLED:
        return 'cancelled'
    if link.status in (PL_PAID, PL_REFUNDED, PL_PART_REFUNDED):
        return 'success'
    if link.status == PL_EXPIRED or link.expires_at <= timezone.now():
        return 'expired'
    if link.attempts.filter(status__in=['pending', 'authorised']).exists():
        return 'processing'
    if attempt is not None and attempt.status in ('failed', 'abandoned'):
        return 'failed'
    if link.amount_paid > 0:
        return 'partial'
    return 'active'


def pay_page(request, token):
    if _limited(request, 'pay-page'):
        return HttpResponse('Too many requests. Try again in a minute.', status=429)
    link = _get_link(token)
    company = CompanySettings.get()
    error = ''
    adapter = gateway.get_adapter()
    if request.method == 'POST':
        if _limited(request, 'pay-start', 10):
            return HttpResponse('Too many requests. Try again in a minute.', status=429)
        return_url = request.build_absolute_uri(reverse('crm:pay_return', args=[token])) + '?order=__ORDER__'
        notify_url = request.build_absolute_uri(reverse('crm:gateway_webhook', args=[adapter.provider]))
        try:
            attempt, session = svc.start_attempt(link, amount=request.POST.get('amount'), ip=client_ip(request) or '',
                                                 return_url=return_url, notify_url=notify_url)
        except svc.PaymentError as exc:
            error = ' '.join(exc.messages)
        else:
            if session.provider == 'sandbox':
                return redirect('crm:pay_sandbox', token=token, order=attempt.gateway_order_id)
            return render(request, 'crm/payments/public_checkout.html', {
                'link': link, 'company': company, 'session_id': session.session_id, 'mode': session.mode,
                'order': attempt.gateway_order_id, 'token': token, 'amount': attempt.amount})
    else:
        svc.record_open(link, client_ip(request) or '')
        link.refresh_from_db()
    attempt = None
    order = request.GET.get('order')
    if order:
        attempt = link.attempts.filter(gateway_order_id=order).first()
    state = _state(link, attempt)
    ready, why = adapter.ready()
    if not ready and state in ('active', 'partial', 'failed'):
        state = 'unavailable'
    paid = [p for p in link.payments.filter(status__in=P_COUNTED)]
    return render(request, 'crm/payments/public_pay.html', {
        'link': link, 'company': company, 'tax': link.tax_snapshot, 'state': state, 'attempt': attempt, 'error': error,
        'token': token, 'payments': paid, 'balance': link.balance, 'test_mode': gateway.is_test_mode(),
        'source_ref': link.source_label, 'due': link.invoice.due_date if link.invoice_id else None,
    })


def pay_return(request, token):
    """Customer comes back from the gateway: ask the gateway, never trust the browser."""
    link = _get_link(token)
    order = request.GET.get('order', '')[:80]
    attempt = link.attempts.filter(gateway_order_id=order).first()
    if attempt is not None and attempt.status not in ('captured',):
        svc.sync_attempt(attempt, source='return')
    url = reverse('crm:pay_page', args=[token])
    return redirect(f'{url}?order={order}' if attempt else url)


def pay_status(request, token):
    """Polled by the page while confirming (JSON)."""
    if _limited(request, 'pay-status', 120):
        return JsonResponse({'state': 'processing'}, status=429)
    link = _get_link(token)
    order = request.GET.get('order', '')[:80]
    attempt = link.attempts.filter(gateway_order_id=order).first() if order else None
    if attempt is not None and attempt.status in ('initiated', 'pending'):
        svc.sync_attempt(attempt, source='return')
        link.refresh_from_db()
        attempt.refresh_from_db()
    return JsonResponse({'state': _state(link, attempt)})


def pay_receipt(request, token, pk):
    link = _get_link(token)
    p = link.payments.filter(pk=pk, status__in=P_COUNTED).first()
    if p is None:
        raise Http404
    resp = HttpResponse(pdf.receipt_pdf(p), content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="{p.receipt_number.replace("/", "-")}.pdf"'
    return resp


def pay_sandbox(request, token, order):
    """Built-in test checkout: lets a tester choose the outcome; sends a signed event through the real webhook pipeline."""
    adapter = gateway.get_adapter()
    if adapter.provider != 'sandbox':
        raise Http404
    link = _get_link(token)
    attempt = PaymentAttempt.objects.filter(link=link, gateway_order_id=order).first()
    if attempt is None:
        raise Http404
    if request.method == 'POST':
        outcome = request.POST.get('outcome')
        if outcome in ('success', 'failure', 'dropped') and attempt.status in ('initiated', 'pending'):
            raw = adapter.simulated_event(attempt, outcome)
            ts = str(int(timezone.now().timestamp()))
            headers = {'x-webhook-timestamp': ts, 'x-webhook-signature': adapter.sign(raw, ts)}
            _ingest('sandbox', raw, headers)
        return redirect(f'{reverse("crm:pay_page", args=[token])}?order={order}')
    return render(request, 'crm/payments/public_sandbox.html', {'link': link, 'attempt': attempt, 'token': token,
                                                                 'company': CompanySettings.get()})


def _ingest(provider: str, raw: bytes, headers) -> tuple[int, str]:
    adapter = gateway.adapter_for(provider)
    ok, _ = adapter.ready()
    if not ok:
        return 404, 'not configured'
    if not adapter.verify_signature(raw, headers):
        audit.security_event('webhook_bad_signature', detail=f'{provider} {len(raw)} bytes')
        return 401, 'invalid signature'
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return 400, 'invalid body'
    ev = adapter.parse_webhook(payload, raw)
    if ev is None:
        return 200, 'ignored'
    row = svc.store_event(provider, ev, payload=payload, raw_body=raw.decode('utf-8', 'replace'), signature_valid=True)
    if row is None:
        return 200, 'duplicate'
    # Stored: the gateway gets 200 even if processing fails (the reprocess job retries).
    from django.db import transaction
    transaction.on_commit(lambda: svc.process_event(row.pk))
    return 200, 'stored'


@csrf_exempt
@require_POST
def gateway_webhook(request, provider):
    if provider not in ('cashfree', 'sandbox'):
        raise Http404
    if _limited(request, f'webhook-{provider}', 600):
        return HttpResponse(status=429)
    raw = request.body
    if len(raw) > 512 * 1024:
        return HttpResponse(status=413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    status, msg = _ingest(provider, raw, headers)
    return JsonResponse({'status': msg}, status=status)
