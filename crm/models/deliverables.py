"""Monthly deliverables: what a client was promised, when it is due, who does it, and whether it was delivered.

Four layers, kept apart on purpose so that editing one never rewrites another:

* ``ServiceDeliverable``  the template that belongs to a service (what that plan normally includes).
* ``DeliveryPlan`` / ``PlanItem``  the client's own copy, attached to a contract. Editable per client.
* ``Deliverable``  one dated instance generated from a plan item. This is what people work on and what the
  calendar shows. It keeps its original due date forever; rescheduling only moves ``due_date``.
* ``DeliverableAsset`` / ``DeliverableEvent``  the files and links delivered, and the timeline of everything done.
"""
from __future__ import annotations

import secrets
from datetime import datetime, time

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import USER, TimeStamped, Versioned

FREQUENCIES = [('weekly', 'Every week'), ('monthly', 'Every month'), ('quarterly', 'Every quarter'),
               ('once', 'One time')]
WEEKDAYS = [(0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'),
            (6, 'Sunday')]
CATEGORIES = ['Poster', 'Reel or video', 'Carousel', 'Story', 'Blog post', 'Ad creative', 'Ad campaign', 'Report',
              'Website update', 'SEO task', 'Email campaign', 'Meeting or call', 'Other']

DEFAULT_DUE_TIME = time(18, 0)
TEAMS = ['Media team', 'Marketing team', 'Design team', 'Video team', 'Content team', 'Ads team', 'Sales team']


class ServiceDeliverable(models.Model):
    """One line of a service's standard monthly plan. Copied onto a client's plan when their contract starts."""
    service = models.ForeignKey('crm.Service', on_delete=models.CASCADE, related_name='deliverable_items')
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=60, blank=True)
    frequency = models.CharField(max_length=10, choices=FREQUENCIES, default='monthly')
    weekdays = models.CharField(max_length=20, blank=True, help_text='Comma list, Monday is 0.')
    month_days = models.CharField(max_length=40, blank=True, help_text='Comma list of days of the month, 1 to 31.')
    due_time = models.TimeField(default=DEFAULT_DUE_TIME)
    quantity = models.PositiveSmallIntegerField(default=1)
    handled_by = models.CharField(max_length=60, blank=True, help_text='The team that produces it, e.g. Media team.')
    requires_review = models.BooleanField(default=False)
    needs_client_approval = models.BooleanField(default=False)
    preferences = models.TextField(blank=True)
    sort = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort', 'id']

    def __str__(self):
        return f'{self.service}: {self.title}'


class DeliveryPlan(TimeStamped, Versioned):
    """A client's monthly promises, tied to one contract version. A renewal or amendment gets a fresh plan that
    starts as a copy of the old one, so no promise is dropped."""
    STATUSES = [('active', 'Active'), ('paused', 'Paused'), ('closed', 'Closed')]
    contract = models.OneToOneField('crm.Contract', on_delete=models.PROTECT, related_name='delivery_plan')
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='delivery_plans')
    account_owner = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_plans_owned',
                                      help_text='Accountable for the client. Gets escalations and client approvals.')
    default_assignee = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+',
                                         help_text='Does the work unless an item names someone else. Empty means the account owner.')
    status = models.CharField(max_length=10, choices=STATUSES, default='active', db_index=True)
    effective_from = models.DateField(help_text='Nothing is created for dates before this.')
    frozen_through = models.DateField(null=True, blank=True,
                                      help_text='Deliverables due on or before this date are locked against plan edits.')
    client_preferences = models.TextField('Client preferences', blank=True,
                                          help_text='Tone, brand colours, things to avoid. Shown on every deliverable.')
    revision = models.PositiveIntegerField(default=0)
    carried_from = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='carried_to')

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f'Plan for {self.client} ({self.contract})'

    @property
    def assignee(self):
        return self.default_assignee or self.account_owner


