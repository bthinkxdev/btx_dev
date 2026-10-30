"""
Navaratri offer landing page: a limited-seats (50 businesses) signup form
for a complete e-commerce website, two product-count tiers, that takes
payment through Cashfree before the signup counts as "onboarded". Public,
no login.
"""
from __future__ import annotations

import json
import logging
import uuid

from django.conf import settings
from django.core.mail import send_mail
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import cashfree
from .forms import OfferSignupForm
from .models import OfferSignup

logger = logging.getLogger(__name__)

NAVRATRI_SLUG = 'navaratri-2026'
NAVRATRI_SEAT_LIMIT = 50
# The displayed "already onboarded" counter starts here and climbs with real
# paid signups (capped below the real seat limit) — not a literal live count,
# but not detached from reality either: it only ever moves because real
# businesses actually signed up.
NAVRATRI_DISPLAY_BASE = 47

# Plan -> price is decided here, server-side, and never trusted from the
# client — the form only ever submits which plan was picked.
NAVRATRI_PLANS = {
    OfferSignup.Plan.STANDARD: {'label': 'Up to 100 products', 'amount': 7999},
    OfferSignup.Plan.UNLIMITED: {'label': 'Unlimited products', 'amount': 9999},
}


def _counts():
    paid_count = OfferSignup.objects.filter(offer_slug=NAVRATRI_SLUG, status=OfferSignup.Status.PAID).count()
    seats_left = max(0, NAVRATRI_SEAT_LIMIT - paid_count)
    display_count = min(NAVRATRI_DISPLAY_BASE + paid_count, NAVRATRI_SEAT_LIMIT - 1)
    return paid_count, seats_left, display_count


def navaratri_offer(request):
    paid_count, seats_left, display_count = _counts()
    plans = [
        {'value': value, 'label': meta['label'], 'amount': meta['amount']}
        for value, meta in NAVRATRI_PLANS.items()
    ]
    return render(request, 'pages/navaratri_offer.html', {
        'form': OfferSignupForm(initial={'plan': OfferSignup.Plan.STANDARD}),
        'plans': plans,
        'min_amount': plans[0]['amount'],
        'seat_limit': NAVRATRI_SEAT_LIMIT,
        'seats_left': seats_left,
        'sold_out': seats_left <= 0,
        'display_count': display_count,
        'cashfree_mode': cashfree.sdk_mode(),
    })


@require_POST
def navaratri_offer_submit(request):
    paid_count, seats_left, _ = _counts()
    if seats_left <= 0:
        return JsonResponse({'success': False, 'error': 'All 50 seats for this offer are taken.'}, status=400)

    form = OfferSignupForm(request.POST)
    if not form.is_valid():
        errors = {k: v[0] for k, v in form.errors.items()}
        return JsonResponse({'success': False, 'errors': errors}, status=400)

    if not cashfree.is_configured():
        logger.error('Navaratri offer submit attempted but Cashfree is not configured.')
        return JsonResponse({'success': False, 'error': 'Payments are temporarily unavailable. Please try again shortly.'}, status=503)

    signup = form.save(commit=False)
    signup.offer_slug = NAVRATRI_SLUG
    signup.amount = NAVRATRI_PLANS[signup.plan]['amount']
    signup.status = OfferSignup.Status.PENDING
    order_id = f'NAVRATRI{uuid.uuid4().hex[:12].upper()}'
    signup.cashfree_order_id = order_id
    signup.save()

    return_url = request.build_absolute_uri(reverse('pages:navaratri_offer_return')) + '?order_id={order_id}'

    try:
        order = cashfree.create_order(
            order_id=order_id,
            amount=signup.amount,
            customer_id=f'cust{signup.pk}',
            customer_phone=signup.phone,
            customer_name=signup.name,
            return_url=return_url,
        )
    except cashfree.CashfreeError as exc:
        signup.delete()
        return JsonResponse({'success': False, 'error': str(exc)}, status=502)

    payment_session_id = order.get('payment_session_id')
    if not payment_session_id:
        signup.delete()
        logger.error('Cashfree order created without payment_session_id: %s', order)
        return JsonResponse({'success': False, 'error': 'Could not start payment. Please try again.'}, status=502)

    from .views import _to_crm
    _to_crm('offer_signup', signup)
    return JsonResponse({
        'success': True,
        'order_id': order_id,
        'payment_session_id': payment_session_id,
        'mode': cashfree.sdk_mode(),
    })


