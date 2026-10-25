"""Lead capture and the never-lose-a-lead engine (Part 4) and sales pipeline (Part 5.1-5.3).

A Lead record carries the opportunity through its whole life: once qualified it
holds value, probability and expected close date, so it *is* the deal on the
pipeline board. Keeping one record avoids two sources of truth for stage.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from django.db import models
from django.db.models import Q
from django.utils import timezone

from crm.core.india import STATE_CHOICES

from .base import USER, ActiveQuerySet, Archivable, MoneyField, TimeStamped, Versioned

CHANNELS = [
    ('website', 'Website form'), ('meta', 'Meta lead form'), ('google', 'Google Ads'),
    ('phone', 'Phone call'), ('missed_call', 'Missed call'), ('email', 'Email'),
    ('whatsapp', 'WhatsApp'), ('manual', 'Manual entry'), ('csv', 'CSV import'),
    ('referral', 'Referral'), ('existing_client', 'Existing client'), ('other', 'Other'),
]

STAGE_NEW = 'new'
STAGE_CONTACTED = 'contacted'
STAGE_NURTURING = 'nurturing'
STAGE_QUALIFIED = 'qualified'
STAGE_QUOTE_PREPARED = 'quotation_prepared'
STAGE_QUOTE_SENT = 'quotation_sent'
STAGE_NEGOTIATION = 'negotiation'
STAGE_WON = 'won'
STAGE_LOST = 'lost'
STAGES = [
    (STAGE_NEW, 'New'), (STAGE_CONTACTED, 'Contacted'), (STAGE_NURTURING, 'Nurturing'),
    (STAGE_QUALIFIED, 'Qualified'), (STAGE_QUOTE_PREPARED, 'Quotation prepared'),
    (STAGE_QUOTE_SENT, 'Quotation sent'), (STAGE_NEGOTIATION, 'Negotiation'),
    (STAGE_WON, 'Won'), (STAGE_LOST, 'Lost'),
]
STAGE_ORDER = [s for s, _ in STAGES]
OPEN_STAGES = [STAGE_NEW, STAGE_CONTACTED, STAGE_NURTURING, STAGE_QUALIFIED, STAGE_QUOTE_PREPARED,
               STAGE_QUOTE_SENT, STAGE_NEGOTIATION]
CLOSED_STAGES = [STAGE_WON, STAGE_LOST]
PIPELINE_STAGES = [STAGE_NEW, STAGE_CONTACTED, STAGE_QUALIFIED, STAGE_QUOTE_PREPARED, STAGE_QUOTE_SENT,
                   STAGE_NEGOTIATION, STAGE_WON, STAGE_LOST]
DEFAULT_PROBABILITY = {
    STAGE_NEW: 10, STAGE_CONTACTED: 20, STAGE_NURTURING: 15, STAGE_QUALIFIED: 40,
    STAGE_QUOTE_PREPARED: 60, STAGE_QUOTE_SENT: 70, STAGE_NEGOTIATION: 80, STAGE_WON: 100, STAGE_LOST: 0,
}

PRIORITIES = [('hot', 'Hot'), ('warm', 'Warm'), ('cold', 'Cold')]

LOST_REASONS = [
    ('price', 'Price too high'), ('competitor', 'Chose a competitor'), ('no_budget', 'No budget'),
    ('no_response', 'No response'), ('not_ready', 'Not ready / timing'), ('scope', 'Scope mismatch'),
    ('invalid', 'Invalid or wrong number'), ('duplicate', 'Duplicate'), ('other', 'Other'),
]


class LeadSource(TimeStamped):
    name = models.CharField(max_length=120, unique=True)
    channel = models.CharField(max_length=20, choices=CHANNELS, default='website')
    source_key = models.CharField(max_length=48, unique=True, editable=False)
    secret = models.CharField(max_length=64, editable=False,
                              help_text='HMAC secret for signed inbound requests.')
    is_active = models.BooleanField(default=True)
    is_paused = models.BooleanField(default=False)
    expected_min_per_day = models.PositiveIntegerField(default=0,
                                                       help_text='0 disables the silent-source alert.')
    silent_window_hours = models.PositiveIntegerField(default=24)
    is_silent = models.BooleanField(default=False, editable=False)
    silent_alerted_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_lead_at = models.DateTimeField(null=True, blank=True, editable=False)
    field_mapping = models.JSONField(default=dict, blank=True,
                                     help_text='Map payload keys to lead fields, e.g. {"full_name": "name"}.')
    default_owner = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    monthly_cost = MoneyField(help_text='Used for cost per lead.')
    consent_basis = models.CharField(max_length=40, default='consent',
                                     choices=[('consent', 'Consent'), ('legitimate_use', 'Legitimate use')])

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.source_key:
            self.source_key = secrets.token_urlsafe(24)
        if not self.secret:
            self.secret = secrets.token_hex(32)
        super().save(*args, **kwargs)

    def rotate_key(self):
        self.source_key = secrets.token_urlsafe(24)
        self.secret = secrets.token_hex(32)


class Campaign(TimeStamped):
    name = models.CharField(max_length=150)
    source = models.ForeignKey(LeadSource, on_delete=models.PROTECT, related_name='campaigns')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    budget = MoneyField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=['source', 'name'], name='crm_campaign_unique')]

    def __str__(self):
        return f'{self.name} ({self.source})'


class LeadTag(models.Model):
    name = models.CharField(max_length=40, unique=True)
    color = models.CharField(max_length=10, default='grey',
                             choices=[(c, c.title()) for c in ('grey', 'blue', 'amber', 'green', 'red', 'purple')])

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class LeadInbox(models.Model):
    """Store first, process second (Part 1.2 #2). Raw payload is kept before any parsing."""
    STATUSES = [
        ('received', 'Received'), ('processed', 'Processed'), ('duplicate', 'Attached to existing lead'),
        ('failed', 'Failed (will retry)'), ('review', 'Needs manual review'), ('resolved', 'Resolved'),
        ('ignored', 'Ignored'),
    ]
    source = models.ForeignKey(LeadSource, on_delete=models.PROTECT, related_name='inbox')
    external_id = models.CharField(max_length=120, blank=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    content_type = models.CharField(max_length=100, blank=True)
    raw_body = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    headers = models.JSONField(default=dict, blank=True)
    remote_ip = models.GenericIPAddressField(null=True, blank=True)
    signature_valid = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=STATUSES, default='received', db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True)
    lead = models.ForeignKey('Lead', null=True, blank=True, on_delete=models.PROTECT, related_name='inbox_items')
    processed_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    resolution_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-id']
        verbose_name_plural = 'lead inbox'
        constraints = [
            models.UniqueConstraint(fields=['source', 'external_id'], condition=~Q(external_id=''),
                                    name='crm_inbox_source_external_unique'),
        ]

    def __str__(self):
        return f'Inbox #{self.pk} from {self.source}'


class Lead(TimeStamped, Versioned, Archivable):
    name = models.CharField(max_length=100)
    company = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True, db_index=True)
    alt_phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True, db_index=True)
    website = models.URLField(blank=True)
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=2, choices=STATE_CHOICES, blank=True)
    services = models.ManyToManyField('crm.Service', blank=True, related_name='leads')
    budget_min = MoneyField(null=True, blank=True, default=None)
    budget_max = MoneyField(null=True, blank=True, default=None)
    expected_close_date = models.DateField(null=True, blank=True)
    decision_maker = models.CharField(max_length=120, blank=True)
    source = models.ForeignKey(LeadSource, on_delete=models.PROTECT, related_name='leads')
    campaign = models.ForeignKey(Campaign, null=True, blank=True, on_delete=models.PROTECT, related_name='leads')
    referrer = models.CharField(max_length=150, blank=True)
    owner = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_leads')
    team = models.ForeignKey('crm.Team', null=True, blank=True, on_delete=models.PROTECT, related_name='leads')
    priority = models.CharField(max_length=5, choices=PRIORITIES, default='warm', db_index=True)
    stage = models.CharField(max_length=20, choices=STAGES, default=STAGE_NEW, db_index=True)
    stage_changed_at = models.DateTimeField(default=timezone.now)
    next_follow_up_at = models.DateTimeField(null=True, blank=True, db_index=True)
    next_action = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    tags = models.ManyToManyField(LeadTag, blank=True, related_name='leads')

    # Deal facts (Part 5.1)
    value = MoneyField(help_text='Expected deal value excluding tax.')
    probability = models.PositiveSmallIntegerField(default=10)
    won_value = MoneyField(null=True, blank=True, default=None)
    won_at = models.DateTimeField(null=True, blank=True)
    expected_start = models.DateField(null=True, blank=True)
    lost_reason = models.CharField(max_length=20, choices=LOST_REASONS, blank=True)
    lost_competitor = models.CharField(max_length=120, blank=True)
    lost_price = MoneyField(null=True, blank=True, default=None)
    lost_notes = models.TextField(blank=True)
    lost_at = models.DateTimeField(null=True, blank=True)
    win_back_date = models.DateField(null=True, blank=True)
    client = models.ForeignKey('crm.Client', null=True, blank=True, on_delete=models.PROTECT, related_name='leads')

    # SLA and activity tracking
    assigned_at = models.DateTimeField(null=True, blank=True)
    first_contacted_at = models.DateTimeField(null=True, blank=True)
    first_response_seconds = models.PositiveIntegerField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True, db_index=True)
    postpone_count = models.PositiveSmallIntegerField(default=0)
    unassigned_alert_level = models.PositiveSmallIntegerField(default=0, editable=False)
    stale_alert_level = models.PositiveSmallIntegerField(default=0, editable=False)
    aging_alerted_stage = models.CharField(max_length=20, blank=True, editable=False)
    is_invalid = models.BooleanField(default=False)
    merged_into = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='merged_leads')
    flagged_for_merge = models.BooleanField(default=False)
    duplicate_override_reason = models.CharField(max_length=300, blank=True)
    import_batch = models.ForeignKey('crm.ImportBatch', null=True, blank=True, on_delete=models.PROTECT, related_name='leads')
    consent_basis = models.CharField(max_length=40, blank=True)

    objects = ActiveQuerySet.as_manager()

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['owner', 'stage']),
            models.Index(fields=['stage', 'next_follow_up_at']),
        ]
        constraints = [
            # Guarantee 5: every open lead has a dated next action.
            models.CheckConstraint(
                check=Q(stage__in=CLOSED_STAGES) | Q(archived_at__isnull=False) | Q(next_follow_up_at__isnull=False),
                name='crm_open_lead_has_next_action'),
            # Guarantee 4: an owner once past ingest (only New may be unassigned).
            models.CheckConstraint(
                check=Q(stage=STAGE_NEW) | Q(archived_at__isnull=False) | Q(owner__isnull=False),
                name='crm_lead_has_owner_after_new'),
            models.CheckConstraint(check=Q(probability__lte=100), name='crm_lead_probability_range'),
            models.CheckConstraint(check=Q(phone__gt='') | Q(email__gt=''), name='crm_lead_phone_or_email'),
        ]

    def __str__(self):
        return self.name + (f' ({self.company})' if self.company else '')

    @property
    def is_open(self) -> bool:
        return self.stage in OPEN_STAGES and not self.archived_at

    @property
    def is_overdue(self) -> bool:
        return bool(self.is_open and self.next_follow_up_at and self.next_follow_up_at < timezone.now())

    @property
    def weighted_value(self):
        from crm.core.money import q2
        return q2(self.value * self.probability / 100)

    @property
    def age_days(self) -> int:
        return (timezone.now() - self.created_at).days

    @property
    def days_in_stage(self) -> int:
        return (timezone.now() - self.stage_changed_at).days


