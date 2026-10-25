"""
Payment links, gateway events, payments, refunds, settlements (Part 6, 7.5, 7.6).

Core rule: generating a link is not receiving money. Link created, customer attempt,
gateway-confirmed payment, settlement and bank reconciliation are separate records.

Lock order everywhere: payment link -> attempt -> payment -> invoice (Part 14.3).
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, audit, crypto, gst, india, notify, numbering, pdf, permissions, posting
from crm.core.money import D, ZERO, q2
from crm.models import (CompanySettings, Dispute, GatewayEvent, Invoice, Payment, PaymentAllocation, PaymentAttempt,
                        PaymentLink, ReceiptVoucher, Refund, Settlement, TdsReceivable)
from crm.models.billing import INV_OPEN
from crm.models.payments import (GATEWAY_METHODS, P_COUNTED, P_PART_REFUNDED, P_PENDING, P_REFUNDED, P_REJECTED,
                                 P_REVIEW, P_VERIFIED, PL_ACTIVE, PL_CANCELLED, PL_DRAFT, PL_EXPIRED, PL_PAID, PL_PART_REFUNDED,
                                 PL_PARTIAL, PL_PAYABLE, PL_PENDING, PL_REFUNDED)

from . import gateway

logger = logging.getLogger('crm.payments')

QUOTATION_LINKABLE = ('approved', 'sent', 'viewed', 'accepted')


class PaymentError(ValidationError):
    pass


def link_url(link) -> str:
    return reverse('crm:link_detail', args=[link.pk])


def payment_url(p) -> str:
    return reverse('crm:payment_detail', args=[p.pk])


def finance_users():
    return notify.users_with_perm('payments', 'approve')


# ---------------------------------------------------------------- tokens

def _new_token(link: PaymentLink):
    link.token_nonce = crypto.new_nonce()
    link.token_hash = crypto.token_hash(crypto.token_from_nonce('paylink', link.token_nonce))


def public_token(link: PaymentLink) -> str:
    return crypto.token_from_nonce('paylink', link.token_nonce)


def public_url(link: PaymentLink, request=None) -> str:
    path = reverse('crm:pay_page', args=[public_token(link)])
    return request.build_absolute_uri(path) if request is not None else settings.SITE_BASE_URL + path


def by_token(token: str):
    if not token or len(token) > 200:
        return None
    return PaymentLink.objects.filter(token_hash=crypto.token_hash(token)).select_related('client', 'invoice', 'quotation').first()


# ---------------------------------------------------------------- eligibility (6.2)

def _quotation_collected(q) -> Decimal:
    versions = q.all_versions()
    agg = Payment.objects.filter(quotation__in=versions, status__in=P_COUNTED + [P_REVIEW]).aggregate(a=Sum('amount'), r=Sum('refunded_amount'))
    return (agg['a'] or ZERO) - (agg['r'] or ZERO)


def collectable(source) -> Decimal:
    """Most that may still be collected against the source."""
    from crm.models import Quotation
    if isinstance(source, Invoice):
        pending_links = ZERO
        return max(source.balance - pending_links, ZERO)
    if isinstance(source, Quotation):
        return max(source.grand_total - _quotation_collected(source), ZERO)
    raise PaymentError('Unsupported source.')


def open_link_for(source, purpose: str):
    qs = PaymentLink.objects.filter(status__in=PL_PAYABLE + [PL_PENDING, PL_DRAFT])
    from crm.models import Quotation
    if isinstance(source, Invoice):
        qs = qs.filter(invoice=source)
    elif isinstance(source, Quotation):
        qs = qs.filter(quotation__in=source.all_versions())
    return qs.filter(purpose=purpose).first()


def eligibility_errors(source) -> list[str]:
    from crm.models import Quotation
    errs = []
    adapter = gateway.get_adapter()
    ok, why = adapter.ready()
    if not ok:
        errs.append(why)
    if isinstance(source, Quotation):
        if source.status not in QUOTATION_LINKABLE:
            errs.append('The quotation must be approved, sent, viewed or accepted first.')
        if source.is_expired_by_date or source.status == 'expired':
            errs.append('Quotation expired. Create a new version first.')
        state = source.place_of_supply or source.customer_state
        if not state and source.customer_type != 'overseas':
            errs.append('Add customer state to determine tax.')
        if source.customer_type == 'registered' and not source.customer_gstin:
            errs.append('Add the customer GSTIN (B2B).')
        if source.client_id and source.client.is_restricted:
            errs.append('This client is restricted.')
    elif isinstance(source, Invoice):
        if source.status not in INV_OPEN or source.balance <= 0:
            errs.append('Only an issued invoice with a balance can have a payment link.')
        if source.disputed:
            errs.append('This invoice is disputed. Resolve the dispute first.')
    if collectable(source) <= 0:
        errs.append('Nothing is left to collect.')
    return errs


def suggested_amount(source, purpose: str, milestone=None) -> Decimal:
    from crm.models import Quotation
    cap = collectable(source)
    if purpose == 'advance' and isinstance(source, Quotation):
        return min(source.advance_amount or cap, cap)
    if purpose == 'milestone' and milestone is not None and isinstance(source, Quotation):
        return min(q2(source.grand_total * D(milestone.percent) / 100), cap)
    return cap


def tax_breakup(source, amount: Decimal) -> dict:
    """Tax share inside a collected amount, proportional to the source's frozen tax (Part 6.3)."""
    amount = q2(amount)
    total = source.grand_total
    if not total:
        return {'taxable': str(amount), 'cgst': '0.00', 'sgst': '0.00', 'igst': '0.00', 'total': str(amount), 'supply_type': source.supply_type}
    sp = gst.proportional_split(amount, total, source.cgst_total, source.sgst_total, source.igst_total)
    cgst, sgst, igst, taxable = sp['cgst'], sp['sgst'], sp['igst'], sp['taxable']
    rates = sorted({str(l.tax_rate.normalize()) for l in source.lines.all() if l.taxability == 'taxable'})
    return {'taxable': str(taxable), 'cgst': str(cgst), 'sgst': str(sgst), 'igst': str(igst), 'total': str(amount),
            'supply_type': source.supply_type, 'rates': rates, 'source_total': str(total),
            'source_tax': str(source.tax_total), 'place_of_supply': source.place_of_supply}