class PlanItem(TimeStamped):
    """One recurring promise on a client's plan."""
    plan = models.ForeignKey(DeliveryPlan, on_delete=models.CASCADE, related_name='items')
    source = models.ForeignKey(ServiceDeliverable, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=60, blank=True)
    frequency = models.CharField(max_length=10, choices=FREQUENCIES, default='monthly')
    weekdays = models.CharField(max_length=20, blank=True)
    month_days = models.CharField(max_length=40, blank=True)
    due_time = models.TimeField(default=DEFAULT_DUE_TIME)
    quantity = models.PositiveSmallIntegerField(default=1, help_text='Units due on each date, e.g. 3 posters.')
    handled_by = models.CharField(max_length=60, blank=True, help_text='The team that produces it, e.g. Media team.')
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    assignee = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    reviewer = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    requires_review = models.BooleanField(default=False)
    needs_client_approval = models.BooleanField(default=False)
    preferences = models.TextField(blank=True)
    is_paused = models.BooleanField(default=False)
    is_removed = models.BooleanField(default=False)
    removed_reason = models.CharField(max_length=300, blank=True)
    carried_from = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='carried_to',
                                     help_text='The same promise on the previous contract version.')
    sort = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort', 'id']

    def __str__(self):
        return self.title

    @property
    def weekday_list(self) -> list[int]:
        return _ints(self.weekdays, 0, 6)

    @property
    def day_list(self) -> list[int]:
        return _ints(self.month_days, 1, 31)

    @property
    def needs_schedule(self) -> bool:
        if self.frequency == 'weekly':
            return not self.weekday_list
        if self.frequency in ('monthly', 'quarterly'):
            return not self.day_list
        return False

    @property
    def active(self) -> bool:
        return not self.is_removed and not self.is_paused

    @property
    def schedule_text(self) -> str:
        names = dict(WEEKDAYS)
        if self.frequency == 'weekly':
            return 'Every ' + ', '.join(names[d] for d in self.weekday_list) if self.weekday_list else 'Every week, day not set'
        if self.frequency in ('monthly', 'quarterly'):
            days = ', '.join(_ordinal(d) for d in self.day_list)
            every = 'Every month' if self.frequency == 'monthly' else 'Every 3 months'
            return f'{every} on the {days}' if days else f'{every}, day not set'
        return f'Once, {self.start_date:%d %b %Y}'


class PlanRevision(models.Model):
    """A snapshot of the plan each time it changes, so you can see what was promised on any day."""
    plan = models.ForeignKey(DeliveryPlan, on_delete=models.CASCADE, related_name='revisions')
    number = models.PositiveIntegerField()
    summary = models.CharField(max_length=300)
    snapshot = models.JSONField(default=dict)
    changed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-number']
        constraints = [models.UniqueConstraint(fields=['plan', 'number'], name='crm_planrev_unique')]


DELIVERABLE_STATUSES = [
    ('upcoming', 'Upcoming'), ('in_progress', 'In progress'), ('in_review', 'In review'),
    ('awaiting_client', 'Awaiting client'), ('delivered', 'Delivered'), ('skipped', 'Skipped'),
    ('cancelled', 'Cancelled'),
]
OPEN_STATUSES = ('upcoming', 'in_progress', 'in_review', 'awaiting_client')
REVIEW_STATES = [('none', 'Not needed'), ('pending', 'Waiting for review'), ('approved', 'Approved'),
                 ('changes', 'Changes requested')]
CLIENT_STATES = [('none', 'Not needed'), ('needed', 'Not sent yet'), ('awaiting', 'Awaiting client'),
                 ('approved', 'Client approved'), ('changes', 'Client wants changes')]