class LeadActivity(models.Model):
    """Timeline entry (Part 4.6). Calls, meetings, notes, stage changes, system events."""
    KINDS = [
        ('call', 'Call'), ('meeting', 'Meeting'), ('note', 'Note'), ('email', 'Email'), ('whatsapp', 'WhatsApp'),
        ('stage', 'Stage change'), ('assignment', 'Assignment'), ('follow_up', 'Follow-up'),
        ('quotation', 'Quotation'), ('payment', 'Payment'), ('inbound', 'Inbound enquiry'),
        ('system', 'System'),
    ]
    CALL_OUTCOMES = [('connected', 'Connected'), ('no_answer', 'No answer'), ('busy', 'Busy'),
                     ('wrong_number', 'Wrong number'), ('call_back', 'Call back')]
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name='activities')
    kind = models.CharField(max_length=12, choices=KINDS, db_index=True)
    direction = models.CharField(max_length=3, blank=True, choices=[('in', 'Incoming'), ('out', 'Outgoing')])
    outcome = models.CharField(max_length=20, blank=True)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    summary = models.TextField(blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    attendees = models.CharField(max_length=300, blank=True)
    visibility = models.CharField(max_length=10, default='team', choices=[('team', 'Team'), ('private', 'Private to manager')])
    actor = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    source_label = models.CharField(max_length=40, default='web')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-occurred_at', '-id']

    @property
    def editable_until(self):
        return self.created_at + timedelta(minutes=10)


class FollowUp(models.Model):
    KINDS = [('call', 'Call'), ('meeting', 'Meeting'), ('email', 'Email'), ('whatsapp', 'WhatsApp')]
    STATUSES = [('pending', 'Pending'), ('done', 'Done'), ('cancelled', 'Cancelled')]
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name='follow_ups')
    kind = models.CharField(max_length=10, choices=KINDS, default='call')
    due_at = models.DateTimeField(db_index=True)
    assignee = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_follow_ups')
    purpose = models.CharField(max_length=200, blank=True)
    reminder_minutes = models.PositiveIntegerField(default=15)
    status = models.CharField(max_length=10, choices=STATUSES, default='pending', db_index=True)
    outcome = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    postpone_count = models.PositiveSmallIntegerField(default=0)
    reminded_at = models.DateTimeField(null=True, blank=True)
    escalation_level = models.PositiveSmallIntegerField(default=0)
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['due_at']
        indexes = [models.Index(fields=['assignee', 'status', 'due_at'])]

    def __str__(self):
        return f'{self.get_kind_display()} with {self.lead} at {self.due_at:%d-%b-%Y %H:%M}'

    @property
    def is_overdue(self) -> bool:
        return self.status == 'pending' and self.due_at < timezone.now()