def approval_reasons(source, actor, *, amount: Decimal, expires_at) -> list[str]:
    from crm.models import Quotation
    c = CompanySettings.get()
    reasons = []
    can_approve = permissions.has_perm(actor, 'payment_links', 'approve')
    if amount > c.exec_payment_link_limit and not can_approve:
        reasons.append(f'Amount above {pdf._money(c.exec_payment_link_limit)} needs manager approval')
    if expires_at > timezone.now() + timedelta(days=c.payment_link_max_expiry_days, minutes=5):
        reasons.append(f'Expiry beyond {c.payment_link_max_expiry_days} days')
    if isinstance(source, Quotation) and timezone.localtime(expires_at).date() > source.valid_until:
        reasons.append('Expiry after the quotation validity date')
    return reasons


# ---------------------------------------------------------------- creation (6.5)

def create_link(source, actor, *, purpose: str, amount: Decimal, description: str, expires_at, allow_partial: bool = False,
                min_partial_amount=None, methods=None, contact_name='', contact_email='', contact_phone='',
                internal_note='', milestone_name='', allow_multiple: bool = False, request_message: str = '') -> PaymentLink:
    from crm.models import Quotation
    amount = q2(amount)
    c = CompanySettings.get()
    with transaction.atomic():
        if isinstance(source, Invoice):
            source = Invoice.objects.select_for_update().get(pk=source.pk)
        else:
            source = Quotation.objects.select_for_update().get(pk=source.pk)
        errs = eligibility_errors(source)
        if errs:
            raise PaymentError(' '.join(errs))
        cap = collectable(source)
        if amount <= 0:
            raise PaymentError('Enter an amount above ₹0.')
        if amount > cap:
            raise PaymentError(f'Maximum collectable is {pdf._money(cap)}.')
        if purpose == 'partial' and amount < cap and amount < c.min_partial_payment:
            raise PaymentError(f'A partial amount cannot be below {pdf._money(c.min_partial_payment)}.')
        if allow_partial:
            min_partial_amount = q2(min_partial_amount or c.min_partial_payment)
            if min_partial_amount > amount:
                raise PaymentError('The minimum partial amount cannot be more than the link amount.')
        if expires_at <= timezone.now():
            raise PaymentError('Choose an expiry in the future.')
        existing = open_link_for(source, purpose)
        if existing and not allow_multiple:
            raise PaymentError(f'An active link already exists ({existing.code}). Resend it or cancel it first.')
        if not description.strip():
            raise PaymentError('Add the customer-visible description.')
        enabled = c.gateway_methods or GATEWAY_METHODS
        methods = [m for m in (methods or enabled) if m in enabled]
        if isinstance(source, Quotation):
            client = source.client
            if client is None:
                if source.lead_id is None:
                    raise PaymentError('This quotation has no client or lead.')
                from crm.modules.clients.services import ensure_client_for_lead
                client = ensure_client_for_lead(source.lead, actor)
                source.refresh_from_db()
        else:
            client = source.client
        if not (contact_email or contact_phone):
            contact = client.billing_contact or client.primary_contact
            contact_name = contact_name or (contact.name if contact else client.legal_name)
            contact_email = contact.email if contact and contact.email else client.email
            contact_phone = contact.phone if contact and contact.phone else client.phone
        if not (contact_email or contact_phone):
            raise PaymentError('The contact must have a phone number or email.')
        link = PaymentLink(
            code=numbering.allocate_simple('PL'), client=client, owner=actor, purpose=purpose, amount=amount,
            milestone_name=milestone_name[:120], description=description.strip()[:200], expires_at=expires_at,
            allow_partial=allow_partial, min_partial_amount=min_partial_amount if allow_partial else None, methods=methods,
            internal_note=internal_note[:500], contact_name=contact_name[:120], contact_email=contact_email or '',
            contact_phone=contact_phone or '', price_mode=source.price_mode, tax_snapshot=tax_breakup(source, amount),
            gst_overridden=source.gst_overridden, provider=gateway.get_adapter().provider)
        if isinstance(source, Invoice):
            link.invoice = source
        else:
            link.quotation = source
        _new_token(link)
        reasons = approval_reasons(source, actor, amount=amount, expires_at=expires_at)
        link.status = PL_PENDING if reasons else PL_ACTIVE
        if not reasons:
            link.activated_at = timezone.now()
        link.save()
        req = None
        if reasons:
            req = approvals.submit('payment_link', link, title=f'Payment link {link.code} for {pdf._money(amount)}',
                                   module='payment_links', requested_by=actor, level='manager', amount=amount,
                                   reasons=reasons, after={'amount': str(amount), 'expires': expires_at.isoformat()},
                                   message=request_message)
        audit.log('payment_link.created', link, actor=actor, changes={
            'amount': str(amount), 'tax': link.tax_snapshot, 'expires_at': expires_at, 'source': link.source_label,
            'purpose': purpose, 'override': link.gst_overridden, 'approval': req.pk if req else None})
    return link


def _link_approved(req, user):
    with transaction.atomic():
        link = PaymentLink.objects.select_for_update().get(pk=req.object_id)
        if link.status == PL_PENDING:
            link.status = PL_ACTIVE
            link.activated_at = timezone.now()
            link.save(update_fields=['status', 'activated_at'])
            audit.log('payment_link.activated', link, actor=user)


def _link_rejected(req, user):
    PaymentLink.objects.filter(pk=req.object_id, status=PL_PENDING).update(status=PL_DRAFT)


approvals.register('payment_link', _link_approved, _link_rejected)


def send_link(link: PaymentLink, actor, *, to: str, message: str = '', request=None):
    from crm.models import MessageTemplate
    if link.status not in PL_PAYABLE:
        raise PaymentError('Only an active link can be sent.')
    if not to:
        raise PaymentError('Enter an email address.')
    company = CompanySettings.get()
    url = public_url(link, request)
    tpl = MessageTemplate.objects.filter(code='payment_link_send', is_active=True).order_by('-version').first()
    ctx = {'name': link.contact_name or str(link.client), 'amount': pdf._money(link.balance), 'link': url,
           'description': link.description, 'expires': timezone.localtime(link.expires_at).strftime('%d-%b-%Y %H:%M'),
           'sender': actor.get_full_name() or actor.username, 'company': company.legal_name}
    subject = notify.render(tpl.subject, ctx) if tpl else f'Payment request: {link.description}'
    body = (message + '\n\n' if message else '') + (notify.render(tpl.body, ctx) if tpl else
                                                    f'Please pay {ctx["amount"]} using this secure link: {url}\nThe link expires on {ctx["expires"]}.')
    notify.send_email(to, subject, body, sent_by=actor, obj=link, template_code='payment_link_send')
    PaymentLink.objects.filter(pk=link.pk).update(sent_at=timezone.now(), last_event=f'Sent to {to}'[:120])
    audit.log('payment_link.sent', link, actor=actor, changes={'to': to})


