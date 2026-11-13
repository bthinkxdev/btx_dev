"""Webinar landing page and the Cashfree payment flow behind it (register -> pay -> return / webhook)."""
from __future__ import annotations

import json
import logging

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import cashfree, meta, webinar, webinar_mail
from .models import WebinarRegistration

logger = logging.getLogger(__name__)

UTM_KEYS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content')


class WebinarRegistrationForm(forms.Form):
    name = forms.CharField(max_length=200)
    email = forms.EmailField(max_length=254)
    phone = forms.CharField(max_length=20)
    business_name = forms.CharField(max_length=200, required=False)
    website = forms.CharField(required=False)           # honeypot

    def clean_name(self):
        name = ' '.join(self.cleaned_data['name'].split())
        if len(name) < 2:
            raise ValidationError('Please enter your name.')
        return name

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()

    def clean_phone(self):
        digits = ''.join(ch for ch in self.cleaned_data['phone'] if ch.isdigit())
        if len(digits) == 12 and digits.startswith('91'):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith('0'):
            digits = digits[1:]
        if len(digits) != 10 or digits[0] not in '6789':
            raise ValidationError('Enter a valid 10-digit mobile number.')
        return digits

    def clean_business_name(self):
        return ' '.join(self.cleaned_data['business_name'].split())


def _tracking(source) -> dict:
    """Ad tracking values from a querystring or a form post (Meta ads add fbclid; UTMs are ours)."""
    out = {k: (source.get(k) or '').strip()[:150] for k in UTM_KEYS}
    out['ad_click_id'] = (source.get('fbclid') or source.get('ad_click_id') or '').strip()[:200]
    return out


def _event_schema(request, w) -> str:
    seats = w.seats_left()
    data = {
        '@context': 'https://schema.org', '@type': 'Event', 'name': w.title, 'description': w.subtitle,
        'startDate': timezone.localtime(w.starts_at).isoformat(), 'endDate': timezone.localtime(w.ends_at).isoformat(),
        'eventStatus': 'https://schema.org/EventScheduled',
        'eventAttendanceMode': 'https://schema.org/OnlineEventAttendanceMode',
        'location': {'@type': 'VirtualLocation', 'url': request.build_absolute_uri(reverse('pages:webinar', args=[w.slug]))},
        'image': request.build_absolute_uri('/static/assets/images/pwa/icon-512.png'),
        'organizer': {'@type': 'Organization', 'name': 'BThinkX', 'url': request.build_absolute_uri('/')},
        'performer': {'@type': 'Organization', 'name': w.host_name},
        'offers': {'@type': 'Offer', 'url': request.build_absolute_uri(reverse('pages:webinar', args=[w.slug])),
                   'price': f'{w.price:.0f}', 'priceCurrency': 'INR',
                   'availability': 'https://schema.org/SoldOut' if seats == 0 else 'https://schema.org/InStock',
                   'validFrom': timezone.localtime(w.created_at).isoformat()},
    }
    return json.dumps(data).replace('</', '<\\/')


@ensure_csrf_cookie
def landing(request, slug=None):
    w = webinar.active_webinar(slug)
    if w is None:
        raise Http404
    seats = w.seats_left()
    local = timezone.localtime(w.starts_at)
    end = timezone.localtime(w.ends_at)
    return render(request, 'pages/webinar.html', {
        'w': w, 'seats_left': seats, 'sold_out': seats == 0, 'closed': not w.registration_open,
        'date_long': local.strftime('%A, %d %B %Y'), 'date_short': local.strftime('%a, %d %b'),
        'time_range': f'{local.strftime("%I:%M %p").lstrip("0")} to {end.strftime("%I:%M %p").lstrip("0")} IST',
        'time_start': local.strftime('%I:%M %p').lstrip('0'),
        'starts_iso': local.isoformat(), 'tracking': _tracking(request.GET),
        'price': f'{w.price:,.0f}', 'cashfree_mode': cashfree.sdk_mode(),
        'meta_pixel_id': meta.page_pixel_id(), 'event_schema': _event_schema(request, w),
        'learn_items': w.learn_items, 'audience_items': w.audience_items,
    })


