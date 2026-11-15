"""Monthly deliverables: the rules.

Two guarantees run through everything here:

* **Nothing is created twice.** An instance is unique per (plan item, original due date) in the database, so the
  nightly job, a manual "create now" and a plan edit can all run at once and never produce duplicates.
* **The promise is never rewritten.** ``original_due_date`` is set once. Rescheduling moves ``due_date`` only, and a
  plan edit never touches an instance a person has worked on, or one inside a frozen period.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_tz

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q, Sum
from django.urls import reverse
from django.utils import timezone

from crm.core import audit, india, notify, permissions
from crm.models import Deliverable, DeliverableAsset, DeliverableEvent, DeliveryPlan, PlanItem, PlanRevision
from crm.models.deliverables import HORIZON_DAYS, OPEN_STATUSES

logger = logging.getLogger('crm.deliverables')

BACKFILL_DAYS = 45            # a job outage shorter than this is caught up on the next run
MORNING_HOUR = 8              # "due today" reminders go out from 8 am IST
NOTE_MAX = 500


class DeliverableError(ValidationError):
    pass


def deliverable_url(d) -> str:
    return reverse('crm:deliverable_detail', args=[d.pk])


def plan_url(plan) -> str:
    return reverse('crm:delivery_plan', args=[plan.pk])


# ---------------------------------------------------------------- dates

def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def due_dates(item, start: date, end: date) -> list[date]:
    """Every date an item falls due inside [start, end], on real calendar months: a 31st clamps to the month end,
    so "the 31st" is the 28th, 29th or 30th in shorter months."""
    if start > end:
        return []
    out: list[date] = []
    if item.frequency == 'weekly':
        wanted = set(item.weekday_list)
        d = start
        while d <= end:
            if d.weekday() in wanted:
                out.append(d)
            d += timedelta(days=1)
    elif item.frequency in ('monthly', 'quarterly'):
        step = 1 if item.frequency == 'monthly' else 3
        anchor = item.start_date.year * 12 + item.start_date.month - 1
        y, m = start.year, start.month
        while date(y, m, 1) <= end:
            if (y * 12 + m - 1 - anchor) % step == 0:
                for day in item.day_list:
                    due = date(y, m, min(day, _last_day(y, m)))
                    if start <= due <= end:
                        out.append(due)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    elif item.frequency == 'once':
        if start <= item.start_date <= end:
            out.append(item.start_date)
    return sorted(set(out))


def _span(plan: DeliveryPlan, item: PlanItem, floor: date, upto: date) -> tuple[date, date]:
    c = plan.contract
    stops = [x for x in (upto, item.end_date, c.end_date, c.terminated_on) if x]
    return max(item.start_date, floor), min(stops)


# ---------------------------------------------------------------- events and saving

def _event(d: Deliverable, kind: str, text: str = '', actor=None):
    DeliverableEvent.objects.create(deliverable=d, kind=kind, text=text[:NOTE_MAX], actor=actor)


def _save(d: Deliverable, actor, *fields: str):
    d.row_version = (d.row_version or 0) + 1
    if actor is not None:
        d.updated_by = actor
    d.save(update_fields=[*fields, 'row_version', 'updated_by', 'updated_at'])


def _lock(pk: int) -> Deliverable:
    return Deliverable.objects.select_for_update().select_related('plan', 'client', 'assignee').get(pk=pk)


def _name(user) -> str:
    return (user.get_full_name() or user.get_username()) if user else 'nobody'


def _need(text: str, label: str, minimum: int = 3) -> str:
    text = (text or '').strip()
    if len(text) < minimum:
        raise DeliverableError(f'{label} is required.')
    return text[:NOTE_MAX]


# ---------------------------------------------------------------- generation

def _reviewer_for(item: PlanItem | None, assignee):
    if item is None or not item.requires_review:
        return None
    return item.reviewer or (notify.manager_of(assignee) if assignee else None)


def _new_instance(plan: DeliveryPlan, item: PlanItem, due: date) -> Deliverable:
    assignee = item.assignee or plan.assignee
    prefs = '\n'.join(x for x in (plan.client_preferences.strip(), item.preferences.strip()) if x)
    return Deliverable(
        plan=plan, plan_item=item, client=plan.client, title=item.title, category=item.category,
        period=due.replace(day=1), original_due_date=due, due_date=due, due_time=item.due_time,
        quantity=item.quantity, handled_by=item.handled_by, assignee=assignee, reviewer=_reviewer_for(item, assignee),
        requires_review=item.requires_review, needs_client_approval=item.needs_client_approval,
        client_state='needed' if item.needs_client_approval else 'none', preferences=prefs)


def _generate_item(plan: DeliveryPlan, item: PlanItem, floor: date, upto: date) -> int:
    start, end = _span(plan, item, floor, upto)
    dates = due_dates(item, start, end)
    if not dates:
        return 0
    have = {d.original_due_date: d for d in Deliverable.objects.filter(plan_item=item, original_due_date__in=dates)}
    carried = set()
    if item.carried_from_id:        # the same promise already exists on the previous contract version
        carried = set(Deliverable.objects.filter(plan_item=item.carried_from_id, original_due_date__in=dates,
                                                 auto_cancelled=False).exclude(status='cancelled')
                      .values_list('original_due_date', flat=True))
    made = 0
    for due in dates:
        row = have.get(due)
        if row is not None:
            if row.auto_cancelled:      # a plan edit removed it earlier and a later edit brought it back
                Deliverable.objects.filter(pk=row.pk, auto_cancelled=True).update(status='upcoming', auto_cancelled=False)
                _event(row, 'revived', 'Back on the plan.')
                made += 1
            continue
        if due in carried:
            continue
        try:
            with transaction.atomic():
                d = _new_instance(plan, item, due)
                d.save()
                _event(d, 'created', 'Created from the plan.')
            made += 1
        except IntegrityError:           # another run created it a moment ago
            continue
    return made


def generate(plan: DeliveryPlan, *, upto: date | None = None, today: date | None = None, after: date | None = None) -> int:
    """Create every due instance up to ``upto`` (default: 30 days ahead). Safe to run any number of times."""
    if plan.status != 'active':
        return 0
    today = today or india.today()
    upto = upto or today + timedelta(days=HORIZON_DAYS)
    floor = max(plan.effective_from, today - timedelta(days=BACKFILL_DAYS))
    if after:
        floor = max(floor, after + timedelta(days=1))
    made = 0
    for item in plan.items.filter(is_removed=False, is_paused=False):
        made += _generate_item(plan, item, floor, upto)
    return made


def generate_all(upto: date | None = None) -> int:
    n = 0
    for plan in DeliveryPlan.objects.filter(status='active', contract__status='active').select_related('contract'):
        try:
            n += generate(plan, upto=upto)
        except Exception:
            logger.exception('Generating deliverables for plan %s failed', plan.pk)
            notify.notify([plan.account_owner], f'Could not create deliverables for {plan.client}',
                          body='A scheduling problem was logged. Open the plan and press "Create upcoming items".',
                          url=plan_url(plan), event_type='deliverable_failed', level='warning',
                          dedupe_key=f'dl-gen-fail:{plan.pk}:{india.today():%Y%m%d}')
    return n


# ---------------------------------------------------------------- plans

def snapshot(plan: DeliveryPlan) -> dict:
    return {
        'account_owner': _name(plan.account_owner), 'assignee': _name(plan.assignee), 'frozen_through': str(plan.frozen_through or ''),
        'client_preferences': plan.client_preferences,
        'items': [{'title': i.title, 'schedule': i.schedule_text, 'quantity': i.quantity, 'time': i.due_time.strftime('%H:%M'),
                   'assignee': _name(i.assignee) if i.assignee else '', 'handled_by': i.handled_by, 'review': i.requires_review,
                   'client_approval': i.needs_client_approval, 'paused': i.is_paused, 'removed': i.is_removed}
                  for i in plan.items.all()],
    }


def record_revision(plan: DeliveryPlan, actor, summary: str) -> PlanRevision:
    plan = DeliveryPlan.objects.select_for_update().get(pk=plan.pk)
    plan.revision += 1
    plan.save(update_fields=['revision', 'updated_at'])
    return PlanRevision.objects.create(plan=plan, number=plan.revision, summary=summary[:300], snapshot=snapshot(plan),
                                       changed_by=actor)


def _default_owner(contract):
    c = contract.client
    if c.owner_id and c.owner.is_active:
        return c.owner
    return contract.owner


@transaction.atomic
def create_plan(contract, actor, *, force: bool = False) -> DeliveryPlan | None:
    """Build the client's plan from the services on the contract. Returns None when the contract has nothing
    recurring to deliver (unless ``force``)."""
    if hasattr(contract, 'delivery_plan'):
        return contract.delivery_plan
    if contract.previous_id and hasattr(contract.previous, 'delivery_plan'):
        return carry_forward(contract.previous.delivery_plan, contract, actor)
    from crm.models.catalog import RECURRING_MODELS
    lines = list(contract.lines.select_related('service'))
    templates, placeholders, seen = [], [], set()
    for ln in lines:
        svc = ln.service
        if svc is None or svc.pk in seen:
            continue
        seen.add(svc.pk)
        rows = list(svc.deliverable_items.all())
        if rows:
            templates += [(ln, r) for r in rows]
        elif ln.billing_model in RECURRING_MODELS:
            placeholders += [(ln, x.strip(' -*\u2022')) for x in (svc.deliverables or '').splitlines() if x.strip(' -*\u2022')]
    if not templates and not placeholders and not force and not any(ln.billing_model in RECURRING_MODELS for ln in lines):
        return None
    owner = _default_owner(contract)
    plan = DeliveryPlan.objects.create(contract=contract, client=contract.client, account_owner=owner,
                                       effective_from=max(contract.start_date, india.today()), created_by=actor)
    for ln, r in templates:
        PlanItem.objects.create(
            plan=plan, source=r, title=r.title, category=r.category, frequency=r.frequency, weekdays=r.weekdays,
            month_days=r.month_days, due_time=r.due_time, quantity=r.quantity, handled_by=r.handled_by,
            start_date=max(ln.start_date, contract.start_date), end_date=ln.end_date, requires_review=r.requires_review,
            needs_client_approval=r.needs_client_approval, preferences=r.preferences, sort=r.sort, created_by=actor)
    for n, (ln, text) in enumerate(placeholders):
        # Free-text promises have no schedule, so they start paused and ask someone to choose the days.
        PlanItem.objects.create(plan=plan, title=text[:200], frequency='monthly', start_date=max(ln.start_date, contract.start_date),
                                end_date=ln.end_date, is_paused=True, sort=100 + n, created_by=actor,
                                preferences='Imported from the service description. Choose the days, then switch it on.')
    record_revision(plan, actor, 'Plan created from the contract services.')
    audit.log('deliverables.plan_created', plan, actor=actor, changes={'contract': str(contract), 'items': plan.items.count()})
    generate(plan)
    return plan


@transaction.atomic
def carry_forward(old: DeliveryPlan, new_contract, actor) -> DeliveryPlan:
    """Renewal or amendment: the new contract version starts with the same promises, owner and preferences.
    Untouched future items of the old plan are cancelled so nothing is due twice; worked-on ones stay with the old plan."""
    if hasattr(new_contract, 'delivery_plan'):
        return new_contract.delivery_plan
    start = new_contract.effective_from or new_contract.start_date
    plan = DeliveryPlan.objects.create(
        contract=new_contract, client=new_contract.client, account_owner=old.account_owner, default_assignee=old.default_assignee,
        effective_from=max(start, india.today()), client_preferences=old.client_preferences, carried_from=old, created_by=actor)
    for it in old.items.filter(is_removed=False):
        PlanItem.objects.create(
            plan=plan, source=it.source, title=it.title, category=it.category, frequency=it.frequency, weekdays=it.weekdays,
            month_days=it.month_days, due_time=it.due_time, quantity=it.quantity, handled_by=it.handled_by,
            start_date=max(it.start_date, new_contract.start_date), end_date=it.end_date, assignee=it.assignee,
            reviewer=it.reviewer, requires_review=it.requires_review, needs_client_approval=it.needs_client_approval,
            preferences=it.preferences, is_paused=it.is_paused, carried_from=it, sort=it.sort, created_by=actor)
    record_revision(plan, actor, f'Carried forward from {old.contract}.')
    if old.status != 'closed':
        DeliveryPlan.objects.filter(pk=old.pk).update(status='closed')
    _cancel_untouched(old.deliverables.all(), after=start - timedelta(days=1), reason='Replaced by the new contract version.')
    audit.log('deliverables.plan_carried_forward', plan, actor=actor, changes={'from': str(old.contract), 'to': str(new_contract)})
    generate(plan)
    return plan


def contract_activated(contract, actor) -> None:
    """Called after a contract is activated. Never blocks activation: a failure is logged and the owner told."""
    try:
        create_plan(contract, actor)
    except Exception:
        logger.exception('Delivery plan for contract %s failed', contract.pk)
        notify.notify([contract.owner], f'Set up the monthly deliverables for {contract.client}',
                      body='The plan could not be created automatically. Open the contract and create it.',
                      url=reverse('crm:contract_detail', args=[contract.pk]), event_type='deliverable_failed', level='warning')


def contract_status_changed(contract) -> None:
    """Keep the plan in step with its contract: pause on suspend, resume, close on terminate or expiry."""
    contract = type(contract).objects.get(pk=contract.pk)          # callers update the status with a queryset
    plan = getattr(contract, 'delivery_plan', None)
    if plan is None:
        return
    try:
        if contract.status == 'suspended' and plan.status == 'active':
            DeliveryPlan.objects.filter(pk=plan.pk).update(status='paused')
        elif contract.status == 'active' and plan.status == 'paused':
            DeliveryPlan.objects.filter(pk=plan.pk).update(status='active')
            plan.refresh_from_db()
            generate(plan)
        elif contract.status in ('terminated', 'expired', 'completed') and plan.status != 'closed':
            DeliveryPlan.objects.filter(pk=plan.pk).update(status='closed')
            edge = contract.terminated_on or contract.end_date or india.today()
            _cancel_untouched(plan.deliverables.all(), after=edge, reason=f'Contract {contract.get_status_display().lower()}.')
    except Exception:
        logger.exception('Syncing plan %s with contract %s failed', plan.pk, contract.pk)


def _cancel_untouched(qs, *, after: date, reason: str, actor=None) -> int:
    """Cancel placeholder instances nobody has worked on, due after ``after``. Worked-on ones are never touched."""
    n = 0
    for d in qs.filter(status='upcoming', locked=False, quantity_done=0, due_date__gt=after, auto_cancelled=False):
        if Deliverable.objects.filter(pk=d.pk, status='upcoming', locked=False).update(status='cancelled', auto_cancelled=True):
            _event(d, 'auto_cancelled', reason, actor)
            n += 1
    return n


# ---------------------------------------------------------------- plan editing

def _check_item(item: PlanItem):
    if not item.title.strip():
        raise DeliverableError('Give the item a name.')
    if item.quantity < 1:
        raise DeliverableError('Quantity must be at least 1.')
    if item.end_date and item.end_date < item.start_date:
        raise DeliverableError('The end date is before the start date.')
    if item.frequency == 'weekly' and not item.weekday_list:
        raise DeliverableError('Choose the day or days of the week.')
    if item.frequency in ('monthly', 'quarterly') and not item.day_list:
        raise DeliverableError('Choose the day of the month.')


def _writable(plan: DeliveryPlan):
    if plan.status == 'closed':
        raise DeliverableError('This plan is closed. Edit the plan of the current contract instead.')


@transaction.atomic
def save_item(plan: DeliveryPlan, item: PlanItem, actor, *, creating: bool, summary: str = '') -> PlanItem:
    """Persist an edited or new item, then bring the future instances in line with it."""
    _writable(plan)
    _check_item(item)
    old_assignee = None
    if not creating and item.pk:
        old = PlanItem.objects.get(pk=item.pk)
        old_assignee = old.assignee_id or plan.assignee.pk
    item.plan = plan
    if creating:
        item.created_by = actor
        item.sort = (plan.items.aggregate(m=Max('sort'))['m'] or 0) + 1
    item.updated_by = actor
    item.save()
    sync_item(item, actor, old_assignee=old_assignee)
    record_revision(plan, actor, summary or (f'Added "{item.title}".' if creating else f'Changed "{item.title}".'))
    audit.log('deliverables.item_added' if creating else 'deliverables.item_changed', plan, actor=actor,
              changes={'item': item.title, 'schedule': item.schedule_text})
    return item


def sync_item(item: PlanItem, actor, *, old_assignee=None) -> None:
    plan = item.plan
    today = india.today()
    frozen = plan.frozen_through
    # Past dates, started work and anything inside a freeze are never rewritten. An item due today that nobody has touched follows the plan.
    floor_after = max([x for x in (today - timedelta(days=1), frozen) if x])
    future = Deliverable.objects.filter(plan_item=item, status='upcoming', locked=False, quantity_done=0,
                                        due_date__gt=floor_after, auto_cancelled=False)
    if not item.active:
        for d in future:
            if Deliverable.objects.filter(pk=d.pk, status='upcoming', locked=False).update(status='cancelled', auto_cancelled=True):
                _event(d, 'auto_cancelled', 'Removed from the plan.' if item.is_removed else 'The item is paused.', actor)
        return
    upto = today + timedelta(days=HORIZON_DAYS)
    start, end = _span(plan, item, floor_after + timedelta(days=1), upto)
    wanted = set(due_dates(item, start, end))
    for d in future:
        if d.original_due_date not in wanted:
            if Deliverable.objects.filter(pk=d.pk, status='upcoming', locked=False).update(status='cancelled', auto_cancelled=True):
                _event(d, 'auto_cancelled', 'The schedule changed.', actor)
    new_assignee = item.assignee or plan.assignee
    for d in Deliverable.objects.filter(plan_item=item, status='upcoming', locked=False, quantity_done=0,
                                        due_date__gt=floor_after, auto_cancelled=False):
        d.title, d.category, d.quantity, d.handled_by, d.due_time = item.title, item.category, item.quantity, item.handled_by, item.due_time
        d.requires_review, d.needs_client_approval = item.requires_review, item.needs_client_approval
        d.client_state = 'needed' if item.needs_client_approval else 'none'
        d.preferences = '\n'.join(x for x in (plan.client_preferences.strip(), item.preferences.strip()) if x)
        if old_assignee is None or d.assignee_id == old_assignee:
            d.assignee = new_assignee
        d.reviewer = _reviewer_for(item, d.assignee)
        d.save(update_fields=['title', 'category', 'quantity', 'handled_by', 'due_time', 'requires_review',
                              'needs_client_approval', 'client_state', 'preferences', 'assignee', 'reviewer', 'updated_at'])
    _generate_item(plan, item, floor_after + timedelta(days=1), upto)


@transaction.atomic
def remove_item(item: PlanItem, actor, reason: str):
    reason = _need(reason, 'A reason', 5)
    _writable(item.plan)
    item.is_removed, item.removed_reason, item.updated_by = True, reason[:300], actor
    item.save(update_fields=['is_removed', 'removed_reason', 'updated_by', 'updated_at'])
    sync_item(item, actor)
    record_revision(item.plan, actor, f'Removed "{item.title}": {reason}')
    audit.log('deliverables.item_removed', item.plan, actor=actor, reason=reason, changes={'item': item.title})


@transaction.atomic
def set_paused(item: PlanItem, actor, paused: bool):
    _writable(item.plan)
    if not paused and item.needs_schedule:
        raise DeliverableError('Choose the days before switching this item on.')
    item.is_paused, item.updated_by = paused, actor
    item.save(update_fields=['is_paused', 'updated_by', 'updated_at'])
    sync_item(item, actor)
    record_revision(item.plan, actor, f'{"Paused" if paused else "Resumed"} "{item.title}".')


@transaction.atomic
def set_preferences(plan: DeliveryPlan, actor, text: str):
    _writable(plan)
    plan.client_preferences = (text or '').strip()[:2000]
    plan.save(update_fields=['client_preferences', 'updated_at'])
    for item in plan.items.filter(is_removed=False):
        for d in Deliverable.objects.filter(plan_item=item, status='upcoming', locked=False, due_date__gte=india.today()):
            d.preferences = '\n'.join(x for x in (plan.client_preferences, item.preferences.strip()) if x)
            d.save(update_fields=['preferences', 'updated_at'])
    record_revision(plan, actor, 'Client preferences updated.')


@transaction.atomic
def freeze(plan: DeliveryPlan, actor, through: date | None):
    """Lock every deliverable due on or before ``through`` against plan edits. Pass None to lift the lock."""
    if through and through < india.today() - timedelta(days=366):
        raise DeliverableError('That date is too far back.')
    plan.frozen_through = through
    plan.save(update_fields=['frozen_through', 'updated_at'])
    record_revision(plan, actor, f'Frozen through {through:%d %b %Y}.' if through else 'Freeze lifted.')
    audit.log('deliverables.plan_frozen', plan, actor=actor, changes={'through': through})


@transaction.atomic
def reassign_plan(plan: DeliveryPlan, to_user, actor, *, account_owner: bool = False, note: str = '') -> int:
    """Give a client to another executive: the plan's default assignee (and optionally the account owner) and every
    open item that was with the previous person. Items someone else was named on stay where they are."""
    _writable(plan)
    if not to_user.is_active:
        raise DeliverableError('Choose an active user.')
    prev = plan.assignee
    plan.default_assignee = to_user
    fields = ['default_assignee', 'updated_at']
    if account_owner:
        plan.account_owner = to_user
        fields.append('account_owner')
    plan.save(update_fields=fields)
    moved = 0
    for d in Deliverable.objects.filter(plan=plan, status__in=OPEN_STATUSES, assignee=prev):
        d.assignee = to_user
        if d.requires_review and (d.reviewer_id is None or d.reviewer_id == to_user.pk):
            d.reviewer = _reviewer_for(d.plan_item, to_user)
        _save(d, actor, 'assignee', 'reviewer')
        _event(d, 'reassigned', f'{_name(prev)} to {_name(to_user)}.{(" " + note) if note else ""}', actor)
        moved += 1
    record_revision(plan, actor, f'Reassigned to {_name(to_user)} ({moved} open items).')
    audit.log('deliverables.plan_reassigned', plan, actor=actor, reason=note,
              changes={'from': _name(prev), 'to': _name(to_user), 'moved': moved, 'owner_too': account_owner})
    recipients = [to_user] + ([prev] if prev and prev.pk != to_user.pk else [])
    notify.notify(recipients, f'{plan.client} deliverables moved to {_name(to_user)}',
                  body=f'{moved} open deliverable{"s" if moved != 1 else ""}.', url=plan_url(plan), event_type='deliverable_assigned')
    return moved


@transaction.atomic
def add_adhoc(plan: DeliveryPlan, actor, *, title: str, due: date, quantity: int = 1, assignee=None, instructions: str = '',
              handled_by: str = '', category: str = '', due_time: time | None = None) -> Deliverable:
    """A one-off deliverable that is not part of the recurring plan."""
    _writable(plan)
    title = _need(title, 'A name', 2)
    if due < india.today() - timedelta(days=1):
        raise DeliverableError('The due date is in the past.')
    assignee = assignee or plan.assignee
    d = Deliverable(plan=plan, plan_item=None, client=plan.client, title=title[:200], category=category[:60], period=due.replace(day=1),
                    original_due_date=due, due_date=due, quantity=max(1, quantity), handled_by=handled_by[:60], assignee=assignee,
                    instructions=instructions.strip()[:2000], locked=True, preferences=plan.client_preferences, created_by=actor)
    if due_time:
        d.due_time = due_time
    d.save()
    _event(d, 'created', 'Added as a one-off.', actor)
    audit.log('deliverables.adhoc_added', d, actor=actor, changes={'due': due, 'client': str(plan.client)})
    notify.notify([assignee] if assignee and assignee.pk != actor.pk else [], f'New deliverable: {d.title} for {plan.client}',
                  body=f'Due {due:%a %d %b}.', url=deliverable_url(d), event_type='deliverable_assigned')
    return d


# ---------------------------------------------------------------- working on a deliverable

def _guard_open(d: Deliverable):
    if d.status not in OPEN_STATUSES:
        raise DeliverableError(f'This is already {d.get_status_display().lower()}.')


@transaction.atomic
def start(pk: int, actor) -> Deliverable:
    d = _lock(pk)
    _guard_open(d)
    if d.status == 'upcoming':
        d.status = 'in_progress'
        _save(d, actor, 'status')
        _event(d, 'started', '', actor)
    return d


@transaction.atomic
def log_progress(pk: int, actor, *, done: int, note: str = '') -> Deliverable:
    """Record how many units are finished so far (a partial delivery). Moves the item to in progress."""
    d = _lock(pk)
    _guard_open(d)
    if done < 0 or done > d.quantity:
        raise DeliverableError(f'Enter a number from 0 to {d.quantity}.')
    before = d.quantity_done
    d.quantity_done = done
    if d.status == 'upcoming' and done:
        d.status = 'in_progress'
    d.locked = True
    _save(d, actor, 'quantity_done', 'status', 'locked')
    _event(d, 'progress', f'{before} to {done} of {d.quantity} done.{(" " + note.strip()) if note.strip() else ""}', actor)
    return d


@transaction.atomic
def set_instructions(pk: int, actor, text: str) -> Deliverable:
    d = _lock(pk)
    d.instructions = (text or '').strip()[:2000]
    d.locked = True
    _save(d, actor, 'instructions', 'locked')
    _event(d, 'instructions', 'Client request for this one updated.', actor)
    return d


@transaction.atomic
def add_note(pk: int, actor, text: str) -> DeliverableEvent:
    d = _lock(pk)
    text = _need(text, 'A note', 2)
    ev = DeliverableEvent.objects.create(deliverable=d, kind='note', text=text, actor=actor)
    d.locked = True
    d.save(update_fields=['locked', 'updated_at'])
    return ev


def can_review(user, d: Deliverable) -> bool:
    if permissions.is_super_admin(user):
        return True
    if user.pk == d.assignee_id:
        return False                         # nobody approves their own work
    return user.pk == d.reviewer_id or permissions.in_scope(user, 'deliverables', 'approve', d)


@transaction.atomic
def submit_review(pk: int, actor, *, note: str) -> Deliverable:
    d = _lock(pk)
    _guard_open(d)
    note = _need(note, 'What is being reviewed (file or work name)')
    if not d.requires_review:
        raise DeliverableError('This item does not need a review.')
    d.status, d.review_state, d.review_requested_at, d.review_note = 'in_review', 'pending', timezone.now(), note
    d.review_nudged_at = None
    d.locked = True
    reviewer = d.reviewer or notify.manager_of(d.assignee) if d.assignee else d.reviewer
    if reviewer and d.reviewer_id is None:
        d.reviewer = reviewer
    _save(d, actor, 'status', 'review_state', 'review_requested_at', 'review_note', 'review_nudged_at', 'locked', 'reviewer')
    _event(d, 'review_requested', note, actor)
    targets = [d.reviewer] if d.reviewer else notify.owners()
    notify.notify(targets, f'Review needed: {d.title} for {d.client}', body=note, url=deliverable_url(d),
                  event_type='deliverable_review', level='warning', dedupe_key=f'dl-review:{d.pk}:{d.review_requested_at:%Y%m%d%H%M%S}')
    return d


@transaction.atomic
def review(pk: int, actor, *, approve: bool, note: str = '') -> Deliverable:
    d = _lock(pk)
    if d.status != 'in_review':
        raise DeliverableError('This is not waiting for a review.')
    if not can_review(actor, d):
        raise DeliverableError('Someone else needs to review this. You cannot approve your own work.')
    if not approve:
        note = _need(note, 'What needs to change')
    d.review_state = 'approved' if approve else 'changes'
    d.status = 'in_progress'
    d.reviewed_by, d.reviewed_at, d.review_note = actor, timezone.now(), (note or d.review_note)[:500]
    _save(d, actor, 'review_state', 'status', 'reviewed_by', 'reviewed_at', 'review_note')
    _event(d, 'review_approved' if approve else 'review_changes', note, actor)
    notify.notify([d.assignee], f'{"Approved" if approve else "Changes requested"}: {d.title} for {d.client}',
                  body=note or 'Ready to send or deliver.', url=deliverable_url(d), event_type='deliverable_review',
                  level='info' if approve else 'warning')
    return d


@transaction.atomic
def send_to_client(pk: int, actor, *, note: str = '') -> Deliverable:
    """Mark the work as sent to the client for approval, which starts the clock on their answer."""
    d = _lock(pk)
    _guard_open(d)
    if d.requires_review and d.review_state != 'approved':
        raise DeliverableError('Get the internal review done before sending it to the client.')
    d.status, d.client_state, d.client_sent_at, d.client_answered_at = 'awaiting_client', 'awaiting', timezone.now(), None
    d.client_nudge_level = 0
    d.locked = True
    _save(d, actor, 'status', 'client_state', 'client_sent_at', 'client_answered_at', 'client_nudge_level', 'locked')
    _event(d, 'sent_to_client', note or 'Sent to the client for approval.', actor)
    return d


@transaction.atomic
def client_answer(pk: int, actor, *, approved: bool, note: str = '') -> Deliverable:
    d = _lock(pk)
    if d.status != 'awaiting_client':
        raise DeliverableError('This is not waiting for the client.')
    if not approved:
        note = _need(note, 'What the client wants changed')
    d.client_state = 'approved' if approved else 'changes'
    d.client_answered_at = timezone.now()
    d.status = 'in_progress'
    if not approved:
        d.client_revisions += 1
        if d.requires_review:
            d.review_state = 'changes'
    _save(d, actor, 'client_state', 'client_answered_at', 'status', 'client_revisions', 'review_state')
    days = d.client_wait_days
    _event(d, 'client_approved' if approved else 'client_changes',
           f'{note} (client took {days} day{"s" if days != 1 else ""})'.strip(), actor)
    return d


@transaction.atomic
def deliver(pk: int, actor, *, note: str, done: int | None = None, shortfall_reason: str = '') -> Deliverable:
    """Mark delivered. The note names what was delivered. Delivering fewer than promised needs a reason."""
    d = _lock(pk)
    _guard_open(d)
    note = _need(note, 'The delivery note (file, ad or report name)')
    done = d.quantity if done is None else done
    if done < 1 or done > d.quantity:
        raise DeliverableError(f'Enter a number from 1 to {d.quantity}.')
    if d.requires_review and d.review_state != 'approved':
        raise DeliverableError('This needs an internal review first.')
    if d.needs_client_approval and d.client_state != 'approved':
        raise DeliverableError('This needs the client approval first.')
    if done < d.quantity:
        shortfall_reason = _need(shortfall_reason, 'The reason it is short')
        d.shortfall_reason = shortfall_reason[:300]
    d.quantity_done, d.status = done, 'delivered'
    d.delivery_note, d.delivered_at, d.delivered_by = note[:300], timezone.now(), actor
    d.locked = True
    _save(d, actor, 'quantity_done', 'status', 'delivery_note', 'delivered_at', 'delivered_by', 'shortfall_reason', 'locked')
    late = ' (late)' if d.was_late_against_contract else ''
    _event(d, 'delivered', f'{done} of {d.quantity}: {note}{late}', actor)
    audit.log('deliverables.delivered', d, actor=actor, changes={'client': str(d.client), 'done': done, 'of': d.quantity, 'late': d.was_late_against_contract})
    if done < d.quantity and d.plan.account_owner_id != actor.pk:
        notify.notify([d.plan.account_owner], f'Short delivery: {d.title} for {d.client}',
                      body=f'{done} of {d.quantity}. {d.shortfall_reason}', url=deliverable_url(d), event_type='deliverable_short', level='warning')
    return d


@transaction.atomic
def reschedule(pk: int, actor, *, new_date: date, reason: str, new_time: time | None = None) -> Deliverable:
    """Move the working date. The contractual date stays on record."""
    d = _lock(pk)
    _guard_open(d)
    reason = _need(reason, 'A reason', 5)
    if new_date < india.today():
        raise DeliverableError('Choose today or a later date.')
    if new_date == d.due_date and (not new_time or new_time == d.due_time):
        raise DeliverableError('That is the date it already has.')
    old = d.due_date
    d.due_date, d.reschedule_count, d.reschedule_reason = new_date, d.reschedule_count + 1, reason[:300]
    if new_time:
        d.due_time = new_time
    d.reminder_level = 0                  # the reminders start again for the new date
    d.missed_at = None
    d.locked = True
    _save(d, actor, 'due_date', 'due_time', 'reschedule_count', 'reschedule_reason', 'reminder_level', 'missed_at', 'locked')
    _event(d, 'rescheduled', f'{old:%d %b} to {new_date:%d %b}: {reason}', actor)
    audit.log('deliverables.rescheduled', d, actor=actor, reason=reason,
              changes={'from': old, 'to': new_date, 'contract_date': d.original_due_date, 'count': d.reschedule_count})
    if new_date > d.original_due_date + timedelta(days=7) and d.plan.account_owner_id != actor.pk:
        notify.notify([d.plan.account_owner], f'Pushed back a week or more: {d.title} for {d.client}',
                      body=f'Promised {d.original_due_date:%d %b}, now {new_date:%d %b}. {reason}', url=deliverable_url(d),
                      event_type='deliverable_rescheduled', level='warning')
    return d


@transaction.atomic
def close_out(pk: int, actor, *, skip: bool, reason: str) -> Deliverable:
    """Skip (the client agreed to drop it this month) or cancel (it should never have been scheduled)."""
    d = _lock(pk)
    _guard_open(d)
    reason = _need(reason, 'A reason', 5)
    d.status = 'skipped' if skip else 'cancelled'
    d.locked = True
    _save(d, actor, 'status', 'locked')
    _event(d, 'skipped' if skip else 'cancelled', reason, actor)
    audit.log('deliverables.skipped' if skip else 'deliverables.cancelled', d, actor=actor, reason=reason, changes={'client': str(d.client)})
    if d.plan.account_owner_id != actor.pk:
        notify.notify([d.plan.account_owner], f'{"Skipped" if skip else "Cancelled"}: {d.title} for {d.client}', body=reason,
                      url=deliverable_url(d), event_type='deliverable_skipped')
    return d


@transaction.atomic
def reassign(pk: int, actor, to_user, *, note: str = '') -> Deliverable:
    d = _lock(pk)
    _guard_open(d)
    if not to_user.is_active:
        raise DeliverableError('Choose an active user.')
    prev = d.assignee
    d.assignee = to_user
    if d.requires_review and (d.reviewer_id is None or d.reviewer_id == to_user.pk):
        d.reviewer = _reviewer_for(d.plan_item, to_user)
    d.locked = True
    _save(d, actor, 'assignee', 'reviewer', 'locked')
    _event(d, 'reassigned', f'{_name(prev)} to {_name(to_user)}.{(" " + note.strip()) if note.strip() else ""}', actor)
    audit.log('deliverables.reassigned', d, actor=actor, reason=note, changes={'from': _name(prev), 'to': _name(to_user)})
    notify.notify([to_user] + ([prev] if prev and prev.pk != to_user.pk else []), f'{d.title} for {d.client} is now with {_name(to_user)}',
                  body=f'Due {d.due_date:%a %d %b}.', url=deliverable_url(d), event_type='deliverable_assigned')
    return d


MAX_UPLOAD = 25 * 1024 * 1024
BLOCKED_EXT = {'exe', 'bat', 'cmd', 'com', 'scr', 'js', 'vbs', 'msi', 'ps1', 'sh', 'php', 'jar', 'html', 'htm', 'svg'}


@transaction.atomic
def add_asset(pk: int, actor, *, name: str, kind: str = 'file', url: str = '', upload=None) -> DeliverableAsset:
    d = _lock(pk)
    name = _need(name, 'A name', 2)
    if not url and upload is None:
        raise DeliverableError('Add a link or choose a file.')
    if upload is not None:
        ext = upload.name.rsplit('.', 1)[-1].lower() if '.' in upload.name else ''
        if ext in BLOCKED_EXT:
            raise DeliverableError('That file type is not allowed.')
        if upload.size > min(MAX_UPLOAD, getattr(settings, 'CRM_MAX_UPLOAD_MB', 10) * 1024 * 1024):
            raise DeliverableError(f'The file is larger than {getattr(settings, "CRM_MAX_UPLOAD_MB", 10)} MB.')
    version = DeliverableAsset.objects.filter(deliverable=d, name__iexact=name).count() + 1
    asset = DeliverableAsset.objects.create(deliverable=d, client=d.client, kind=kind if kind in dict(DeliverableAsset.KINDS) else 'file',
                                            name=name[:200], url=url[:500], file=upload or '', version=version, added_by=actor)
    _event(d, 'asset', f'Added {asset.name}{" v" + str(version) if version > 1 else ""}.', actor)
    return asset


# ---------------------------------------------------------------- reading: summaries

def month_start(d: date | None = None) -> date:
    return (d or india.today()).replace(day=1)


def month_summary(client, period: date) -> dict:
    """Promised against delivered for one client and month, per item."""
    rows: dict[str, dict] = {}
    qs = Deliverable.objects.filter(client=client, period=period).exclude(status='cancelled').select_related('assignee').order_by('due_date')
    for d in qs:
        r = rows.setdefault(d.title, {'title': d.title, 'category': d.category, 'promised': 0, 'delivered': 0, 'late': 0,
                                      'open': 0, 'skipped': 0, 'items': []})
        r['items'].append(d)
        if d.status == 'skipped':
            r['skipped'] += d.quantity
            continue
        r['promised'] += d.quantity
        r['delivered'] += d.quantity_done
        if d.status == 'delivered' and d.was_late_against_contract:
            r['late'] += 1
        if d.is_open:
            r['open'] += 1
    promised = sum(r['promised'] for r in rows.values())
    delivered = sum(r['delivered'] for r in rows.values())
    return {'rows': list(rows.values()), 'promised': promised, 'delivered': delivered,
            'percent': int(delivered * 100 / promised) if promised else 0,
            'late': sum(r['late'] for r in rows.values()), 'open': sum(r['open'] for r in rows.values())}


def client_health(period: date | None = None) -> list[dict]:
    """Clients where something is overdue or the month is behind, for the manager board."""
    period = period or month_start()
    out = []
    qs = (Deliverable.objects.filter(period=period).exclude(status__in=['cancelled', 'skipped'])
          .values('client_id', 'client__display_name')
          .annotate(promised=Sum('quantity'), done=Sum('quantity_done'),
                    overdue=Count('id', filter=Q(status__in=OPEN_STATUSES, due_date__lt=india.today()))))
    for r in qs:
        pct = int(r['done'] * 100 / r['promised']) if r['promised'] else 100
        out.append({'client_id': r['client_id'], 'name': r['client__display_name'], 'promised': r['promised'], 'done': r['done'],
                    'overdue': r['overdue'], 'percent': pct})
    out.sort(key=lambda x: (-x['overdue'], x['percent']))
    return out


def team_load(users) -> list[dict]:
    """How many open deliverables each person is carrying, so work can be balanced. A count, not hours: the
    executives coordinate the media and marketing teams, so the effort is not theirs to measure."""
    today = india.today()
    week_end = today + timedelta(days=7)
    out = []
    open_qs = Deliverable.objects.filter(status__in=OPEN_STATUSES, plan__status='active')
    for u in users:
        mine = list(open_qs.filter(assignee=u).only('status', 'due_date', 'due_time'))
        out.append({
            'user': u, 'open': len(mine),
            'overdue': sum(1 for d in mine if d.is_overdue),
            'today': sum(1 for d in mine if d.due_date == today),
            'week': sum(1 for d in mine if today < d.due_date <= week_end),
            'later': sum(1 for d in mine if d.due_date > week_end),
            'waiting': sum(1 for d in mine if d.status in ('in_review', 'awaiting_client')),
        })
    out.sort(key=lambda r: (-r['overdue'], -r['open']))
    return out


# ---------------------------------------------------------------- calendar feed

def _ics_escape(text: str) -> str:
    return (text or '').replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\r', '').replace('\n', '\\n')


def ics_feed(user) -> str:
    """Deliverables, project tasks and lead follow-ups for one person as an iCalendar file."""
    from crm.models import FollowUp, Task
    base = settings.SITE_BASE_URL
    now = timezone.now().strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//BThinkX//Deliverables//EN', 'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
             'X-WR-CALNAME:BThinkX work', 'REFRESH-INTERVAL;VALUE=DURATION:PT1H']

    def event(uid, start_dt, minutes, summary, desc, url, alarm_minutes):
        end_dt = start_dt + timedelta(minutes=max(minutes, 15))
        lines.extend(['BEGIN:VEVENT', f'UID:{uid}@bthinkx', f'DTSTAMP:{now}',
                      f'DTSTART:{start_dt.astimezone(dt_tz.utc):%Y%m%dT%H%M%SZ}', f'DTEND:{end_dt.astimezone(dt_tz.utc):%Y%m%dT%H%M%SZ}',
                      f'SUMMARY:{_ics_escape(summary)}', f'DESCRIPTION:{_ics_escape(desc)}', f'URL:{base}{url}',
                      'BEGIN:VALARM', 'ACTION:DISPLAY', f'DESCRIPTION:{_ics_escape(summary)}', f'TRIGGER:-PT{alarm_minutes}M', 'END:VALARM',
                      'END:VEVENT'])

    window = india.today() - timedelta(days=14)
    for d in Deliverable.objects.filter(assignee=user, status__in=OPEN_STATUSES, due_date__gte=window).select_related('client')[:400]:
        event(f'dl{d.pk}', d.due_at - timedelta(minutes=30), 30,
              f'Deliver: {d.title}, {d.client}', f'{d.instructions}\n{d.preferences}'.strip(), deliverable_url(d), 120)
    for t in Task.objects.filter(assignee=user, due_date__gte=window).exclude(status__in=['done', 'cancelled']).select_related('project')[:400]:
        start_dt = timezone.make_aware(datetime.combine(t.due_date, time(10, 0)))
        event(f't{t.pk}', start_dt, 60, f'Task: {t.title}', f'Project {t.project}', reverse('crm:task_list'), 60)
    for f in FollowUp.objects.filter(assignee=user, status='pending', due_at__gte=timezone.now() - timedelta(days=14)).select_related('lead')[:400]:
        event(f'f{f.pk}', f.due_at, 30, f'{f.get_kind_display()}: {f.lead}', f.purpose, reverse('crm:lead_detail', args=[f.lead_id]),
              f.reminder_minutes or 15)
    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines) + '\r\n'