def cancel_link(link: PaymentLink, actor, reason: str):
    if len(reason.strip()) < 10:
        raise PaymentError('Give a reason of at least 10 characters.')
    with transaction.atomic():
        link = PaymentLink.objects.select_for_update().get(pk=link.pk)
        if link.status not in (PL_ACTIVE, PL_PARTIAL, PL_DRAFT, PL_PENDING):
            raise PaymentError('This link cannot be cancelled in its current status.')
        # Check open attempts with the gateway first: money already taken must be recorded, not lost.
        for a in link.attempts.filter(status__in=['initiated', 'pending', 'authorised']):
            sync_attempt(a)
        link.refresh_from_db()
        if link.attempts.filter(status__in=['pending', 'authorised']).exists():
            raise PaymentError('A payment is being processed at the gateway. Try again in a few minutes.')
        if link.status not in (PL_ACTIVE, PL_PARTIAL, PL_DRAFT, PL_PENDING):
            raise PaymentError('The link was paid while cancelling. Nothing was cancelled.')
        pend = approvals.pending_for(link)
        if pend:
            approvals.cancel(pend, actor, 'Link cancelled', run_reject_handler=False)
        link.status = PL_CANCELLED
        link.cancelled_at = timezone.now()
        link.cancel_reason = reason[:300]
        link.save(update_fields=['status', 'cancelled_at', 'cancel_reason'])
        audit.log('payment_link.cancelled', link, actor=actor, reason=reason)
    return link


def request_extension(link: PaymentLink, actor, *, new_expiry, reason: str):
    if len(reason.strip()) < 10:
        raise PaymentError('Give a reason of at least 10 characters.')
    if link.status not in (PL_ACTIVE, PL_PARTIAL, PL_EXPIRED):
        raise PaymentError('Only an active or expired link can be extended.')
    if new_expiry <= timezone.now():
        raise PaymentError('Choose a new expiry in the future.')
    if link.quotation_id and link.quotation.status in ('superseded', 'void'):
        raise PaymentError('The quotation was replaced. Create a new link from the latest version.')
    c = CompanySettings.get()
    within = new_expiry <= timezone.now() + timedelta(days=c.payment_link_max_expiry_days) and link.status != PL_EXPIRED
    if within and permissions.has_perm(actor, 'payment_links', 'approve'):
        _apply_extension(link, new_expiry, actor)
        return None
    return approvals.submit('payment_link_extension', link, title=f'Extend {link.code} to {timezone.localtime(new_expiry):%d-%b-%Y}',
                            module='payment_links', requested_by=actor, level='manager', amount=link.balance,
                            reasons=['Link expired' if link.status == PL_EXPIRED else 'Extend expiry'],
                            before={'expires': link.expires_at.isoformat()}, after={'expires': new_expiry.isoformat()},
                            payload={'expires_at': new_expiry.isoformat()}, message=reason)


def _apply_extension(link, new_expiry, actor):
    with transaction.atomic():
        link = PaymentLink.objects.select_for_update().get(pk=link.pk)
        old = link.expires_at
        link.expires_at = new_expiry
        if link.status == PL_EXPIRED:
            link.status = PL_PARTIAL if link.amount_paid > 0 else PL_ACTIVE
            link.expired_at = None
        link.save(update_fields=['expires_at', 'status', 'expired_at'])
        audit.log('payment_link.extended', link, actor=actor, changes={'expires_at': [old, new_expiry]})


def _extension_approved(req, user):
    from datetime import datetime
    link = PaymentLink.objects.get(pk=req.object_id)
    _apply_extension(link, datetime.fromisoformat(req.payload['expires_at']), user)


approvals.register('payment_link_extension', _extension_approved)


def record_open(link: PaymentLink, ip: str):
    now = timezone.now()
    first = link.first_opened_at is None
    PaymentLink.objects.filter(pk=link.pk).update(open_count=link.open_count + 1, last_opened_at=now,
                                                  first_opened_at=link.first_opened_at or now,
                                                  last_event='Opened by customer')
    if first:
        notify.notify([link.owner], f'{link.client} opened payment link {link.code}', url=link_url(link),
                      event_type='payment_link_opened', dedupe_key=f'pl-open:{link.pk}')


# ---------------------------------------------------------------- customer attempt (6.7)

def start_attempt(link: PaymentLink, *, amount=None, ip: str = '', return_url: str, notify_url: str):
    """Create the gateway order. Returns (attempt, session)."""
    adapter = gateway.get_adapter()
    ok, why = adapter.ready()
    if not ok:
        raise PaymentError(why)
    with transaction.atomic():
        link = PaymentLink.objects.select_for_update().get(pk=link.pk)
        if not link.is_payable:
            raise PaymentError('This payment request is no longer active.')
        if link.attempts.filter(status__in=['pending', 'authorised']).exists():
            raise PaymentError('We are confirming your payment. Do not pay again.')
        balance = link.balance
        amt = balance
        if amount not in (None, ''):
            try:
                amt = q2(D(amount))
            except (ValueError, ArithmeticError):
                raise PaymentError('Enter a valid amount.')
            if amt != balance:
                if not link.allow_partial:
                    raise PaymentError('Partial payment is not allowed on this link.')
                if amt > balance:
                    raise PaymentError(f'The most you can pay is {pdf._money(balance)}.')
                if amt < (link.min_partial_amount or ZERO) and amt != balance:
                    raise PaymentError(f'The minimum payment is {pdf._money(link.min_partial_amount)}.')
        if amt <= 0:
            raise PaymentError('Nothing is due on this link.')
        attempt = PaymentAttempt.objects.create(link=link, gateway_order_id=f'{link.code}-{secrets.token_hex(5)}', amount=amt,
                                                ip_hash=crypto.hash_ip(ip))
    try:
        session = adapter.create_order(attempt, link, return_url.replace('__ORDER__', attempt.gateway_order_id),
                                       notify_url)
    except gateway.GatewayError as exc:
        PaymentAttempt.objects.filter(pk=attempt.pk).update(status='failed', failure_code='GATEWAY_ERROR', failure_reason=str(exc)[:300])
        raise PaymentError(str(exc))
    PaymentAttempt.objects.filter(pk=attempt.pk).update(session_id=session.session_id[:300])
    PaymentLink.objects.filter(pk=link.pk).update(last_event='Payment started', provider=adapter.provider)
    attempt.session_id = session.session_id
    return attempt, session


