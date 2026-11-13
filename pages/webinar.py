"""Webinar registrations: the rules that decide who has a seat.

Both the browser return page, the Cashfree webhook and the reconcile job can learn that a payment succeeded, in any
order and more than once. `mark_paid` is the single place that turns a pending registration into a paid one, and it
does so exactly once, so the thank-you email and the CRM lead are never duplicated."""
from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.utils import timezone

from . import cashfree
from .models import Webinar, WebinarRegistration

logger = logging.getLogger(__name__)


def active_webinar(slug: str | None = None) -> Webinar | None:
    """The webinar a visitor should see: the one asked for, else the next upcoming one, else the latest."""
    qs = Webinar.objects.filter(is_active=True)
    if slug:
        return qs.filter(slug=slug).first()
    return qs.filter(starts_at__gt=timezone.now()).order_by('starts_at').first() or qs.order_by('-starts_at').first()


def new_order_id() -> str:
    return f'WEB{uuid.uuid4().hex[:16].upper()}'


def _to_crm(fn_name: str, *args, **kwargs) -> None:
    """Hand a registration to the CRM (lead + alerts). Never breaks the public flow."""
    try:
        from crm.modules.leads import website
        getattr(website, fn_name)(*args, **kwargs)
    except Exception:
        logger.exception('CRM hand-off %s failed', fn_name)


def mark_paid(reg_pk: int, payment_id: str = '') -> bool:
    """Confirm a seat. Returns True only for the call that actually made the change."""
    with transaction.atomic():
        reg = WebinarRegistration.objects.select_for_update().get(pk=reg_pk)
        if reg.status == WebinarRegistration.Status.PAID:
            return False
        reg.status = WebinarRegistration.Status.PAID
        reg.paid_at = timezone.now()
        if payment_id:
            reg.cashfree_payment_id = str(payment_id)[:80]
        reg.save(update_fields=['status', 'paid_at', 'cashfree_payment_id'])
        transaction.on_commit(lambda: after_paid(reg.pk))
    return True


def mark_failed(reg_pk: int) -> None:
    WebinarRegistration.objects.filter(pk=reg_pk, status=WebinarRegistration.Status.PENDING).update(
        status=WebinarRegistration.Status.FAILED)


def after_paid(reg_pk: int) -> None:
    """Everything that follows a confirmed payment: CRM lead, the attendee's confirmation, a note to the team."""
    from . import webinar_mail

    reg = WebinarRegistration.objects.select_related('webinar').get(pk=reg_pk)
    _to_crm('webinar_registration', reg, paid=True)
    webinar_mail.send_confirmation(reg)
    webinar_mail.notify_team(reg)
    from . import meta
    meta.queue('Purchase', reg, when=reg.paid_at)


def reconcile(reg: WebinarRegistration) -> str:
    """Ask Cashfree what happened to this order and apply it. Returns 'paid', 'failed' or 'processing'."""
    if reg.status != WebinarRegistration.Status.PENDING:
        return reg.status
    try:
        order = cashfree.fetch_order(reg.cashfree_order_id)
    except cashfree.CashfreeError:
        return 'processing'
    state = order.get('order_status')
    if state == 'PAID':
        mark_paid(reg.pk)
        return 'paid'
    if state in ('EXPIRED', 'TERMINATED'):
        mark_failed(reg.pk)
        return 'failed'
    return 'processing'
