"""AI business staff for the owner: daily snapshots, monthly goals, uploaded business plans, stored briefings and
the actions the analyst suggests (which only happen after the owner approves them)."""
from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import USER, MoneyField, TimeStamped
from .core import private_storage

METRIC_CHOICES = [
    ('revenue_collected', 'Revenue collected (INR)'),
    ('revenue_invoiced', 'Invoiced, before tax (INR)'),
    ('won_value', 'Deals won, value (INR)'),
    ('new_leads', 'New leads'),
    ('new_clients', 'New clients onboarded'),
    ('deliverables_delivered', 'Deliverables delivered'),
    ('win_rate', 'Win rate (%)'),
]
MONEY_METRICS = ('revenue_collected', 'revenue_invoiced', 'won_value')


class DailySnapshot(models.Model):
    """One row per day: the day's numbers, the team board and the business position at the end of it. Stored so
    yesterday and last week can be compared exactly, even for figures that cannot be rebuilt later (open pipeline)."""
    date = models.DateField(unique=True)
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f'Snapshot {self.date:%d %b %Y}'


class BusinessGoal(TimeStamped):
    """A company target for one month. Entered by hand or confirmed from an uploaded business plan."""
    SOURCES = [('manual', 'Entered by hand'), ('plan', 'From the business plan')]
    metric = models.CharField(max_length=30, choices=METRIC_CHOICES)
    month = models.DateField(help_text='First day of the month.')
    target = MoneyField()
    note = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=10, choices=SOURCES, default='manual')
    plan = models.ForeignKey('crm.BusinessPlan', null=True, blank=True, on_delete=models.SET_NULL, related_name='goals')

    class Meta:
        ordering = ['-month', 'metric']
        constraints = [
            models.UniqueConstraint(fields=['metric', 'month'], name='crm_goal_metric_month'),
            models.CheckConstraint(check=Q(target__gt=0), name='crm_goal_target_positive'),
        ]

    def __str__(self):
        return f'{self.get_metric_display()} {self.month:%b %Y}'


class BusinessPlan(TimeStamped):
    """An uploaded business plan or financial document. The text is extracted once; the owner reviews the goals the
    assistant proposes before anything is saved, so a misread number can never change the reports."""
    STATUSES = [('uploaded', 'Uploaded'), ('proposed', 'Goals proposed, waiting for review'), ('confirmed', 'Reviewed')]
    title = models.CharField(max_length=200)
    file = models.FileField(upload_to='desk/plans/%Y/%m', storage=private_storage, max_length=300, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    extracted_text = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    proposed = models.JSONField(default=list, blank=True, help_text='Goals the assistant found: metric, target, note.')
    proposed_by = models.CharField(max_length=60, blank=True, help_text='Model name, or "rules" when no AI was used.')
    context_notes = models.TextField(blank=True, help_text='Priorities and strategy in the owner\'s own words.')
    status = models.CharField(max_length=10, choices=STATUSES, default='uploaded')
    is_active = models.BooleanField(default=False)

    class Meta:
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=['is_active'], condition=Q(is_active=True), name='crm_one_active_plan')]

    def __str__(self):
        return self.title


class DeskBriefing(models.Model):
    """A written briefing kept so it can be reopened without another AI call."""
    KINDS = [('morning', 'Morning'), ('evening', 'Evening'), ('manual', 'On request')]
    kind = models.CharField(max_length=8, choices=KINDS)
    for_date = models.DateField(db_index=True)
    text = models.TextField()
    engine = models.CharField(max_length=60, blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['kind', 'for_date'], condition=~Q(kind='manual'),
                                               name='crm_briefing_once_a_day')]

    def __str__(self):
        return f'{self.get_kind_display()} briefing {self.for_date:%d %b %Y}'


class DeskAction(models.Model):
    """Something the assistant suggests doing. Nothing is created until the owner presses Approve."""
    KINDS = [('follow_up', 'Schedule a lead follow-up'), ('nudge', 'Ask someone to act')]
    STATUSES = [('suggested', 'Suggested'), ('approved', 'Approved'), ('dismissed', 'Dismissed'), ('failed', 'Failed')]
    kind = models.CharField(max_length=10, choices=KINDS)
    title = models.CharField(max_length=200)
    detail = models.CharField(max_length=500, blank=True)
    area = models.CharField(max_length=20, blank=True, help_text='sales, delivery, money, customers')
    assignee = models.ForeignKey(USER, null=True, blank=True, on_delete=models.CASCADE, related_name='crm_desk_actions')
    lead = models.ForeignKey('crm.Lead', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    url = models.CharField(max_length=300, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    for_date = models.DateField(db_index=True)
    source_key = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=10, choices=STATUSES, default='suggested', db_index=True)
    result = models.CharField(max_length=300, blank=True)
    decided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-for_date', 'id']

    def __str__(self):
        return self.title