def sync_attempt(attempt: PaymentAttempt, source: str = 'backstop') -> list[GatewayEvent]:
    """Ask the gateway what happened (return URL and backstop job); creates missing events, processed idempotently."""
    adapter = gateway.adapter_for(attempt.link.provider or gateway.get_adapter().provider)
    ok, _ = adapter.ready()
    if not ok:
        return []
    try:
        payments = adapter.fetch_payments(attempt.gateway_order_id)
    except gateway.GatewayError:
        return []
    events = []
    for gp in payments:
        ev = gateway.WebhookEvent(event_id=f'poll:{gp.payment_id}:{gp.status}', event_type='payment',
                                  order_id=attempt.gateway_order_id, payment=gp)
        row = store_event(adapter.provider, ev, payload={'polled': gp.raw, 'status': gp.status, 'amount': str(gp.amount)},
                          raw_body='', signature_valid=True, source=source)
        if row is not None:
            process_event(row.pk)
            events.append(row)
    return events


# ---------------------------------------------------------------- gateway events (6.8)

def store_event(provider: str, ev: gateway.WebhookEvent, *, payload: dict, raw_body: str, signature_valid: bool,
                source: str = 'webhook') -> GatewayEvent | None:
    """Inbox first: persist before processing. Returns None when the same provider event was already stored."""
    try:
        with transaction.atomic():
            return GatewayEvent.objects.create(
                provider=provider, event_id=ev.event_id[:160], event_type=ev.event_type, raw_body=raw_body[:100000],
                payload={'parsed': _event_dict(ev), 'body': payload}, signature_valid=signature_valid, source=source)
    except IntegrityError:
        GatewayEvent.objects.filter(provider=provider, event_id=ev.event_id[:160]).exclude(status='duplicate') \
            .update(note='Received again (duplicate delivery ignored)')
        return None


def _event_dict(ev: gateway.WebhookEvent) -> dict:
    d = {'event_id': ev.event_id, 'event_type': ev.event_type, 'order_id': ev.order_id, 'refund_id': ev.refund_id,
         'refund_status': ev.refund_status, 'dispute_id': ev.dispute_id,
         'dispute_amount': str(ev.dispute_amount) if ev.dispute_amount is not None else '', 'dispute_reason': ev.dispute_reason}
    if ev.payment:
        p = ev.payment
        d['payment'] = {'payment_id': p.payment_id, 'status': p.status, 'amount': str(p.amount), 'method': p.method,
                        'failure_code': p.failure_code, 'failure_reason': p.failure_reason}
    return d


def process_event(event_id: int) -> GatewayEvent:
    try:
        return _process_event(event_id)
    except Exception as exc:  # recorded on the row; reprocess job retries
        logger.exception('Gateway event %s failed', event_id)
        GatewayEvent.objects.filter(pk=event_id).update(status='failed', error=str(exc)[:2000])
        return GatewayEvent.objects.get(pk=event_id)


def _process_event(event_id: int) -> GatewayEvent:
    with transaction.atomic():
        row = GatewayEvent.objects.select_for_update().get(pk=event_id)
        if row.status in ('processed', 'duplicate', 'ignored', 'review'):
            return row
        row.attempts += 1
        parsed = (row.payload or {}).get('parsed') or {}
        kind = parsed.get('event_type')
        if not row.signature_valid:
            row.status, row.note = 'ignored', 'Signature not valid'
        elif kind == 'payment':
            _handle_payment_event(row, parsed)
        elif kind == 'refund':
            _handle_refund_event(row, parsed)
        elif kind == 'dispute':
            _handle_dispute_event(row, parsed)
        else:
            row.status, row.note = 'ignored', 'Event type not used'
        row.processed_at = timezone.now()
        row.error = ''
        row.save()
    return row