class Deliverable(TimeStamped, Versioned):
    plan = models.ForeignKey(DeliveryPlan, on_delete=models.PROTECT, related_name='deliverables')
    plan_item = models.ForeignKey(PlanItem, null=True, blank=True, on_delete=models.SET_NULL, related_name='instances')
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='deliverables')
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=60, blank=True)
    period = models.DateField(db_index=True, help_text='First day of the month this was promised for.')
    original_due_date = models.DateField(help_text='What the contract promised. Never changes.')
    due_date = models.DateField(db_index=True, help_text='Where it stands now. Moves when rescheduled.')
    due_time = models.TimeField(default=DEFAULT_DUE_TIME)
    quantity = models.PositiveSmallIntegerField(default=1)
    quantity_done = models.PositiveSmallIntegerField(default=0)
    handled_by = models.CharField(max_length=60, blank=True, help_text='The team that produces it, e.g. Media team.')
    assignee = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_deliverables')
    reviewer = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    status = models.CharField(max_length=16, choices=DELIVERABLE_STATUSES, default='upcoming', db_index=True)

    requires_review = models.BooleanField(default=False)
    review_state = models.CharField(max_length=10, choices=REVIEW_STATES, default='none')
    review_requested_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=500, blank=True)
    review_nudged_at = models.DateTimeField(null=True, blank=True)

    needs_client_approval = models.BooleanField(default=False)
    client_state = models.CharField(max_length=10, choices=CLIENT_STATES, default='none')
    client_sent_at = models.DateTimeField(null=True, blank=True)
    client_answered_at = models.DateTimeField(null=True, blank=True)
    client_revisions = models.PositiveSmallIntegerField(default=0)
    client_nudge_level = models.PositiveSmallIntegerField(default=0)

    preferences = models.TextField(blank=True, help_text='The client standing preferences for this kind of item.')
    instructions = models.TextField(blank=True, help_text='Anything special the client asked for this time.')
    delivery_note = models.CharField('Delivery note', max_length=300, blank=True,
                                     help_text='File name, ad name, report name or link that was delivered.')
    shortfall_reason = models.CharField(max_length=300, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivered_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    reschedule_count = models.PositiveSmallIntegerField(default=0)
    reschedule_reason = models.CharField(max_length=300, blank=True)
    locked = models.BooleanField(default=False, help_text='Edited by a person, so a plan change will not rewrite it.')
    auto_cancelled = models.BooleanField(default=False, help_text='Cancelled by a plan change; revived if the plan brings the date back.')
    reminder_level = models.PositiveSmallIntegerField(default=0, editable=False)
    missed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['due_date', 'due_time', 'id']
        indexes = [models.Index(fields=['assignee', 'status', 'due_date']), models.Index(fields=['client', 'period'])]
        constraints = [
            models.UniqueConstraint(fields=['plan_item', 'original_due_date'], condition=Q(plan_item__isnull=False),
                                    name='crm_deliverable_once_per_date'),
            models.CheckConstraint(check=Q(quantity_done__lte=models.F('quantity')), name='crm_deliverable_qty'),
        ]

    def __str__(self):
        return f'{self.title} for {self.client} on {self.due_date:%d %b %Y}'

    # ---------------------------------------------------------------- derived
    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def due_at(self) -> datetime:
        return timezone.make_aware(datetime.combine(self.due_date, self.due_time))

    @property
    def overdue_hours(self) -> int:
        """Whole hours past the deadline for an open item, otherwise 0."""
        if not self.is_open:
            return 0
        late = timezone.now() - self.due_at
        return max(0, int(late.total_seconds() // 3600))

    @property
    def is_overdue(self) -> bool:
        return self.is_open and timezone.now() > self.due_at

    @property
    def was_late(self) -> bool:
        """Delivered after the date it currently stands at."""
        return bool(self.delivered_at and timezone.localtime(self.delivered_at).date() > self.due_date)

    @property
    def was_late_against_contract(self) -> bool:
        return bool(self.delivered_at and timezone.localtime(self.delivered_at).date() > self.original_due_date)

    @property
    def client_wait_days(self):
        """How long the client has taken (or has been taking) to answer."""
        if not self.client_sent_at:
            return None
        end = self.client_answered_at or timezone.now()
        return max(0, (end - self.client_sent_at).days)

    @property
    def percent(self) -> int:
        return int(self.quantity_done * 100 / self.quantity) if self.quantity else 0

    @property
    def tone(self) -> str:
        if self.status == 'delivered':
            return 'green'
        if self.status in ('skipped', 'cancelled'):
            return 'grey'
        if self.is_overdue:
            return 'red'
        if self.status in ('in_review', 'awaiting_client'):
            return 'amber'
        return 'blue' if self.status == 'in_progress' else 'grey'


class DeliverableAsset(models.Model):
    """What was actually delivered: a file, a link, an ad, a report. Searchable per client in the repository."""
    KINDS = [('file', 'File'), ('link', 'Link'), ('ad', 'Ad'), ('report', 'Report'), ('other', 'Other')]
    deliverable = models.ForeignKey(Deliverable, on_delete=models.CASCADE, related_name='assets')
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='+')
    kind = models.CharField(max_length=8, choices=KINDS, default='file')
    name = models.CharField(max_length=200)
    url = models.URLField(max_length=500, blank=True)
    file = models.FileField(upload_to='deliverables/%Y/%m/', blank=True)
    version = models.PositiveSmallIntegerField(default=1)
    added_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class DeliverableEvent(models.Model):
    """The timeline of one deliverable: status changes, reschedules, notes, reminders and who did what."""
    deliverable = models.ForeignKey(Deliverable, on_delete=models.CASCADE, related_name='events')
    kind = models.CharField(max_length=30, db_index=True)
    text = models.CharField(max_length=500, blank=True)
    actor = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-id']

    @property
    def label(self) -> str:
        return self.kind.replace('_', ' ').capitalize()


class CalendarFeedToken(models.Model):
    """A private address that puts a person's deliverables, tasks and follow-ups on their phone calendar."""
    user = models.OneToOneField(USER, on_delete=models.CASCADE, related_name='calendar_feed')
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    @classmethod
    def for_user(cls, user, *, rotate: bool = False):
        row, made = cls.objects.get_or_create(user=user, defaults={'token': secrets.token_urlsafe(32)})
        if rotate and not made:
            row.token = secrets.token_urlsafe(32)
            row.created_at = timezone.now()
            row.save(update_fields=['token', 'created_at'])
        return row


def _ints(text: str, lo: int, hi: int) -> list[int]:
    out = []
    for part in (text or '').split(','):
        part = part.strip()
        if part.isdigit() and lo <= int(part) <= hi and int(part) not in out:
            out.append(int(part))
    return sorted(out)


def _ordinal(n: int) -> str:
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


HORIZON_DAYS = 30