@require_POST
def register(request, slug):
    from crm.core import ratelimit
    from crm.core.middleware import client_ip

    w = webinar.active_webinar(slug)
    if w is None:
        raise Http404
    if not w.registration_open:
        return JsonResponse({'success': False, 'error': 'Registration for this webinar has closed.'}, status=400)
    if w.seats_left() == 0:
        return JsonResponse({'success': False, 'error': 'All seats for this webinar are taken.'}, status=400)
    if not ratelimit.hit('webinar-register', client_ip(request) or '', 10, 3600):
        return JsonResponse({'success': False, 'error': 'Too many attempts. Please try again in a little while.'}, status=429)

    form = WebinarRegistrationForm(request.POST)
    if not form.is_valid():
        return JsonResponse({'success': False, 'errors': {k: v[0] for k, v in form.errors.items()}}, status=400)
    d = form.cleaned_data
    if d['website']:                                     # honeypot tripped: pretend it went through
        return JsonResponse({'success': False, 'error': 'Please try again.'}, status=400)
    if not cashfree.is_configured():
        logger.error('Webinar registration attempted but Cashfree is not configured.')
        return JsonResponse({'success': False, 'error': 'Payments are temporarily unavailable. Please try again shortly.'},
                            status=503)

    # Already paid with this email or number: never charge twice, just send the confirmation again.
    paid = (WebinarRegistration.objects.filter(webinar=w, status=WebinarRegistration.Status.PAID)
            .filter(email=d['email']).first()
            or WebinarRegistration.objects.filter(webinar=w, status=WebinarRegistration.Status.PAID, phone=d['phone']).first())
    if paid:
        if ratelimit.hit('webinar-resend', str(paid.pk), 1, 600):
            webinar_mail.send_confirmation(paid)
        return JsonResponse({'success': False, 'already': True,
                             'error': f'You are already registered with this {"email" if paid.email == d["email"] else "number"}. '
                                      f'We have sent your confirmation again to {paid.email}.'}, status=409)

    track = _tracking(request.POST)
    order_id = webinar.new_order_id()
    # Someone retrying after an abandoned checkout reuses their earlier row instead of piling up duplicates.
    reg = (WebinarRegistration.objects.filter(webinar=w, email=d['email'])
           .exclude(status=WebinarRegistration.Status.PAID).order_by('-created_at').first())
    if reg is None:
        reg = WebinarRegistration(webinar=w)
    reg.name, reg.email, reg.phone, reg.business_name = d['name'], d['email'], d['phone'], d['business_name']
    reg.amount = w.price
    reg.status = WebinarRegistration.Status.PENDING
    reg.cashfree_order_id = order_id
    for key, value in track.items():
        if value:
            setattr(reg, key, value)
    meta.capture_browser(request, reg)
    reg.save()

    notify_url = request.build_absolute_uri(reverse('pages:webinar_webhook'))
    return_url = request.build_absolute_uri(reverse('pages:webinar_return')) + '?order_id={order_id}'
    try:
        order = cashfree.create_order(
            order_id=order_id, amount=reg.amount, customer_id=f'web{reg.pk}', customer_phone=reg.phone,
            customer_name=reg.name, customer_email=reg.email, return_url=return_url, notify_url=notify_url,
            order_note=w.title)
    except cashfree.CashfreeError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=502)

    session_id = order.get('payment_session_id')
    if not session_id:
        logger.error('Cashfree order created without payment_session_id: %s', order)
        return JsonResponse({'success': False, 'error': 'Could not start payment. Please try again.'}, status=502)

    webinar._to_crm('webinar_registration', reg)
    meta.queue('Lead', reg)
    meta.queue('InitiateCheckout', reg)
    return JsonResponse({'success': True, 'order_id': order_id, 'payment_session_id': session_id,
                         'mode': cashfree.sdk_mode()})


@never_cache
def payment_return(request):
    order_id = (request.GET.get('order_id') or '').strip()
    reg = WebinarRegistration.objects.select_related('webinar').filter(cashfree_order_id=order_id).first()
    status = 'not_found'
    if reg:
        status = reg.status
        if reg.status == WebinarRegistration.Status.PENDING:
            status = webinar.reconcile(reg)
            reg.refresh_from_db()
    w = reg.webinar if reg else webinar.active_webinar()
    local = timezone.localtime(w.starts_at) if w else None
    return render(request, 'pages/webinar_result.html', {
        'reg': reg, 'status': status, 'w': w, 'meta_pixel_id': meta.page_pixel_id(),
        'date_long': local.strftime('%A, %d %B %Y') if local else '',
        'time_range': (f'{local.strftime("%I:%M %p").lstrip("0")} to '
                       f'{timezone.localtime(w.ends_at).strftime("%I:%M %p").lstrip("0")} IST') if local else '',
    })


@csrf_exempt
@require_POST
def handle_webhook(request):
    """Cashfree server-to-server confirmation: the authoritative signal, because a visitor can close the browser
    before the return page loads. Also called from the older offer webhook, so one dashboard URL serves both."""
    timestamp = request.headers.get('x-webhook-timestamp', '')
    signature = request.headers.get('x-webhook-signature', '')
    if not cashfree.verify_webhook_signature(request.body, timestamp, signature):
        logger.warning('Rejected Cashfree webhook with invalid signature.')
        return HttpResponseForbidden('Invalid signature')
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return HttpResponse(status=400)
    apply_webhook(payload)
    return HttpResponse(status=200)


def apply_webhook(payload: dict) -> bool:
    """Apply a verified webhook payload. Returns True when it belonged to a webinar registration."""
    data = payload.get('data') or {}
    order_id = (data.get('order') or {}).get('order_id')
    payment = data.get('payment') or {}
    reg = WebinarRegistration.objects.filter(cashfree_order_id=order_id).first() if order_id else None
    if reg is None:
        return False
    state = payment.get('payment_status')
    if state == 'SUCCESS':
        webinar.mark_paid(reg.pk, str(payment.get('cf_payment_id') or ''))
    elif state in ('FAILED', 'USER_DROPPED'):
        webinar.mark_failed(reg.pk)
    return True


@require_GET
def calendar_file(request):
    order_id = (request.GET.get('order_id') or '').strip()
    reg = WebinarRegistration.objects.select_related('webinar').filter(
        cashfree_order_id=order_id, status=WebinarRegistration.Status.PAID).first()
    if reg is None:
        raise Http404
    response = HttpResponse(webinar_mail.build_ics(reg.webinar, reg), content_type='text/calendar; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="webinar.ics"'
    return response