def _handle_payment_event(row: GatewayEvent, parsed: dict):
    p = parsed.get('payment') or {}
    try:
        attempt = PaymentAttempt.objects.select_related('link').get(gateway_order_id=parsed.get('order_id', ''))
    except PaymentAttempt.DoesNotExist:
        row.status, row.note = 'ignored', 'No matching order'
        return
    link = PaymentLink.objects.select_for_update().get(pk=attempt.link_id)
    attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
    row.link = link
    status = p.get('status')
    if status == 'captured':
        existing = Payment.objects.filter(provider=row.provider, provider_payment_id=p.get('payment_id', '')).first()
        if existing is not None:
            row.status, row.payment, row.note = 'duplicate', existing, 'Payment already recorded'
            return
        if attempt.status == 'captured':
            row.status, row.note = 'duplicate', 'Attempt already captured'
            return
        amount = q2(D(p.get('amount') or 0))
        attempt.status = 'captured'
        attempt.gateway_payment_id = p.get('payment_id', '')[:80]
        attempt.method = p.get('method', '')[:20]
        attempt.save(update_fields=['status', 'gateway_payment_id', 'method', 'updated_at'])
        review = []
        if amount != attempt.amount:
            review.append(f'Amount {amount} differs from the order amount {attempt.amount}')
        if link.status in (PL_CANCELLED, PL_EXPIRED) or link.expires_at < timezone.now() - timedelta(hours=1):
            review.append(f'Paid after the link was {link.get_status_display().lower()}')
        if link.amount_paid + amount > link.amount:
            review.append('Payment is more than the link balance')
        payment = Payment.objects.create(
            client=link.client, link=link, attempt=attempt, invoice=link.invoice, quotation=link.quotation, kind='gateway',
            method=attempt.method if attempt.method in dict(Payment._meta.get_field('method').choices) else 'other',
            amount=amount, payment_date=india.today(), provider=row.provider, provider_payment_id=attempt.gateway_payment_id,
            status=P_REVIEW if review else P_PENDING, review_reason='; '.join(review)[:300])
        row.payment = payment
        if review:
            row.status, row.note = 'review', payment.review_reason
            link.last_event = 'Payment needs review'
            link.save(update_fields=['last_event'])
            audit.log('payment.review', payment, changes={'reasons': review})
            transaction.on_commit(lambda: notify.notify(
                finance_users() + [link.owner], f'Payment {pdf._money(amount)} needs review', body=payment.review_reason,
                url=payment_url(payment), event_type='gateway_review', level='warning', email=True))
            return
        _verify(payment, None, link=link)
        row.status, row.note = 'processed', f'Payment {payment.receipt_number} recorded'
    elif status in ('failed', 'abandoned'):
        if attempt.status == 'captured':
            row.status, row.note = 'ignored', 'Stale failure after capture'
            return
        attempt.status = status
        attempt.failure_code = p.get('failure_code', '')[:80]
        attempt.failure_reason = p.get('failure_reason', '')[:300]
        attempt.save(update_fields=['status', 'failure_code', 'failure_reason', 'updated_at'])
        link.last_event = 'Attempt failed' if status == 'failed' else 'Customer left the payment page'
        link.save(update_fields=['last_event'])
        row.status, row.note = 'processed', f'Attempt {status}'
        if status == 'failed':
            transaction.on_commit(lambda: notify.notify([link.owner], f'Payment attempt failed on {link.code}',
                                                        body=attempt.failure_reason, url=link_url(link), event_type='payment_failed',
                                                        level='warning'))
    else:
        if attempt.status in ('initiated',):
            attempt.status = 'pending'
            attempt.save(update_fields=['status', 'updated_at'])
        row.status, row.note = 'processed', 'Pending at gateway'


def _handle_refund_event(row, parsed):
    ref = parsed.get('refund_id', '')
    refund = None
    if ref.startswith('RF') and ref[2:].isdigit():
        refund = Refund.objects.select_for_update().filter(pk=int(ref[2:])).first()
    if refund is None:
        row.status, row.note = 'ignored', 'Unknown refund'
        return
    if refund.status == 'processed':
        row.status, row.note = 'duplicate', 'Refund already processed'
        return
    st = parsed.get('refund_status')
    if st == 'processed':
        _refund_done(refund, None)
        row.status, row.note = 'processed', 'Refund processed'
    elif st == 'failed':
        refund.status = 'failed'
        refund.error = 'Gateway reported the refund failed'
        refund.save(update_fields=['status', 'error', 'updated_at'])
        row.status, row.note = 'processed', 'Refund failed'
        transaction.on_commit(lambda: notify.notify(finance_users(), f'Refund failed for {refund.payment}',
                                                    url=payment_url(refund.payment), event_type='refund_failed', level='error'))
    else:
        row.status, row.note = 'processed', 'Refund pending'


def _handle_dispute_event(row, parsed):
    attempt = PaymentAttempt.objects.filter(gateway_order_id=parsed.get('order_id', '')).first()
    payment = Payment.objects.filter(attempt=attempt).first() if attempt else None
    if payment is None:
        row.status, row.note = 'ignored', 'Dispute for unknown payment'
        return
    d, created = Dispute.objects.get_or_create(
        payment=payment, provider_dispute_id=parsed.get('dispute_id', ''),
        defaults={'amount': D(parsed.get('dispute_amount') or payment.amount), 'reason': parsed.get('dispute_reason', '')[:300],
                  'due_by': india.today() + timedelta(days=7)})
    row.payment = payment
    row.status, row.note = 'processed', 'Dispute opened' if created else 'Dispute update'
    if created:
        audit.log('payment.disputed', payment, changes={'amount': str(d.amount), 'reason': d.reason})
        transaction.on_commit(lambda: notify.notify(finance_users(), f'Chargeback raised on {payment}', body=d.reason,
                                                    url=payment_url(payment), event_type='dispute', level='error', email=True))


def reprocess(event: GatewayEvent, actor) -> GatewayEvent:
    if event.status not in ('failed', 'received'):
        raise PaymentError('Only a failed or unprocessed event can be reprocessed.')
    audit.log('gateway_event.reprocessed', event, actor=actor)
    return process_event(event.pk)


def ignore_event(event: GatewayEvent, actor, reason: str):
    if len(reason.strip()) < 10:
        raise PaymentError('Give a reason of at least 10 characters.')
    if event.status not in ('failed', 'received'):
        raise PaymentError('Only a failed or unprocessed event can be ignored.')
    GatewayEvent.objects.filter(pk=event.pk).update(status='ignored', note=f'Ignored: {reason}'[:300])
    audit.log('gateway_event.ignored', event, actor=actor, reason=reason)


# ---------------------------------------------------------------- verification and allocation

