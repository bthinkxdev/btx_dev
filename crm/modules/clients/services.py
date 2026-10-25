"""Client service layer (Part 9.1-9.2)."""
from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from crm.core import audit, india
from crm.core.money import ZERO
from crm.models import Client, Contact


class ClientError(ValidationError):
    pass


def normalized(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def possible_duplicates(*, legal_name='', gstin='', pan='', phone='', email='', exclude_id=None):
    q = Q()
    if gstin:
        q |= Q(gstin=gstin.upper())
    if pan:
        q |= Q(pan=pan.upper())
    if phone:
        q |= Q(phone=phone) | Q(contacts__phone=phone)
    if email:
        q |= Q(email__iexact=email) | Q(contacts__email__iexact=email)
    n = normalized(legal_name)
    if len(n) >= 4:
        q |= Q(normalized_name=n)
    if not q:
        return Client.objects.none()
    qs = Client.objects.filter(q, archived_at__isnull=True).distinct()
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    return qs


def client_from_lead(lead, actor) -> Client:
    """Create a client from a won/converted lead without losing the lead's history (Part 9)."""
    with transaction.atomic():
        name = lead.company or lead.name
        client = Client.objects.create(
            legal_name=name, display_name=name, client_type='company' if lead.company else 'individual',
            phone=lead.phone, email=lead.email, website=lead.website, billing_city=lead.city,
            billing_state=lead.state, owner=lead.owner or actor, source_lead=lead,
            customer_type='unregistered', created_by=actor, updated_by=actor,
        )
        Contact.objects.create(client=client, name=lead.name, phone=lead.phone, email=lead.email, is_primary=True,
                               is_billing=True, is_decision_maker=(lead.decision_maker or '').lower() == lead.name.lower(),
                               created_by=actor)
        audit.log('client.created', client, actor=actor, changes={'from_lead': lead.pk})
    return client


def ensure_client_for_lead(lead, actor) -> Client:
    from crm.models import Lead
    if lead.client_id:
        return lead.client
    with transaction.atomic():
        lead = Lead.objects.select_for_update().get(pk=lead.pk)
        if lead.client_id:
            return lead.client
        matches = list(possible_duplicates(phone=lead.phone, email=lead.email, legal_name=lead.company))
        client = matches[0] if len(matches) == 1 else client_from_lead(lead, actor)
        lead.client = client
        lead.save(update_fields=['client'])
        lead.quotations.filter(client__isnull=True).update(client=client)
    return client


def outstanding(client) -> object:
    from crm.models import Invoice
    return Invoice.objects.filter(client=client, status__in=['issued', 'partially_paid']).aggregate(s=Sum('balance'))['s'] or ZERO


def archive_blockers(client) -> list[str]:
    from crm.models import Contract, Invoice, Project
    out = []
    if Invoice.objects.filter(client=client, status__in=['draft', 'pending_approval', 'issued', 'partially_paid']).exists():
        out.append('open invoices')
    if Contract.objects.filter(client=client, status__in=['draft', 'pending_approval', 'active', 'suspended']).exists():
        out.append('active contracts')
    if Project.objects.filter(client=client, status__in=['planned', 'active', 'on_hold']).exists():
        out.append('open projects')
    return out


def archive(client, actor, reason: str):
    if len(reason.strip()) < 10:
        raise ClientError('Give a reason of at least 10 characters.')
    blockers = archive_blockers(client)
    if blockers:
        raise ClientError('Cannot archive: this client has ' + ', '.join(blockers) + '.')
    with transaction.atomic():
        c = Client.objects.select_for_update().get(pk=client.pk)
        c.archived_at = timezone.now()
        c.archived_by = actor
        c.archive_reason = reason
        c.status = 'archived'
        c.save(update_fields=['archived_at', 'archived_by', 'archive_reason', 'status'])
        audit.log('client.archived', c, actor=actor, reason=reason)
    return c


def set_status(client, actor, status: str, reason: str):
    if status not in ('active', 'on_hold', 'restricted'):
        raise ClientError('Choose a valid status.')
    if status != 'active' and len(reason.strip()) < 10:
        raise ClientError('Give a reason of at least 10 characters.')
    with transaction.atomic():
        c = Client.objects.select_for_update().get(pk=client.pk)
        old = c.status
        c.status = status
        c.status_reason = reason if status != 'active' else ''
        c.updated_by = actor
        c.row_version += 1
        c.save(update_fields=['status', 'status_reason', 'updated_by', 'row_version', 'updated_at'])
        audit.log('client.status_changed', c, actor=actor, changes={'status': [old, status]}, reason=reason)
    return c


def merge(primary: Client, secondary: Client, actor):
    """Move all child records to primary; secondary archived with a redirect pointer (Part 9.1)."""
    from crm.models import (Contract, Invoice, Lead, Payment, PaymentLink, Project, Quotation, ReceiptVoucher,
                            TdsReceivable, Ticket)
    if primary.pk == secondary.pk:
        raise ClientError('Choose two different clients.')
    with transaction.atomic():
        ids = sorted([primary.pk, secondary.pk])
        locked = {c.pk: c for c in Client.objects.select_for_update().filter(pk__in=ids)}
        p, s = locked[primary.pk], locked[secondary.pk]
        if p.gstin and s.gstin and p.gstin != s.gstin:
            raise ClientError('These clients have different GSTINs and are different legal entities.')
        for model in (Lead, Quotation, Invoice, Payment, PaymentLink, Contract, Project, ReceiptVoucher,
                      TdsReceivable, Ticket):
            model.objects.filter(client=s).update(client=p)
        Contact.objects.filter(client=s).update(client=p, is_primary=False)
        from crm.models import JournalLine
        JournalLine.objects.filter(client=s).update(client=p)
        s.merged_into = p
        s.archived_at = timezone.now()
        s.archived_by = actor
        s.archive_reason = f'Merged into client #{p.pk}'
        s.status = 'archived'
        gstin = s.gstin
        s.gstin = ''
        s.save(update_fields=['merged_into', 'archived_at', 'archived_by', 'archive_reason', 'status', 'gstin'])
        if gstin and not p.gstin:
            p.gstin = gstin
            p.save(update_fields=['gstin'])
        audit.log('client.merged', p, actor=actor, changes={'secondary': s.pk})
    return p


def financial_summary(client) -> dict:
    from crm.models import Invoice, Payment, PaymentAllocation, TdsReceivable
    inv = Invoice.objects.filter(client=client, status__in=['issued', 'partially_paid', 'paid', 'credit_noted', 'written_off'])
    agg = inv.aggregate(billed=Sum('grand_total'), outstanding=Sum('balance'), credited=Sum('credit_noted'))
    pay = Payment.objects.filter(client=client, status__in=['verified', 'partially_refunded', 'refunded'])
    pagg = pay.aggregate(collected=Sum('amount'), allocated=Sum('allocated_amount'), tds=Sum('tds_amount'),
                         refunded=Sum('refunded_amount'))
    advances = (pagg['collected'] or ZERO) + (pagg['tds'] or ZERO) - (pagg['allocated'] or ZERO) - (pagg['refunded'] or ZERO)
    buckets = {'Current': ZERO, '1-30': ZERO, '31-60': ZERO, '61-90': ZERO, '90+': ZERO}
    for i in inv.filter(status__in=['issued', 'partially_paid']):
        if i.age_bucket:
            buckets[i.age_bucket] += i.balance
    last = pay.order_by('-payment_date').first()
    return {
        'billed': agg['billed'] or ZERO, 'outstanding': agg['outstanding'] or ZERO, 'credited': agg['credited'] or ZERO,
        'collected': pagg['collected'] or ZERO, 'tds': TdsReceivable.objects.filter(client=client).exclude(
            status='written_off').aggregate(s=Sum('amount'))['s'] or ZERO,
        'advances': max(advances, ZERO), 'ageing': buckets, 'last_payment': last,
    }