class StageHistory(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name='stage_history')
    from_stage = models.CharField(max_length=20, blank=True)
    to_stage = models.CharField(max_length=20)
    actor = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    reason = models.CharField(max_length=500, blank=True)
    seconds_in_previous = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-id']
        verbose_name_plural = 'stage history'


class LeadAssignment(models.Model):
    RULES = [('manual', 'Manual'), ('round_robin', 'Round-robin'), ('source_default', 'Source default'),
             ('import', 'Import'), ('offboarding', 'Offboarding'), ('creator', 'Creator')]
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name='assignments')
    from_user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    to_user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    rule = models.CharField(max_length=20, choices=RULES, default='manual')
    reason = models.CharField(max_length=300, blank=True)
    actor = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    undo_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']


class DuplicateCandidate(models.Model):
    STATUSES = [('open', 'Open'), ('merged', 'Merged'), ('not_duplicate', 'Not a duplicate'), ('later', 'Review later')]
    lead_a = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name='+')
    lead_b = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name='+')
    reason = models.CharField(max_length=120)
    score = models.PositiveSmallIntegerField(default=50)
    status = models.CharField(max_length=15, choices=STATUSES, default='open', db_index=True)
    decided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-score', '-id']
        constraints = [models.UniqueConstraint(fields=['lead_a', 'lead_b'], name='crm_duplicate_pair_unique')]


class SalesTarget(TimeStamped):
    """Effective-dated monthly targets per user or team (Part 5.3)."""
    month = models.DateField(help_text='First day of the month.')
    user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_targets')
    team = models.ForeignKey('crm.Team', null=True, blank=True, on_delete=models.PROTECT, related_name='targets')
    lead_count = models.PositiveIntegerField(default=0)
    quotation_count = models.PositiveIntegerField(default=0)
    won_value = MoneyField()
    collected_value = MoneyField()

    class Meta:
        ordering = ['-month']
        constraints = [
            models.UniqueConstraint(fields=['month', 'user'], condition=Q(user__isnull=False), name='crm_target_user_month'),
            models.UniqueConstraint(fields=['month', 'team'], condition=Q(team__isnull=False), name='crm_target_team_month'),
            models.CheckConstraint(check=Q(user__isnull=False) | Q(team__isnull=False), name='crm_target_has_subject'),
        ]
