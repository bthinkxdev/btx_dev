"""Quotations (Part 5.4-5.9). Each row is one version; versions of a quotation share `family`."""
from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import USER, MoneyField, RateField, TimeStamped, Versioned
from .core import private_storage
from .documents import TaxDocument, TaxLine

Q_DRAFT = 'draft'
Q_AWAITING = 'awaiting_approval'
Q_APPROVED = 'approved'
Q_SENT = 'sent'
Q_VIEWED = 'viewed'
Q_ACCEPTED = 'accepted'
Q_REJECTED = 'rejected'
Q_EXPIRED = 'expired'
Q_SUPERSEDED = 'superseded'
Q_CONVERTED = 'converted'
Q_VOID = 'void'
QUOTATION_STATUSES = [
    (Q_DRAFT, 'Draft'), (Q_AWAITING, 'Awaiting approval'), (Q_APPROVED, 'Approved'), (Q_SENT, 'Sent'),
    (Q_VIEWED, 'Viewed'), (Q_ACCEPTED, 'Accepted'), (Q_REJECTED, 'Rejected'), (Q_EXPIRED, 'Expired'),
    (Q_SUPERSEDED, 'Superseded'), (Q_CONVERTED, 'Converted'), (Q_VOID, 'Void'),
]
Q_EDITABLE = [Q_DRAFT]
Q_LIVE = [Q_SENT, Q_VIEWED]                     # with the customer, awaiting decision
Q_COLLECTABLE = [Q_APPROVED, Q_SENT, Q_VIEWED, Q_ACCEPTED]   # payment links allowed (Part 6.2)

REJECTION_REASONS = [('price', 'Price too high'), ('competitor', 'Chose a competitor'), ('scope', 'Scope mismatch'),
                     ('timing', 'Not the right time'), ('no_budget', 'No budget'), ('other', 'Other')]


class Quotation(TaxDocument, TimeStamped, Versioned):
    family = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='versions',
                               help_text='The first version; null on the first version itself.')
    version = models.PositiveSmallIntegerField(default=1)
    number = models.CharField(max_length=30, blank=True, db_index=True,
                              help_text='Allocated at approval/send, shared by all versions.')
    status = models.CharField(max_length=20, choices=[(s, label) for s, label in QUOTATION_STATUSES], default=Q_DRAFT, db_index=True)

    lead = models.ForeignKey('crm.Lead', null=True, blank=True, on_delete=models.PROTECT, related_name='quotations')
    client = models.ForeignKey('crm.Client', null=True, blank=True, on_delete=models.PROTECT, related_name='quotations')
    contact_name = models.CharField(max_length=120, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    owner = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_quotations')
    quotation_date = models.DateField()
    valid_until = models.DateField()
    title = models.CharField(max_length=200, blank=True)
    reference = models.CharField(max_length=100, blank=True)

    advance_percent = RateField()
    balance_terms = models.CharField(max_length=300, blank=True)
    payment_terms_days = models.PositiveSmallIntegerField(default=15)
    late_fee_text = models.CharField(max_length=300, blank=True)
    deliverables = models.TextField(blank=True)
    exclusions = models.TextField(blank=True)
    assumptions = models.TextField(blank=True)
    timeline = models.CharField(max_length=300, blank=True)
    revision_limits = models.CharField(max_length=200, blank=True)
    terms = models.TextField(blank=True)
    terms_modified = models.BooleanField(default=False)
    signatory_name = models.CharField(max_length=120, blank=True)
    internal_notes = models.TextField(blank=True)
    cost_estimate = MoneyField(null=True, blank=True, default=None)
    advance_amount = MoneyField(editable=False)
    approval_reasons = models.JSONField(default=list, blank=True, editable=False)

    issued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_to = models.CharField(max_length=300, blank=True)
    first_viewed_at = models.DateTimeField(null=True, blank=True)
    last_viewed_at = models.DateTimeField(null=True, blank=True)
    view_count = models.PositiveIntegerField(default=0)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by_name = models.CharField(max_length=120, blank=True)
    acceptance_evidence = models.CharField(max_length=30, blank=True)
    acceptance_note = models.TextField(blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=20, choices=REJECTION_REASONS, blank=True)
    rejection_notes = models.TextField(blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=300, blank=True)
    converted_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    hot_flagged_at = models.DateTimeField(null=True, blank=True)

    share_nonce = models.CharField(max_length=32, blank=True, editable=False)
    share_token_hash = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    pdf_file = models.FileField(upload_to='quotations/%Y/%m', storage=private_storage, blank=True,
                                max_length=300, editable=False)
    pdf_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    content_hash = models.CharField(max_length=64, blank=True, editable=False,
                                    help_text='Hash of the sent version; proves immutability.')

    class Meta:
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=['family', 'version'], condition=Q(family__isnull=False),
                                    name='crm_quotation_family_version'),
            models.UniqueConstraint(fields=['number', 'version'], condition=~Q(number=''),
                                    name='crm_quotation_number_version'),
            models.CheckConstraint(check=Q(lead__isnull=False) | Q(client__isnull=False),
                                   name='crm_quotation_lead_or_client'),
        ]

    def __str__(self):
        return self.display_number

    @property
    def root(self):
        return self.family or self

    @property
    def display_number(self) -> str:
        if self.number:
            return self.number + (f' v{self.version}' if self.version > 1 else '')
        return f'Draft #{self.pk}' + (f' v{self.version}' if self.version > 1 else '')

    @property
    def customer_display(self) -> str:
        if self.client_id:
            return str(self.client)
        if self.lead_id:
            return self.lead.company or self.lead.name
        return self.customer_name

    @property
    def is_editable(self) -> bool:
        return self.status in Q_EDITABLE

    @property
    def is_expired_by_date(self) -> bool:
        from crm.core.india import today
        return self.valid_until < today()

    @property
    def days_to_expiry(self) -> int:
        from crm.core.india import today
        return (self.valid_until - today()).days

    def all_versions(self):
        root = self.root
        return Quotation.objects.filter(Q(pk=root.pk) | Q(family=root)).order_by('version')

    @property
    def margin_percent(self):
        if self.cost_estimate is None or not self.taxable_total:
            return None
        from crm.core.money import q2
        return q2((self.taxable_total - self.cost_estimate) * 100 / self.taxable_total)

    @property
    def collected_amount(self):
        from django.db.models import Sum
        from crm.core.money import ZERO
        return self.payment_links.aggregate(s=Sum('amount_paid'))['s'] or ZERO


class QuotationLine(TaxLine):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='lines')

    class Meta(TaxLine.Meta):
        pass


class QuotationMilestone(models.Model):
    TRIGGERS = [('on_acceptance', 'On acceptance'), ('on_start', 'On project start'),
                ('on_milestone', 'On milestone completion'), ('on_delivery', 'On delivery'), ('on_date', 'On a date')]
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='milestones')
    sort = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=120)
    percent = RateField()
    trigger = models.CharField(max_length=20, choices=TRIGGERS, default='on_acceptance')
    due_rule = models.CharField(max_length=120, blank=True, help_text='For example "Within 7 days of trigger".')

    class Meta:
        ordering = ['sort', 'id']


class QuotationView(models.Model):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='views')
    viewed_at = models.DateTimeField(default=timezone.now)
    ip_hash = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-id']
