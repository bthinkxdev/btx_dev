"""Who gets the credit for revenue. One rule, used by every page, report and commission, so the same rupee is never
credited to two different people.

* **A win** is credited to ``credited_to`` (the owner when the owner closed it, or the person the closer chose). Wins from
  before credit existed fall back to the lead owner.
* **A collection** is credited like the win of the lead the client came from. A client created by hand, with no source
  lead, is credited to the client's owner.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Q

from .money import ZERO

COUNTED = ['verified', 'partially_refunded']      # money that really arrived and was not returned in full


def lead_credit_id(lead) -> int | None:
    return lead.credited_to_id or lead.owner_id


def won_q(uids, prefix: str = '') -> Q:
    """Leads whose win counts for one of these people."""
    uids = list(uids)
    return Q(**{f'{prefix}credited_to_id__in': uids}) | Q(**{f'{prefix}credited_to__isnull': True, f'{prefix}owner_id__in': uids})


def payment_credit_id(payment) -> int | None:
    client = payment.client
    src = client.source_lead if client.source_lead_id else None
    if src is not None:
        return lead_credit_id(src) or client.owner_id
    return client.owner_id


def collected_by_user(start: date, end: date, statuses=None) -> dict:
    """{user id: rupees collected} for payments dated in the range."""
    from crm.models import Payment
    out: dict = defaultdict(lambda: ZERO)
    qs = Payment.objects.filter(status__in=statuses or COUNTED, payment_date__gte=start, payment_date__lte=end).select_related('client__source_lead')
    for p in qs:
        out[payment_credit_id(p)] += p.amount
    return out


def collected_total(uids, start: date, end: date, statuses=None) -> Decimal:
    by_user = collected_by_user(start, end, statuses)
    return sum((by_user.get(u, ZERO) for u in uids), ZERO)