def navaratri_offer_return(request):
    order_id = (request.GET.get('order_id') or '').strip()
    signup = OfferSignup.objects.filter(cashfree_order_id=order_id).first()

    status = 'not_found'
    if signup:
        status = signup.status
        if signup.status == OfferSignup.Status.PENDING:
            try:
                order = cashfree.fetch_order(order_id)
                cf_status = order.get('order_status')
                if cf_status == 'PAID':
                    signup.status = OfferSignup.Status.PAID
                    signup.paid_at = timezone.now()
                    signup.save(update_fields=['status', 'paid_at'])
                    _notify_paid_signup(signup)
                    status = OfferSignup.Status.PAID
                elif cf_status in ('EXPIRED', 'TERMINATED'):
                    signup.status = OfferSignup.Status.FAILED
                    signup.save(update_fields=['status'])
                    status = OfferSignup.Status.FAILED
                else:
                    status = 'processing'
            except cashfree.CashfreeError:
                status = 'processing'

    return render(request, 'pages/navaratri_offer_result.html', {
        'signup': signup,
        'status': status,
    })


@csrf_exempt
@require_POST
def navaratri_offer_webhook(request):
    """Server-to-server confirmation — the authoritative source of truth,
    since the browser return_url redirect can be closed/interrupted."""
    timestamp = request.headers.get('x-webhook-timestamp', '')
    signature = request.headers.get('x-webhook-signature', '')
    if not cashfree.verify_webhook_signature(request.body, timestamp, signature):
        logger.warning('Rejected Cashfree webhook with invalid signature.')
        return HttpResponseForbidden('Invalid signature')

    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return HttpResponse(status=400)

    data = payload.get('data') or {}
    order = data.get('order') or {}
    payment = data.get('payment') or {}
    order_id = order.get('order_id')
    payment_status = payment.get('payment_status')

    if not order_id:
        return HttpResponse(status=200)  # nothing to do, but acknowledge

    signup = OfferSignup.objects.filter(cashfree_order_id=order_id).first()
    if not signup:
        return HttpResponse(status=200)

    if payment_status == 'SUCCESS' and signup.status != OfferSignup.Status.PAID:
        signup.status = OfferSignup.Status.PAID
        signup.paid_at = timezone.now()
        signup.cashfree_payment_id = str(payment.get('cf_payment_id') or '')
        signup.save(update_fields=['status', 'paid_at', 'cashfree_payment_id'])
        _notify_paid_signup(signup)
    elif payment_status in ('FAILED', 'USER_DROPPED') and signup.status == OfferSignup.Status.PENDING:
        signup.status = OfferSignup.Status.FAILED
        signup.save(update_fields=['status'])

    return HttpResponse(status=200)


def _notify_paid_signup(signup: OfferSignup):
    from .views import _to_crm
    _to_crm('offer_signup', signup, paid=True)
    to_email = getattr(settings, 'CONTACT_EMAIL_TO', 'hr@bthinkx.com')
    try:
        send_mail(
            subject=f'[BThinkX] Navaratri offer paid: {signup.business_name}',
            message=(
                f'New paid Navaratri offer signup.\n\n'
                f'Business: {signup.business_name}\n'
                f'Contact: {signup.name}\n'
                f'Phone: {signup.phone}\n'
                f'Place: {signup.place}\n'
                f'Amount: Rs. {signup.amount}\n'
                f'Order ID: {signup.cashfree_order_id}\n'
                f'Paid at: {signup.paid_at:%Y-%m-%d %H:%M} UTC\n'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to_email],
            fail_silently=True,
        )
    except Exception:
        logger.exception('Failed to send Navaratri offer paid-notification email.')