def _verify(payment: Payment, verifier, *, link=None, allocations=None):
    """Make a payment count: receipt number, allocation, advance voucher, TDS receivable, ledger. Caller holds locks."""
    payment.status = P_VERIFIED
    payment.verified_by = verifier
    payment.verified_at = timezone.now()
    payment.receipt_number = numbering.allocate('RCPT', payment.payment_date)
    payment.save(update_fields=['status', 'verified_by', 'verified_at', 'receipt_number'])
    from crm.modules.billing.services import refresh_balance
    allocated = ZERO
    rows = allocations
    if rows is None:
        rows = []
        if payment.invoice_id:
            inv = Invoice.objects.select_for_update().get(pk=payment.invoice_id)
            if inv.status in INV_OPEN:
                take = min(payment.gross_settled, inv.balance)
                tds = min(payment.tds_amount, take)
                rows.append((inv, take - tds, tds))
    used_tds = ZERO
    for inv, cash, tds in sorted(rows, key=lambda r: r[0].pk):
        cash, tds = q2(cash), q2(tds)
        if cash + tds <= 0:
            continue
        inv = Invoice.objects.select_for_update().get(pk=inv.pk)
        if inv.client_id != payment.client_id:
            raise PaymentError(f'{inv} belongs to a different client.')
        if inv.status not in INV_OPEN:
            raise PaymentError(f'{inv} is not open for payment.')
        if cash + tds > inv.balance:
            raise PaymentError(f'Allocation to {inv} is more than its balance ({inv.balance}).')
        used_tds += tds
        PaymentAllocation.objects.create(payment=payment, invoice=inv, amount=cash, tds_amount=tds, created_by=verifier)
        refresh_balance(inv)
        allocated += cash + tds
    if allocated > payment.gross_settled:
        raise PaymentError('Allocations cannot exceed the payment.')
    if used_tds > payment.tds_amount:
        raise PaymentError('TDS allocated cannot exceed the TDS recorded on the payment.')
    payment.allocated_amount = allocated
    payment.save(update_fields=['allocated_amount'])
    if payment.gross_settled - allocated > 0:
        _advance_voucher(payment, payment.gross_settled - allocated)
    if payment.tds_amount > 0:
        TdsReceivable.objects.get_or_create(payment=payment, defaults={
            'client': payment.client, 'amount': payment.tds_amount, 'section': payment.tds_section,
            'deductor_tan': payment.deductor_tan, 'fiscal_year': india.fy_label(payment.payment_date),
            'quarter': india.fy_quarter(payment.payment_date)})
    posting.enqueue('payment_received', payment, 'received', {'allocated': str(allocated)})
    if link is not None:
        link.amount_paid = min(link.amount_paid + payment.amount + payment.tds_amount, link.amount)
        link.status = PL_PAID if link.amount_paid >= link.amount else PL_PARTIAL
        if link.status == PL_PAID:
            link.paid_at = timezone.now()
        link.last_event = f'Paid {pdf._money(payment.amount)}'
        link.save(update_fields=['amount_paid', 'status', 'paid_at', 'last_event'])
    audit.log('payment.verified', payment, actor=verifier, changes={
        'receipt': payment.receipt_number, 'amount': str(payment.amount), 'tds': str(payment.tds_amount),
        'allocated': str(allocated), 'kind': payment.kind})
    _after_payment(payment)
    return payment


def _advance_voucher(payment: Payment, advance: Decimal):
    """Advance receipt voucher (Part 7.5). Tax shown per the CA-approved setting."""
    c = CompanySettings.get()
    source = payment.quotation
    taxable, cgst, sgst, igst = advance, ZERO, ZERO, ZERO
    if c.tax_on_advance and source is not None and source.grand_total:
        sp = gst.proportional_split(advance, source.grand_total, source.cgst_total, source.sgst_total, source.igst_total)
        cgst, sgst, igst, taxable = sp['cgst'], sp['sgst'], sp['igst'], sp['taxable']
    if not hasattr(payment, 'receipt_voucher'):
        ReceiptVoucher.objects.create(number=numbering.allocate('RV', payment.payment_date), payment=payment,
                                      client=payment.client, voucher_date=payment.payment_date, amount=advance,
                                      taxable_value=taxable, cgst=cgst, sgst=sgst, igst=igst)


def _after_payment(payment: Payment):
    from crm.models import LeadActivity
    lead = payment.quotation.lead if payment.quotation_id and payment.quotation.lead_id else None
    if lead is not None:
        LeadActivity.objects.create(lead=lead, kind='payment', summary=f'Payment {pdf._money(payment.amount)} received ({payment.receipt_number})',
                                    metadata={'payment': payment.pk})
    owner = payment.link.owner if payment.link_id else payment.client.owner
    url = payment_url(payment)
    amount = pdf._money(payment.amount)
    transaction.on_commit(lambda: notify.notify([owner] + finance_users(), f'Payment {amount} received from {payment.client}',
                                                url=url, event_type='payment_received', level='success'))
    from crm.modules.payroll.services import accrue_commission_for_payment
    accrue_commission_for_payment(payment)


def record_manual(actor, *, client, invoice=None, link=None, amount, payment_date, method, reference_number='',
                  deposit_account=None, tds_amount=ZERO, tds_section='', deductor_tan='', note='', proof=None) -> Payment:
    """Manual payment (6.9): Pending verification until a different Finance user verifies it."""
    c = CompanySettings.get()
    amount, tds_amount = q2(amount), q2(tds_amount or 0)
    if amount <= 0:
        raise PaymentError('Enter an amount above ₹0.')
    if payment_date > india.today():
        raise PaymentError('The payment date cannot be in the future.')
    if method in ('bank_transfer', 'upi', 'cheque') and not reference_number:
        raise PaymentError('A reference number is required for bank transfer, UPI and cheque.')
    if method != 'cash' and deposit_account is not None and deposit_account.kind == 'cash':
        raise PaymentError('Choose a bank account for non-cash payments.')
    if amount > c.manual_payment_proof_above and proof is None:
        raise PaymentError(f'Attach proof for payments above {pdf._money(c.manual_payment_proof_above)}.')
    if tds_amount and not tds_section:
        raise PaymentError('Choose the TDS section.')
    if invoice is not None:
        if invoice.client_id != client.pk:
            raise PaymentError('The invoice belongs to a different client.')
        if invoice.status not in INV_OPEN:
            raise PaymentError('The invoice is not open for payment.')
    if link is not None and link.client_id != client.pk:
        raise PaymentError('The link belongs to a different client.')
    if reference_number and deposit_account is not None and Payment.objects.filter(
            deposit_account=deposit_account, reference_number=reference_number).exclude(status=P_REJECTED).exists():
        raise PaymentError(f'A payment with reference {reference_number} is already recorded in this account.')
    posting.ensure_period_open(payment_date, actor)
    with transaction.atomic():
        p = Payment.objects.create(
            client=client, invoice=invoice, link=link, quotation=link.quotation if link and link.quotation_id else None,
            kind='manual', method=method, amount=amount, tds_amount=tds_amount, tds_section=tds_section,
            deductor_tan=deductor_tan.upper(), payment_date=payment_date, reference_number=reference_number.strip(),
            deposit_account=deposit_account, submitted_by=actor, internal_note=note[:500], status=P_PENDING)
        if proof is not None:
            from crm.modules.common.views import save_attachment
            save_attachment(p, proof, actor, purpose='payment_proof')
        audit.log('payment.recorded', p, actor=actor, changes={'amount': str(amount), 'tds': str(tds_amount), 'method': method,
                                                               'reference': reference_number})
        url = payment_url(p)
        transaction.on_commit(lambda: notify.notify(
            [u for u in finance_users() if u.pk != actor.pk], f'Verify payment {pdf._money(amount)} from {client}', url=url,
            event_type='payment_verify', level='warning'))
    return p


