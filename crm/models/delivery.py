"""Contracts, AMC, recurring billing (Part 9.4-9.7) and projects, tasks, timesheets, tickets (Part 10)."""
from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone

from crm.core.gst import TAXABILITY_CHOICES

from .base import USER, MoneyField, RateField, TimeStamped, Versioned
from .catalog import BILLING_MODELS

CONTRACT_STATUSES = [
    ('draft', 'Draft'), ('pending_approval', 'Pending approval'), ('active', 'Active'),
    ('suspended', 'Suspended'), ('expired', 'Expired'), ('terminated', 'Terminated'),
    ('completed', 'Completed'), ('superseded', 'Amended (superseded)'),
]


class Contract(TimeStamped, Versioned):
    number = models.CharField(max_length=30, blank=True, db_index=True)
    version = models.PositiveSmallIntegerField(default=1)
    previous = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT, related_name='amendment')
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='contracts')
    quotation = models.ForeignKey('crm.Quotation', null=True, blank=True, on_delete=models.PROTECT, related_name='contracts')
    title = models.CharField(max_length=200)
    owner = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_contracts')
    status = models.CharField(max_length=20, choices=CONTRACT_STATUSES, default='draft', db_index=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    effective_from = models.DateField(null=True, blank=True)
    auto_renew = models.BooleanField(default=False)
    notice_days = models.PositiveSmallIntegerField(default=30)
    invoice_policy = models.CharField(max_length=12, default='draft',
                                      choices=[('draft', 'Create draft for review'), ('auto_issue', 'Auto-issue')])
    payment_terms_days = models.PositiveSmallIntegerField(default=15)
    scope = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    amendment_reason = models.CharField(max_length=300, blank=True)
    suspended_reason = models.CharField(max_length=300, blank=True)
    terminated_on = models.DateField(null=True, blank=True)
    terminated_reason = models.CharField(max_length=300, blank=True)
    renewal_alert_level = models.PositiveSmallIntegerField(default=0, editable=False)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']
        constraints = [
            models.CheckConstraint(check=Q(end_date__isnull=True) | Q(end_date__gte=models.F('start_date')),
                                   name='crm_contract_dates'),
        ]

    def __str__(self):
        return (self.number or f'Contract draft #{self.pk}') + (f' v{self.version}' if self.version > 1 else '')

    @property
    def value(self):
        from crm.core.money import ZERO
        return sum((ln.line_total for ln in self.lines.all()), ZERO)

    @property
    def days_to_end(self):
        from crm.core.india import today
        return (self.end_date - today()).days if self.end_date else None

    @property
    def renewal_due(self) -> bool:
        d = self.days_to_end
        return self.status == 'active' and d is not None and d <= 60


class ContractLine(models.Model):
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name='lines')
    service = models.ForeignKey('crm.Service', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1'))
    unit_price = MoneyField()
    billing_model = models.CharField(max_length=20, choices=BILLING_MODELS, default='monthly')
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    taxability = models.CharField(max_length=20, choices=TAXABILITY_CHOICES, default='taxable')
    tax_rate = RateField(default=Decimal('18.00'))
    is_paused = models.BooleanField(default=False)
    sac_code = models.CharField(max_length=8, blank=True)

    class Meta:
        ordering = ['id']

    @property
    def line_total(self):
        from crm.core.money import q2
        return q2(self.quantity * self.unit_price)

    @property
    def monthly_value(self):
        """MRR contribution (Part 9.7): annual / 12, quarterly / 3; one-time lines contribute 0."""
        from crm.core.money import ZERO, q2
        from .catalog import RECURRING_MODELS
        months = RECURRING_MODELS.get(self.billing_model)
        if not months or self.is_paused:
            return ZERO
        return q2(self.line_total / months)


class BillingSchedule(models.Model):
    STATUSES = [('scheduled', 'Scheduled'), ('generated', 'Generated'), ('sent', 'Sent'),
                ('failed', 'Failed'), ('skipped', 'Skipped (paused)')]
    contract_line = models.ForeignKey(ContractLine, on_delete=models.PROTECT, related_name='schedule')
    period_start = models.DateField()
    period_end = models.DateField()
    invoice_date = models.DateField(db_index=True)
    amount = MoneyField()
    status = models.CharField(max_length=12, choices=STATUSES, default='scheduled', db_index=True)
    error = models.CharField(max_length=500, blank=True)
    milestone = models.ForeignKey('Milestone', null=True, blank=True, on_delete=models.PROTECT, related_name='billing_rows')

    class Meta:
        ordering = ['invoice_date']
        constraints = [models.UniqueConstraint(fields=['contract_line', 'period_start', 'period_end'],
                                               name='crm_schedule_period_unique')]


# ---------------------------------------------------------------- Projects (Part 10)

PROJECT_STATUSES = [('planned', 'Planned'), ('active', 'Active'), ('on_hold', 'On hold'),
                    ('completed', 'Completed'), ('closed', 'Closed'), ('cancelled', 'Cancelled')]


class Project(TimeStamped, Versioned):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200)
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='projects')
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.PROTECT, related_name='projects')
    lead = models.ForeignKey('crm.Lead', null=True, blank=True, on_delete=models.PROTECT, related_name='projects')
    quotation = models.ForeignKey('crm.Quotation', null=True, blank=True, on_delete=models.PROTECT, related_name='projects')
    service = models.ForeignKey('crm.Service', null=True, blank=True, on_delete=models.PROTECT, related_name='projects')
    manager = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_managed_projects')
    status = models.CharField(max_length=12, choices=PROJECT_STATUSES, default='planned', db_index=True)
    start_date = models.DateField()
    due_date = models.DateField()
    budget_hours = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0'))
    budget_amount = MoneyField()
    contract_value = MoneyField()
    scope = models.TextField(blank=True)
    acceptance_criteria = models.TextField(blank=True)
    hold_reason = models.CharField(max_length=300, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    lessons = models.TextField(blank=True)

    class Meta:
        ordering = ['-id']
        constraints = [models.CheckConstraint(check=Q(due_date__gte=models.F('start_date')), name='crm_project_dates')]

    def __str__(self):
        return f'{self.code} {self.name}'

    @property
    def is_at_risk(self) -> bool:
        """Computed (Part 10.1): late, overdue tasks or budget burn above 90 percent."""
        from crm.core.india import today
        if self.status not in ('planned', 'active'):
            return False
        if self.due_date < today():
            return True
        if self.budget_hours and self.hours_logged > self.budget_hours * Decimal('0.9'):
            return True
        return self.tasks.filter(status__in=['todo', 'in_progress', 'blocked'], due_date__lt=today()).exists()

    @property
    def hours_logged(self):
        from django.db.models import Sum
        return self.time_entries.exclude(status='rejected').aggregate(s=Sum('hours'))['s'] or Decimal('0')


class ProjectMember(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_project_memberships')
    role = models.CharField(max_length=60, blank=True)
    allocation_percent = models.PositiveSmallIntegerField(default=100)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    cost_rate = MoneyField(help_text='Loaded cost per hour (cost permission).')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['project', 'user'], name='crm_project_member_unique')]


