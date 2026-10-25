"""
Contracts, billing schedules, amendments, renewals and recurring invoices (Part 9.4-9.7, 7.10).

* An amendment is a new contract version with an effective date; the prior version is kept
  as Superseded and its schedule rows from the effective date are skipped.
* Schedule rows are unique per (line, period start, period end), so generation is idempotent.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, audit, india, notify, numbering, permissions
from crm.core.money import ZERO, q2
from crm.models import BillingSchedule, Contract, ContractLine, Invoice, InvoiceLine
from crm.models.catalog import RECURRING_MODELS


class ContractError(ValidationError):
    pass


LIVE = ('active', 'suspended')
HORIZON_MONTHS = 3          # open-ended contracts: schedule this far ahead


def contract_url(c) -> str:
    return reverse('crm:contract_detail', args=[c.pk])


# ---------------------------------------------------------------- creation

def prefill(*, client, owner, quotation=None) -> Contract:
    today = india.today()
    c = Contract(client=client, owner=owner, quotation=quotation, start_date=today, title='',
                 payment_terms_days=client.payment_terms_days or 15)
    if quotation is not None:
        c.title = quotation.title or f'Services for {client.display_name}'
        c.scope = quotation.deliverables
        c.payment_terms_days = quotation.payment_terms_days
    return c


def lines_from_quotation(quotation) -> list[dict]:
    out = []
    for ql in quotation.lines.select_related('service'):
        model = ql.service.billing_model if ql.service_id else 'one_time'
        out.append({'service': ql.service_id, 'description': ql.description, 'quantity': ql.quantity,
                    'unit_price': ql.taxable_value / ql.quantity if ql.quantity else ql.unit_price,
                    'billing_model': model if model in dict(ContractLine._meta.get_field('billing_model').choices) else 'one_time',
                    'taxability': ql.taxability, 'tax_rate': f'{ql.tax_rate.normalize():f}', 'sac_code': ql.sac_code})
    return out


def activation_reasons(c: Contract, actor) -> list[str]:
    reasons = []
    for ln in c.lines.select_related('service'):
        if ln.service_id and ln.service.floor_price and ln.unit_price < ln.service.floor_price:
            reasons.append(f'"{ln.description}" is priced below the floor price')
    if not permissions.has_perm(actor, 'contracts', 'approve'):
        reasons.append('Contracts are activated by a manager')
    return reasons


def validate_ready(c: Contract) -> list[str]:
    errs = []
    lines = list(c.lines.all())
    if not lines:
        errs.append('Add at least one contract line')
    for ln in lines:
        if ln.start_date < c.start_date:
            errs.append(f'"{ln.description}" starts before the contract')
        if c.end_date and ln.end_date and ln.end_date > c.end_date:
            errs.append(f'"{ln.description}" ends after the contract')
        if ln.taxability == 'taxable' and not ln.sac_code and not (ln.service_id and ln.service.sac_code):
            errs.append(f'"{ln.description}" needs a SAC code')
    if c.client.is_restricted:
        errs.append('The client is restricted')
    return errs


def submit_or_activate(c: Contract, actor, message: str = ''):
    errs = validate_ready(c)
    if errs:
        raise ContractError('Before activating: ' + '; '.join(errs) + '.')
    reasons = activation_reasons(c, actor)
    if reasons:
        with transaction.atomic():
            Contract.objects.filter(pk=c.pk).update(status='pending_approval')
            approvals.submit('contract', c, title=f'Activate contract {c.title}', module='contracts', requested_by=actor,
                             level='manager', amount=c.value, reasons=reasons, message=message)
        return None
    return activate(c, actor)


def activate(c: Contract, actor) -> Contract:
    with transaction.atomic():
        c = Contract.objects.select_for_update().get(pk=c.pk)
        if c.status not in ('draft', 'pending_approval'):
            raise ContractError('Only a draft contract can be activated.')
        if not c.number:
            c.number = c.previous.number if c.previous_id else numbering.allocate('CT', c.start_date)
        c.status = 'active'
        c.activated_at = timezone.now()
        c.save(update_fields=['number', 'status', 'activated_at'])
        if c.previous_id:
            _supersede(c.previous, c)
        generate_schedule(c)
        audit.log('contract.activated', c, actor=actor, changes={'number': c.number, 'version': c.version, 'value': str(c.value)})
    if c.quotation_id:
        from crm.modules.quotations.services import mark_converted
        mark_converted(c.quotation, actor, 'contract')
    return c


def _supersede(old: Contract, new: Contract):
    eff = new.effective_from or new.start_date
    old = Contract.objects.select_for_update().get(pk=old.pk)
    if old.status in LIVE:
        old.status = 'superseded'
        old.save(update_fields=['status'])
        BillingSchedule.objects.filter(contract_line__contract=old, status='scheduled', period_start__gte=eff).update(status='skipped')


approvals.register('contract', lambda req, user: activate(Contract.objects.get(pk=req.object_id), user),
                   lambda req, user: Contract.objects.filter(pk=req.object_id, status='pending_approval').update(status='draft'))


# ---------------------------------------------------------------- schedule

def _period_end(start: date, months: int, cap: date | None) -> date:
    end = india.add_months(start, months) - timedelta(days=1)
    return min(end, cap) if cap else end


def generate_schedule(c: Contract, horizon: date | None = None) -> int:
    """Create schedule rows (idempotent). Recurring lines run to their end date, or HORIZON_MONTHS ahead if open-ended."""
    horizon = horizon or india.add_months(india.today(), HORIZON_MONTHS)
    eff = c.effective_from or c.start_date
    made = 0
    for ln in c.lines.all():
        months = RECURRING_MODELS.get(ln.billing_model)
        start = max(ln.start_date, eff)
        stop = ln.end_date or c.end_date
        if not months:
            if ln.billing_model in ('one_time', 'milestone', 'usage'):
                made += _row(ln, start, stop or start, start, ln.line_total)
            continue
        p = start
        while p <= (stop or horizon) and p <= (stop or date.max):
            end = _period_end(p, months, stop)
            full = india.add_months(p, months) - timedelta(days=1)
            amount = ln.line_total
            if end < full:
                days_full = (full - p).days + 1
                amount = q2(ln.line_total * Decimal((end - p).days + 1) / Decimal(days_full))
            made += _row(ln, p, end, p, amount)
            p = end + timedelta(days=1)
    return made


def _row(ln, start, end, inv_date, amount) -> int:
    if amount <= 0:
        return 0
    try:
        with transaction.atomic():
            BillingSchedule.objects.create(contract_line=ln, period_start=start, period_end=end, invoice_date=inv_date, amount=amount)
        return 1
    except IntegrityError:
        return 0


def edit_row(row: BillingSchedule, actor, *, invoice_date: date, amount: Decimal):
    if row.status != 'scheduled' or row.invoice_date <= india.today():
        raise ContractError('Only future scheduled rows can be edited.')
    if amount <= 0:
        raise ContractError('Enter an amount above ₹0.')
    if invoice_date < india.today():
        raise ContractError('The invoice date cannot be in the past.')
    old = (row.invoice_date, row.amount)
    BillingSchedule.objects.filter(pk=row.pk).update(invoice_date=invoice_date, amount=q2(amount))
    audit.log('contract.schedule_edited', row.contract_line.contract, actor=actor,
              changes={'row': row.pk, 'date': [old[0], invoice_date], 'amount': [str(old[1]), str(amount)]})


# ---------------------------------------------------------------- recurring invoices (7.10)

def invoice_for_row(row: BillingSchedule, actor=None, *, issue: bool | None = None) -> Invoice:
    from crm.modules.billing import services as bs
    with transaction.atomic():
        row = BillingSchedule.objects.select_for_update(of=('self',)).select_related('contract_line__contract__client', 'contract_line__service').get(pk=row.pk)
        if row.status != 'scheduled' and row.status != 'failed':
            raise ContractError('This row was already invoiced or skipped.')
        ln = row.contract_line
        c = ln.contract
        if c.status != 'active' or ln.is_paused:
            row.status = 'skipped'
            row.error = 'Contract or line paused'
            row.save(update_fields=['status', 'error'])
            raise ContractError('The contract or line is paused.')
        owner = actor or c.owner
        inv = bs.prefill(client=c.client, owner=c.owner)
        inv.contract = c
        inv.billing_schedule = row
        inv.invoice_date = max(row.invoice_date, india.today())
        inv.due_date = inv.invoice_date + timedelta(days=c.payment_terms_days)
        inv.milestone_ref = f'{ln.description}: {row.period_start:%d-%b-%Y} to {row.period_end:%d-%b-%Y}'[:120]
        inv.created_by = owner
        inv.save()
        sac = ln.sac_code or (ln.service.sac_code if ln.service_id else '')
        InvoiceLine.objects.create(invoice=inv, service=ln.service, description=inv.milestone_ref, sac_code=sac, quantity=1,
                                   unit_price=row.amount, taxability=ln.taxability, tax_rate=ln.tax_rate)
        bs.recalculate(inv)
        row.status = 'generated'
        row.error = ''
        row.save(update_fields=['status', 'error'])
        audit.log('contract.invoice_generated', c, actor=actor, changes={'row': row.pk, 'invoice': inv.pk})
        should_issue = (c.invoice_policy == 'auto_issue') if issue is None else issue
    if should_issue:
        inv = bs.issue(inv, owner, approved=True)
        to = (c.client.billing_contact.email if c.client.billing_contact and c.client.billing_contact.email else c.client.email)
        if to:
            bs.send(inv, owner, to=to)
            BillingSchedule.objects.filter(pk=row.pk).update(status='sent')
    return inv


def generate_recurring_invoices(upto: date | None = None) -> int:
    upto = upto or india.today()
    n = 0
    for c in Contract.objects.filter(status='active', end_date__isnull=True):
        generate_schedule(c)
    for row in BillingSchedule.objects.filter(status='scheduled', invoice_date__lte=upto,
                                              contract_line__contract__status='active').select_related('contract_line__contract'):
        if row.contract_line.is_paused:
            BillingSchedule.objects.filter(pk=row.pk).update(status='skipped', error='Line paused')
            continue
        try:
            invoice_for_row(row)
            n += 1
        except Exception as exc:  # recorded on the row; Retry button re-runs
            BillingSchedule.objects.filter(pk=row.pk, status='scheduled').update(status='failed', error=str(exc)[:500])
            c = row.contract_line.contract
            notify.notify([c.owner], f'Recurring invoice failed for {c}', body=str(exc)[:300], url=contract_url(c),
                          event_type='recurring_failed', level='error', dedupe_key=f'recur-fail:{row.pk}')
    return n


def retry_row(row: BillingSchedule, actor):
    if row.status != 'failed':
        raise ContractError('Only a failed row can be retried.')
    BillingSchedule.objects.filter(pk=row.pk).update(status='scheduled', error='')
    row.refresh_from_db()
    return invoice_for_row(row, actor)


# ---------------------------------------------------------------- lifecycle changes

def amend(c: Contract, actor, *, effective_from: date, reason: str) -> Contract:
    if c.status != 'active':
        raise ContractError('Only an active contract can be amended.')
    if len(reason.strip()) < 10:
        raise ContractError('Give a reason of at least 10 characters.')
    if effective_from < c.start_date:
        raise ContractError('The effective date cannot be before the contract start.')
    if hasattr(c, 'amendment'):
        raise ContractError('An amendment already exists for this version.')
    with transaction.atomic():
        new = Contract.objects.create(
            number=c.number, version=c.version + 1, previous=c, client=c.client, quotation=c.quotation, title=c.title, owner=c.owner,
            status='draft', start_date=c.start_date, end_date=c.end_date, effective_from=effective_from, auto_renew=c.auto_renew,
            notice_days=c.notice_days, invoice_policy=c.invoice_policy, payment_terms_days=c.payment_terms_days, scope=c.scope,
            notes=c.notes, amendment_reason=reason, created_by=actor)
        for ln in c.lines.all():
            ContractLine.objects.create(contract=new, service=ln.service, description=ln.description, quantity=ln.quantity,
                                        unit_price=ln.unit_price, billing_model=ln.billing_model,
                                        start_date=max(ln.start_date, effective_from) if (not ln.end_date or ln.end_date >= effective_from) else ln.start_date,
                                        end_date=ln.end_date, taxability=ln.taxability, tax_rate=ln.tax_rate, sac_code=ln.sac_code)
        audit.log('contract.amendment_created', c, actor=actor, reason=reason, changes={'new_version': new.version, 'effective': effective_from})
    return new


def suspend(c: Contract, actor, reason: str):
    if c.status != 'active':
        raise ContractError('Only an active contract can be suspended.')
    if len(reason.strip()) < 10:
        raise ContractError('Give a reason of at least 10 characters.')
    Contract.objects.filter(pk=c.pk).update(status='suspended', suspended_reason=reason[:300])
    audit.log('contract.suspended', c, actor=actor, reason=reason)


def resume(c: Contract, actor):
    if c.status != 'suspended':
        raise ContractError('Only a suspended contract can be resumed.')
    Contract.objects.filter(pk=c.pk).update(status='active', suspended_reason='')
    BillingSchedule.objects.filter(contract_line__contract=c, status='skipped', invoice_date__gte=india.today()).update(status='scheduled')
    audit.log('contract.resumed', c, actor=actor)


def terminate(c: Contract, actor, *, on: date, reason: str):
    if c.status not in LIVE:
        raise ContractError('Only an active or suspended contract can be terminated.')
    if len(reason.strip()) < 10:
        raise ContractError('Give a reason of at least 10 characters.')
    with transaction.atomic():
        Contract.objects.filter(pk=c.pk).update(status='terminated', terminated_on=on, terminated_reason=reason[:300])
        BillingSchedule.objects.filter(contract_line__contract=c, status__in=['scheduled', 'failed'], period_start__gt=on).update(status='skipped')
        audit.log('contract.terminated', c, actor=actor, reason=reason, changes={'on': on})


def complete(c: Contract, actor):
    if c.status != 'active':
        raise ContractError('Only an active contract can be completed.')
    if BillingSchedule.objects.filter(contract_line__contract=c, status__in=['scheduled', 'failed']).exists():
        raise ContractError('Some billing rows are not invoiced yet.')
    Contract.objects.filter(pk=c.pk).update(status='completed')
    audit.log('contract.completed', c, actor=actor)


def renew(c: Contract, actor, *, new_end: date, price_change_percent: Decimal = ZERO, note: str = '') -> Contract:
    """New term starting the day after the current end, as a new active version linked to the old one."""
    if c.status not in ('active', 'expired'):
        raise ContractError('Only an active or expired contract can be renewed.')
    if not c.end_date:
        raise ContractError('An open-ended contract does not need renewal.')
    if new_end <= c.end_date:
        raise ContractError('The new end date must be after the current end date.')
    if hasattr(c, 'amendment'):
        raise ContractError('This contract already has a newer version.')
    start = c.end_date + timedelta(days=1)
    factor = Decimal('1') + (price_change_percent or ZERO) / Decimal('100')
    with transaction.atomic():
        new = Contract.objects.create(
            number=c.number, version=c.version + 1, previous=c, client=c.client, title=c.title, owner=c.owner, status='draft',
            start_date=start, end_date=new_end, auto_renew=c.auto_renew, notice_days=c.notice_days, invoice_policy=c.invoice_policy,
            payment_terms_days=c.payment_terms_days, scope=c.scope, notes=note, amendment_reason='Renewal', created_by=actor)
        for ln in c.lines.filter(billing_model__in=list(RECURRING_MODELS)):
            ContractLine.objects.create(contract=new, service=ln.service, description=ln.description, quantity=ln.quantity,
                                        unit_price=q2(ln.unit_price * factor), billing_model=ln.billing_model, start_date=start,
                                        end_date=new_end, taxability=ln.taxability, tax_rate=ln.tax_rate, sac_code=ln.sac_code)
        result = activate(new, actor)
        audit.log('contract.renewed', c, actor=actor, changes={'new_end': new_end, 'price_change': str(price_change_percent)})
    return result


def record_lost_renewal(c: Contract, actor, reason: str):
    if len(reason.strip()) < 5:
        raise ContractError('Give the reason the client did not renew.')
    Contract.objects.filter(pk=c.pk).update(notes=(c.notes + f'\nNot renewed: {reason}').strip(), auto_renew=False)
    audit.log('contract.renewal_lost', c, actor=actor, reason=reason)


# ---------------------------------------------------------------- jobs (9.6)

def renewal_alerts() -> int:
    """Owner at 60 days, customer at 45 and 15, manager at 30; expire or auto-renew at the end."""
    today = india.today()
    n = 0
    steps = [(60, 1, 'owner'), (45, 2, 'customer'), (30, 3, 'manager'), (15, 4, 'customer')]
    for c in Contract.objects.filter(status='active', end_date__isnull=False).select_related('client', 'owner'):
        days = (c.end_date - today).days
        if days < 0:
            if c.auto_renew and not hasattr(c, 'amendment'):
                months = max(1, round((c.end_date - c.start_date).days / 30.4))
                try:
                    renew(c, c.owner, new_end=india.add_months(c.end_date, months))
                except ValidationError as exc:
                    notify.notify([c.owner], f'Auto-renewal failed for {c}', body=' '.join(exc.messages), url=contract_url(c),
                                  event_type='renewal', level='error')
            else:
                Contract.objects.filter(pk=c.pk).update(status='expired')
                audit.log('contract.expired', c)
                BillingSchedule.objects.filter(contract_line__contract=c, status='scheduled', period_start__gt=c.end_date).update(status='skipped')
            n += 1
            continue
        for limit, level, who in steps:
            if days <= limit and c.renewal_alert_level < level:
                Contract.objects.filter(pk=c.pk).update(renewal_alert_level=level)
                if who == 'owner':
                    notify.notify([c.owner], f'Renewal due in {days} days: {c.client}', url=contract_url(c), event_type='renewal',
                                  level='warning', email=True)
                elif who == 'manager':
                    notify.notify([notify.manager_of(c.owner) or c.owner], f'Renewal at risk in {days} days: {c.client}',
                                  url=contract_url(c), event_type='renewal', level='warning', email=True)
                else:
                    to = c.client.billing_contact.email if c.client.billing_contact and c.client.billing_contact.email else c.client.email
                    if to:
                        from crm.models import CompanySettings
                        co = CompanySettings.get()
                        notify.send_email(to, f'Your {c.title} renews on {c.end_date:%d-%b-%Y}',
                                          f'Dear {c.client.legal_name},\n\nYour contract {c.number} for {c.title} ends on '
                                          f'{c.end_date:%d-%b-%Y}. We will contact you about renewal.\n\nRegards,\n{co.legal_name}',
                                          obj=c, template_code='renewal_reminder')
                n += 1
                break
    return n


# ---------------------------------------------------------------- metrics (9.7)

def mrr(on: date | None = None) -> Decimal:
    on = on or india.today()
    total = ZERO
    for ln in ContractLine.objects.filter(contract__status='active', is_paused=False, start_date__lte=on).select_related('contract'):
        if ln.end_date and ln.end_date < on:
            continue
        total += ln.monthly_value
    return total


def mrr_movements(start: date, end: date) -> dict:
    """New, expansion, contraction and churned MRR between two dates by client."""
    def by_client(on):
        out = {}
        for ln in ContractLine.objects.filter(contract__status__in=['active', 'superseded', 'completed', 'terminated', 'expired'],
                                              start_date__lte=on, is_paused=False).select_related('contract'):
            c = ln.contract
            if ln.end_date and ln.end_date < on:
                continue
            if c.end_date and c.end_date < on or (c.terminated_on and c.terminated_on < on):
                continue
            nxt = getattr(c, 'amendment', None)
            if c.status == 'superseded' and nxt is not None and (nxt.effective_from or nxt.start_date) <= on:
                continue
            out[c.client_id] = out.get(c.client_id, ZERO) + ln.monthly_value
        return out
    a, b = by_client(start), by_client(end)
    new = sum((v for k, v in b.items() if k not in a), ZERO)
    churn = sum((v for k, v in a.items() if k not in b), ZERO)
    expansion = sum((b[k] - a[k] for k in b if k in a and b[k] > a[k]), ZERO)
    contraction = sum((a[k] - b[k] for k in b if k in a and b[k] < a[k]), ZERO)
    start_mrr, end_mrr = sum(a.values(), ZERO), sum(b.values(), ZERO)
    return {'start': start_mrr, 'end': end_mrr, 'new': new, 'expansion': expansion, 'contraction': contraction, 'churn': churn,
            'net_new': end_mrr - start_mrr, 'arr': end_mrr * 12}