def verify_manual(payment: Payment, verifier, allocations=None) -> Payment:
    if not permissions.has_perm(verifier, 'payments', 'approve'):
        permissions.denied(verifier, 'payments', 'approve', detail='verify payment')
    if payment.submitted_by_id == verifier.pk:
        audit.log('payment.verify_blocked', payment, actor=verifier, result='denied', reason='maker-checker')
        raise PaymentError('A different Finance user must verify a payment you recorded.')
    with transaction.atomic():
        link = PaymentLink.objects.select_for_update().get(pk=payment.link_id) if payment.link_id else None
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != P_PENDING:
            raise PaymentError('This payment is not pending verification.')
        posting.ensure_period_open(payment.payment_date, verifier)
        if link is not None and link.status not in PL_PAYABLE + [PL_PAID]:
            link = None
        _verify(payment, verifier, link=link, allocations=allocations)
    return payment


def reject_manual(payment: Payment, user, reason: str):
    if len(reason.strip()) < 10:
        raise PaymentError('Give a reason of at least 10 characters.')
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != P_PENDING:
            raise PaymentError('This payment is not pending verification.')
        payment.status = P_REJECTED
        payment.rejected_reason = reason[:300]
        payment.save(update_fields=['status', 'rejected_reason'])
        audit.log('payment.rejected', payment, actor=user, reason=reason)
        if payment.submitted_by:
            notify.notify([payment.submitted_by], f'Payment {pdf._money(payment.amount)} rejected', body=reason,
                          url=payment_url(payment), event_type='payment_rejected', level='warning')


def accept_review(payment: Payment, user, note: str):
    """Late or mismatched gateway money that the business keeps (6.11 review queue)."""
    if not permissions.has_perm(user, 'payments', 'approve'):
        permissions.denied(user, 'payments', 'approve', detail='accept review')
    if len(note.strip()) < 10:
        raise PaymentError('Give a note of at least 10 characters.')
    with transaction.atomic():
        link = PaymentLink.objects.select_for_update().get(pk=payment.link_id) if payment.link_id else None
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != P_REVIEW:
            raise PaymentError('This payment is not in review.')
        if link is not None and payment.amount + link.amount_paid > link.amount:
            link = None   # do not overstate the link; the money is held as advance
        if payment.invoice_id and Invoice.objects.filter(pk=payment.invoice_id, status__in=INV_OPEN).exists() is False:
            payment.invoice = None
            payment.save(update_fields=['invoice'])
        _verify(payment, user, link=link if link and link.status in PL_PAYABLE + [PL_PAID, PL_EXPIRED, PL_CANCELLED] else None)
        audit.log('payment.review_accepted', payment, actor=user, reason=note)
        GatewayEvent.objects.filter(payment=payment, status='review').update(status='processed', note='Accepted after review')
    return payment


# ---------------------------------------------------------------- refunds (6.10)

def refundable_now(payment: Payment) -> Decimal:
    """Money that can go back without touching invoices: received cash not allocated and not already refunded."""
    allocated_cash = payment.allocations.filter(reversed_at__isnull=True).aggregate(s=Sum('amount'))['s'] or ZERO
    unallocated_cash = payment.amount - allocated_cash
    pending = payment.refunds.filter(status__in=['requested', 'approved', 'initiated']).aggregate(s=Sum('amount'))['s'] or ZERO
    return max(unallocated_cash - payment.refunded_amount - pending, ZERO)


def request_refund(payment: Payment, actor, *, amount, reason: str, notes: str = '', method: str = 'gateway') -> Refund:
    amount = q2(amount)
    if payment.status not in P_COUNTED + [P_REVIEW]:
        raise PaymentError('Only a received payment can be refunded.')
    if amount <= 0:
        raise PaymentError('Enter an amount above ₹0.')
    if method == 'gateway' and payment.kind != 'gateway':
        raise PaymentError('A manual payment can only be refunded manually.')
    if reason == 'other' and len(notes.strip()) < 10:
        raise PaymentError('Describe the reason in the notes.')
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        cap = refundable_now(payment)
        if amount > cap:
            allocated = payment.allocated_amount
            hint = ' Money allocated to invoices must first be credited with a credit note (treatment: refund money).' if allocated else ''
            raise PaymentError(f'The most that can be refunded now is {pdf._money(cap)}.{hint}')
        tax_portion = ZERO
        voucher = getattr(payment, 'receipt_voucher', None)
        if voucher is not None and voucher.amount:
            tax_portion = q2((voucher.cgst + voucher.sgst + voucher.igst) * amount / voucher.amount)
        r = Refund.objects.create(payment=payment, amount=amount, reason=reason, notes=notes, method=method,
                                  tax_portion=tax_portion, status='requested')
        approvals.submit('refund', r, title=f'Refund {pdf._money(amount)} on {payment}', module='refunds', requested_by=actor,
                         level='finance', amount=amount, reasons=[r.get_reason_display()],
                         before={'refundable': str(cap)}, after={'refund': str(amount), 'method': method}, message=notes)
        audit.log('refund.requested', payment, actor=actor, changes={'amount': str(amount), 'reason': reason, 'method': method})
    return r


def _refund_approved(req, user):
    with transaction.atomic():
        r = Refund.objects.select_for_update().get(pk=req.object_id)
        if r.status != 'requested':
            return
        r.status = 'approved'
        r.approved_by = user
        r.save(update_fields=['status', 'approved_by', 'updated_at'])
        audit.log('refund.approved', r.payment, actor=user, changes={'refund': r.pk})
    if r.method == 'gateway':
        transaction.on_commit(lambda: execute_refund(Refund.objects.get(pk=r.pk), user))


def _refund_rejected(req, user):
    Refund.objects.filter(pk=req.object_id, status='requested').update(status='rejected')


approvals.register('refund', _refund_approved, _refund_rejected)