MILESTONE_STATUSES = [('pending', 'Pending'), ('in_progress', 'In progress'), ('ready_for_review', 'Ready for review'),
                      ('accepted', 'Accepted'), ('rejected', 'Rejected')]


class Milestone(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    name = models.CharField(max_length=150)
    due_date = models.DateField()
    amount = MoneyField(help_text='Billable amount on acceptance (excluding tax).')
    status = models.CharField(max_length=20, choices=MILESTONE_STATUSES, default='pending')
    evidence = models.TextField(blank=True)
    accepted_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    accepted_at = models.DateTimeField(null=True, blank=True)
    rejection_comment = models.CharField(max_length=300, blank=True)
    invoice = models.ForeignKey('crm.Invoice', null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['due_date', 'id']

    def __str__(self):
        return self.name


TASK_STATUSES = [('todo', 'To do'), ('in_progress', 'In progress'), ('blocked', 'Blocked'),
                 ('in_review', 'In review'), ('done', 'Done'), ('cancelled', 'Cancelled')]
TASK_PRIORITIES = [('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('urgent', 'Urgent')]


class Task(TimeStamped, Versioned):
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name='tasks')
    milestone = models.ForeignKey(Milestone, null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    assignee = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_tasks')
    reviewer = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    status = models.CharField(max_length=12, choices=TASK_STATUSES, default='todo', db_index=True)
    priority = models.CharField(max_length=8, choices=TASK_PRIORITIES, default='medium')
    due_date = models.DateField(null=True, blank=True, db_index=True)
    estimate_hours = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal('0'))
    blocked_reason = models.CharField(max_length=300, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    sort = models.PositiveIntegerField(default=0)
    depends_on = models.ManyToManyField('self', symmetrical=False, blank=True, related_name='dependents')

    class Meta:
        ordering = ['sort', 'due_date', 'id']

    def __str__(self):
        return self.title

    @property
    def is_overdue(self):
        from crm.core.india import today
        return self.status not in ('done', 'cancelled') and self.due_date and self.due_date < today()


class TimeEntry(models.Model):
    STATUSES = [('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('rejected', 'Rejected')]
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_time_entries')
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name='time_entries')
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.PROTECT, related_name='time_entries')
    entry_date = models.DateField(db_index=True)
    hours = models.DecimalField(max_digits=4, decimal_places=2)
    billable = models.BooleanField(default=True)
    note = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default='draft', db_index=True)
    approved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    reject_comment = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-entry_date', '-id']
        verbose_name_plural = 'time entries'
        constraints = [models.CheckConstraint(check=Q(hours__gt=0) & Q(hours__lte=24), name='crm_time_hours_range')]


class Blocker(models.Model):
    STATUSES = [('open', 'Open'), ('escalated', 'Escalated'), ('resolved', 'Resolved')]
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='blockers')
    kind = models.CharField(max_length=30, default='dependency',
                            choices=[('dependency', 'Dependency'), ('client', 'Waiting on client'), ('technical', 'Technical'),
                                     ('access', 'Access / credentials'), ('other', 'Other')])
    description = models.CharField(max_length=500)
    owner = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    status = models.CharField(max_length=10, choices=STATUSES, default='open')
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-id']


class ChangeRequest(TimeStamped):
    STATUSES = [('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'),
                ('rejected', 'Rejected'), ('billed', 'Billed')]
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name='change_requests')
    title = models.CharField(max_length=200)
    description = models.TextField()
    effort_hours = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal('0'))
    price = MoneyField()
    tax_rate = RateField(default=Decimal('18.00'))
    client_evidence = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default='draft')
    decided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    invoice = models.ForeignKey('crm.Invoice', null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['-id']


TICKET_STATUSES = [('open', 'Open'), ('in_progress', 'In progress'), ('waiting', 'Waiting for client'),
                   ('resolved', 'Resolved'), ('closed', 'Closed')]


class Ticket(TimeStamped):
    number = models.CharField(max_length=20, unique=True)
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='tickets')
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT, related_name='tickets')
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.PROTECT, related_name='tickets')
    subject = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=8, choices=TASK_PRIORITIES, default='medium')
    assignee = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_tickets')
    status = models.CharField(max_length=12, choices=TICKET_STATUSES, default='open', db_index=True)
    sla_due_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    breach_alerted = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f'{self.number} {self.subject}'

    @property
    def is_breached(self):
        return self.status not in ('resolved', 'closed') and self.sla_due_at and self.sla_due_at < timezone.now()
