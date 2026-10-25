"""Commission accrual and clawback (Part 12.5).

Each business event accrues at most once per rule: CommissionEntry.event_key is unique.
The basis is always the pre-tax value. Accrued entries are approved by a manager and
paid through payroll.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q

from crm.core import india
from crm.core.money import ZERO, q2

logger = logging.getLogger('crm.commissions')


def _rules(basis: str, user, on):
    from crm.models import CommissionRule
    qs = CommissionRule.objects.filter(is_active=True, basis=basis, effective_from__lte=on).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gte=on)).prefetch_related('roles')
    profile = getattr(user, 'crm_profile', None)
    role_ids = set(profile.roles.values_list('pk', flat=True)) if profile else set()
    out = []
    for r in qs:
        rids = {x.pk for x in r.roles.all()}
        if rids and not rids & role_ids:
            continue
        out.append(r)
    return out


def _service_share(doc, service_id) -> Decimal:
    """Share of the pre-tax value of a quotation or invoice that belongs to one service (0..1)."""
    lines = list(doc.lines.all())
    total = sum((l.taxable_value for l in lines), ZERO)
    if not total:
        return ZERO
    part = sum((l.taxable_value for l in lines if l.service_id == service_id), ZERO)
    return part / total


def _create(user, rule, key, basis_amount, **refs):
    from crm.models import CommissionEntry
    amount = q2(basis_amount * rule.rate_percent / Decimal('100'))
    if amount == 0:
        return None
    try:
        with transaction.atomic():
            return CommissionEntry.objects.create(user=user, rule=rule, event_key=key[:120], basis_amount=q2(basis_amount),
                                                  rate=rule.rate_percent, amount=amount, **refs)
    except IntegrityError:
        return None


def accrue(basis: str, *, lead=None, invoice=None, payment=None) -> list:
    made = []
    on = india.today()
    if basis == 'won' and lead is not None:
        user, doc, base, ref = lead.owner, None, lead.won_value or lead.value or ZERO, {'lead': lead}
        tag = f'won:lead{lead.pk}'
    elif basis == 'invoiced' and invoice is not None:
        user, doc, base, ref = invoice.owner, invoice, invoice.taxable_total, {'invoice': invoice}
        tag = f'inv:{invoice.pk}'
    elif basis == 'collected' and payment is not None:
        doc = payment.invoice or payment.quotation
        user = (payment.link.owner if payment.link_id else None) or (doc.owner if doc is not None else payment.client.owner)
        ratio = (doc.taxable_total / doc.grand_total) if doc is not None and doc.grand_total else Decimal('1')
        base, ref = q2(payment.gross_settled * ratio), {'payment': payment}
        tag = f'pay:{payment.pk}'
    else:
        return made
    if user is None or base <= 0:
        return made
    for rule in _rules(basis, user, on):
        amount = base
        if rule.service_id:
            if basis == 'won':
                if not lead.services.filter(pk=rule.service_id).exists():
                    continue
            elif doc is not None:
                amount = q2(base * _service_share(doc, rule.service_id))
            else:
                continue
        e = _create(user, rule, f'{tag}:r{rule.pk}', amount, **ref)
        if e:
            made.append(e)
    return made


def clawback(credit_note) -> list:
    """Reverse commission proportionally when an invoice is credited (rules with clawback on)."""
    from crm.models import CommissionEntry
    inv = credit_note.invoice
    made = []
    if not inv.taxable_total:
        return made
    ratio = credit_note.taxable_value / inv.taxable_total
    pay_ids = list(inv.allocations.values_list('payment_id', flat=True))
    for e in CommissionEntry.objects.filter(Q(invoice=inv) | Q(payment_id__in=pay_ids), amount__gt=0,
                                            rule__clawback_on_refund=True).select_related('rule'):
        amt = -q2(e.amount * ratio)
        if not amt:
            continue
        try:
            with transaction.atomic():
                made.append(CommissionEntry.objects.create(
                    user=e.user, rule=e.rule, invoice=inv, event_key=f'clawback:cn{credit_note.pk}:e{e.pk}',
                    basis_amount=-q2(e.basis_amount * ratio), rate=e.rate, amount=amt, status='accrued'))
        except IntegrityError:
            continue
    return made


def clawback_refund(refund) -> list:
    from crm.models import CommissionEntry
    p = refund.payment
    made = []
    if not p.gross_settled:
        return made
    ratio = refund.amount / p.gross_settled
    for e in CommissionEntry.objects.filter(payment=p, amount__gt=0, rule__clawback_on_refund=True).select_related('rule'):
        try:
            with transaction.atomic():
                made.append(CommissionEntry.objects.create(
                    user=e.user, rule=e.rule, payment=p, event_key=f'clawback:rf{refund.pk}:e{e.pk}',
                    basis_amount=-q2(e.basis_amount * ratio), rate=e.rate, amount=-q2(e.amount * ratio)))
        except IntegrityError:
            continue
    return made


def approve_entries(entries, approver) -> int:
    """Accrued -> Approved; never your own. Approved amounts flow into the next payroll run."""
    from crm.core import audit
    from crm.models import CommissionEntry
    n = 0
    for e in entries:
        if e.user_id == approver.pk:
            raise ValidationError('You cannot approve your own commission.')
        if e.status != 'accrued':
            continue
        CommissionEntry.objects.filter(pk=e.pk, status='accrued').update(status='approved', approved_by=approver)
        n += 1
    audit.log('commission.approved', actor=approver, object_type='commissionentry', object_id='', changes={'entries': n})
    return n