def execute_refund(r: Refund, actor):
    """Run an approved gateway refund (also Retry failed refund)."""
    if r.method != 'gateway':
        raise PaymentError('This is a manual refund. Mark it refunded with the bank reference.')
    if r.status not in ('approved', 'failed'):
        raise PaymentError('Only an approved or failed refund can be executed.')
    payment = r.payment
    if not payment.attempt_id:
        raise PaymentError('The original gateway order is missing.')
    adapter = gateway.adapter_for(payment.provider)
    result = adapter.refund(payment.attempt.gateway_order_id, f'RF{r.pk}', r.amount, r.get_reason_display())
    with transaction.atomic():
        r = Refund.objects.select_for_update().get(pk=r.pk)
        if r.status == 'processed':
            return r
        r.gateway_refund_id = result.gateway_refund_id[:80]
        if result.status == 'processed':
            _refund_done(r, actor)
        elif result.status == 'initiated':
            r.status = 'initiated'
            r.error = ''
            r.save(update_fields=['status', 'gateway_refund_id', 'error', 'updated_at'])
        else:
            r.status = 'failed'
            r.error = result.error[:500] or 'The gateway did not accept the refund.'
            r.save(update_fields=['status', 'gateway_refund_id', 'error', 'updated_at'])
        audit.log('refund.executed', payment, actor=actor, changes={'refund': r.pk, 'status': r.status})
    return r


def mark_manual_refunded(r: Refund, actor, reference: str):
    if not permissions.has_perm(actor, 'payments', 'approve'):
        permissions.denied(actor, 'payments', 'approve', detail='manual refund')
    if not reference.strip():
        raise PaymentError('Enter the bank or UPI reference of the refund.')
    with transaction.atomic():
        r = Refund.objects.select_for_update().get(pk=r.pk)
        if r.status not in ('approved', 'failed', 'initiated'):
            raise PaymentError('Only an approved refund can be marked refunded.')
        r.manual_reference = reference.strip()[:80]
        r.save(update_fields=['manual_reference'])
        _refund_done(r, actor)
        audit.log('refund.manual_done', r.payment, actor=actor, changes={'refund': r.pk, 'reference': reference})
    return r


def _refund_done(r: Refund, actor):
    """Caller holds the refund lock."""
    link = PaymentLink.objects.select_for_update().get(pk=r.payment.link_id) if r.payment.link_id else None
    payment = Payment.objects.select_for_update().get(pk=r.payment_id)
    r.status = 'processed'
    r.processed_at = timezone.now()
    r.error = ''
    r.save(update_fields=['status', 'processed_at', 'error', 'gateway_refund_id', 'updated_at'])
    was_review = payment.status == P_REVIEW
    payment.refunded_amount += r.amount
    if was_review:
        # Review money was never counted; it is returned as-is. Record the receipt so the ledger shows in and out.
        payment.receipt_number = payment.receipt_number or numbering.allocate('RCPT', payment.payment_date)
        posting.enqueue('payment_received', payment, 'received', {'allocated': '0'})
        GatewayEvent.objects.filter(payment=payment, status='review').update(status='processed', note='Refunded after review')
    payment.status = P_REFUNDED if payment.refunded_amount >= payment.amount else P_PART_REFUNDED
    payment.save(update_fields=['refunded_amount', 'status', 'receipt_number'])
    posting.enqueue('refund_processed', r, 'processed', {'debit': posting.CUSTOMER_ADVANCES})
    from crm.modules.payroll.commissions import clawback_refund
    clawback_refund(r)
    if link is not None:
        link.amount_refunded += r.amount
        if link.status in (PL_PAID, PL_PARTIAL, PL_PART_REFUNDED, PL_REFUNDED):
            link.status = PL_REFUNDED if link.amount_refunded >= link.amount_paid else PL_PART_REFUNDED
        link.save(update_fields=['amount_refunded', 'status'])
    url = payment_url(payment)
    transaction.on_commit(lambda: notify.notify(finance_users(), f'Refund {pdf._money(r.amount)} processed', url=url,
                                                event_type='refund_processed', level='success'))


# ---------------------------------------------------------------- disputes and settlements

def update_dispute(d: Dispute, actor, *, status: str, note: str = ''):
    if status not in dict(Dispute.STATUSES):
        raise PaymentError('Unknown status.')
    Dispute.objects.filter(pk=d.pk).update(status=status, outcome_note=note[:300])
    audit.log('dispute.updated', d.payment, actor=actor, changes={'status': status}, reason=note)


def record_settlement(actor, *, provider, settlement_id, settled_on, gross, fees, tax_on_fees, tds, bank_account, payment_ids=()):
    gross, fees, tax_on_fees, tds = q2(gross), q2(fees), q2(tax_on_fees), q2(tds)
    net = gross - fees - tax_on_fees - tds
    if net < 0:
        raise PaymentError('Fees and deductions cannot exceed the gross amount.')
    with transaction.atomic():
        if Settlement.objects.filter(provider=provider, settlement_id=settlement_id).exists():
            raise PaymentError('This settlement is already recorded.')
        s = Settlement.objects.create(provider=provider, settlement_id=settlement_id, settled_on=settled_on, gross=gross, fees=fees,
                                      tax_on_fees=tax_on_fees, tds=tds, net=net, bank_account=bank_account, status='posted')
        if payment_ids:
            pays = Payment.objects.filter(pk__in=payment_ids, kind='gateway', settlement__isnull=True)
            total = pays.aggregate(s=Sum('amount'))['s'] or ZERO
            if total != gross:
                raise PaymentError(f'The selected payments add up to {pdf._money(total)}, not the gross {pdf._money(gross)}.')
            pays.update(settlement=s)
        posting.enqueue('settlement_recorded', s, 'recorded')
        audit.log('settlement.recorded', s, actor=actor, changes={'gross': str(gross), 'net': str(net)})
    return s


# ---------------------------------------------------------------- reporting helpers

def customer_advances(client=None):
    qs = Payment.objects.filter(status__in=P_COUNTED).select_related('client', 'quotation')
    if client is not None:
        qs = qs.filter(client=client)
    rows = []
    for p in qs:
        held = p.gross_settled - p.allocated_amount - p.refunded_amount
        if held > 0:
            rows.append({'payment': p, 'held': held, 'age': (india.today() - p.payment_date).days})
    return sorted(rows, key=lambda r: -r['age'])


def held_advance(payment: Payment) -> Decimal:
    return max(payment.gross_settled - payment.allocated_amount - payment.refunded_amount, ZERO)
