"""Payment links, attempts, gateway events, payments, allocations, refunds, TDS receivable (Part 6, 7.6, 7.9)."""
from __future__ import annotations

from datetime import timedelta

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import USER, MoneyField, TimeStamped, Versioned

PL_DRAFT = 'draft'
PL_PENDING = 'pending_approval'
PL_ACTIVE = 'active'
PL_PARTIAL = 'partially_paid'
PL_PAID = 'paid'
PL_EXPIRED = 'expired'
PL_CANCELLED = 'cancelled'
PL_REFUNDED = 'refunded'
PL_PART_REFUNDED = 'partially_refunded'
LINK_STATUSES = [
    (PL_DRAFT, 'Draft'), (PL_PENDING, 'Pending approval'), (PL_ACTIVE, 'Active'),
    (PL_PARTIAL, 'Partially paid'), (PL_PAID, 'Paid'), (PL_EXPIRED, 'Expired'),
    (PL_CANCELLED, 'Cancelled'), (PL_REFUNDED, 'Refunded'), (PL_PART_REFUNDED, 'Partially refunded'),
]
PL_PAYABLE = [PL_ACTIVE, PL_PARTIAL]

PURPOSES = [('full', 'Full balance'), ('advance', 'Advance percent'), ('milestone', 'Milestone'),
            ('partial', 'Custom partial amount')]
METHODS = [('upi', 'UPI'), ('card', 'Card'), ('netbanking', 'Netbanking'), ('wallet', 'Wallet'),
           ('bank_transfer', 'Bank transfer'), ('cheque', 'Cheque'), ('cash', 'Cash'), ('other', 'Other')]
GATEWAY_METHODS = ['upi', 'card', 'netbanking', 'wallet']


class PaymentLink(TimeStamped, Versioned):
    code = models.CharField('Link ID', max_length=20, unique=True)
    token_nonce = models.CharField(max_length=32, editable=False)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    quotation = models.ForeignKey('crm.Quotation', null=True, blank=True, on_delete=models.PROTECT, related_name='payment_links')
    invoice = models.ForeignKey('crm.Invoice', null=True, blank=True, on_delete=models.PROTECT, related_name='payment_links')
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='payment_links')
    owner = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_payment_links')
    contact_name = models.CharField(max_length=120, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    purpose = models.CharField(max_length=12, choices=PURPOSES, default='full')
    milestone_name = models.CharField(max_length=120, blank=True)
    amount = MoneyField()
    price_mode = models.CharField(max_length=12, default='inclusive', editable=False)
    tax_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    gst_overridden = models.BooleanField(default=False)
    description = models.CharField('Customer-visible description', max_length=200)
    expires_at = models.DateTimeField()
    allow_partial = models.BooleanField('Allow partial payments by customer', default=False)
    min_partial_amount = MoneyField(null=True, blank=True, default=None)
    methods = models.JSONField(default=list, blank=True)
    internal_note = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=LINK_STATUSES, default=PL_DRAFT, db_index=True)
    amount_paid = MoneyField(editable=False)
    amount_refunded = MoneyField(editable=False)
    open_count = models.PositiveIntegerField(default=0, editable=False)
    first_opened_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_opened_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_event = models.CharField(max_length=120, blank=True, editable=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    abandoned_alerted_at = models.DateTimeField(null=True, blank=True, editable=False)
    provider = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ['-id']
        constraints = [
            models.CheckConstraint(check=Q(amount__gt=0), name='crm_link_amount_positive'),
            models.CheckConstraint(check=Q(amount_paid__lte=models.F('amount')),
                                   name='crm_link_paid_not_above_amount'),
            models.CheckConstraint(check=Q(quotation__isnull=False) | Q(invoice__isnull=False),
                                   name='crm_link_has_source'),
        ]

    def __str__(self):
        return self.code

    @property
    def balance(self):
        return max(self.amount - self.amount_paid, 0)

    @property
    def source_label(self) -> str:
        if self.invoice_id:
            return self.invoice.display_number
        if self.quotation_id:
            return self.quotation.display_number
        return ''

    @property
    def is_payable(self) -> bool:
        return self.status in PL_PAYABLE and self.expires_at > timezone.now()

    @property
    def expires_soon(self) -> bool:
        return self.status in PL_PAYABLE and self.expires_at - timezone.now() < timedelta(days=2)


class PaymentAttempt(models.Model):
    STATUSES = [('initiated', 'Initiated'), ('pending', 'Pending at gateway'), ('authorised', 'Authorised'),
                ('captured', 'Captured'), ('failed', 'Failed'), ('abandoned', 'Abandoned')]
    link = models.ForeignKey(PaymentLink, on_delete=models.PROTECT, related_name='attempts')
    gateway_order_id = models.CharField(max_length=80, unique=True)
    amount = MoneyField()
    status = models.CharField(max_length=12, choices=STATUSES, default='initiated', db_index=True)
    method = models.CharField(max_length=20, blank=True)
    gateway_payment_id = models.CharField(max_length=80, blank=True)
    session_id = models.CharField(max_length=300, blank=True)
    failure_code = models.CharField(max_length=80, blank=True)
    failure_reason = models.CharField(max_length=300, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-id']


class GatewayEvent(models.Model):
    """Inbox for gateway webhooks: stored before processing, unique per provider event (Part 6.8)."""
    STATUSES = [('received', 'Received'), ('processed', 'Processed'), ('duplicate', 'Duplicate'),
                ('ignored', 'Ignored'), ('failed', 'Failed'), ('review', 'Review')]
    provider = models.CharField(max_length=20)
    event_id = models.CharField(max_length=160)
    event_type = models.CharField(max_length=60, blank=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    raw_body = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    signature_valid = models.BooleanField(default=False)
    source = models.CharField(max_length=12, default='webhook', choices=[('webhook', 'Webhook'), ('backstop', 'Backstop poll'), ('return', 'Return URL')])
    status = models.CharField(max_length=12, choices=STATUSES, default='received', db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)
    note = models.CharField(max_length=300, blank=True)
    link = models.ForeignKey(PaymentLink, null=True, blank=True, on_delete=models.PROTECT, related_name='gateway_events')
    payment = models.ForeignKey('Payment', null=True, blank=True, on_delete=models.PROTECT, related_name='gateway_events')

    class Meta:
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=['provider', 'event_id'], name='crm_gateway_event_unique')]


P_PENDING = 'pending_verification'
P_VERIFIED = 'verified'
P_REJECTED = 'rejected'
P_REVIEW = 'review'
P_REFUNDED = 'refunded'
P_PART_REFUNDED = 'partially_refunded'
PAYMENT_STATUSES = [(P_PENDING, 'Pending verification'), (P_VERIFIED, 'Verified'), (P_REJECTED, 'Rejected'),
                    (P_REVIEW, 'Review'), (P_REFUNDED, 'Refunded'), (P_PART_REFUNDED, 'Partially refunded')]
P_COUNTED = [P_VERIFIED, P_PART_REFUNDED, P_REFUNDED]


class Payment(TimeStamped, Versioned):
    """A verified receipt of money, once per gateway transaction or manual proof (glossary)."""
    receipt_number = models.CharField(max_length=30, blank=True, db_index=True)
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='payments')
    link = models.ForeignKey(PaymentLink, null=True, blank=True, on_delete=models.PROTECT, related_name='payments')
    attempt = models.OneToOneField(PaymentAttempt, null=True, blank=True, on_delete=models.PROTECT, related_name='payment')
    invoice = models.ForeignKey('crm.Invoice', null=True, blank=True, on_delete=models.PROTECT, related_name='direct_payments',
                                help_text='Invoice this payment was recorded against (allocation decides the effect).')
    quotation = models.ForeignKey('crm.Quotation', null=True, blank=True, on_delete=models.PROTECT, related_name='payments')
    kind = models.CharField(max_length=10, choices=[('gateway', 'Gateway'), ('manual', 'Manual')], default='manual')
    method = models.CharField(max_length=20, choices=METHODS, default='bank_transfer')
    amount = MoneyField(help_text='Money actually received.')
    tds_amount = MoneyField('TDS deducted by customer')
    tds_section = models.CharField(max_length=10, blank=True)
    deductor_tan = models.CharField('Deductor TAN', max_length=10, blank=True)
    fee = MoneyField('Gateway fee')
    fee_tax = MoneyField('GST on gateway fee')
    payment_date = models.DateField()
    provider = models.CharField(max_length=20, blank=True)
    provider_payment_id = models.CharField(max_length=80, blank=True)
    reference_number = models.CharField(max_length=80, blank=True)
    deposit_account = models.ForeignKey('crm.BankAccount', null=True, blank=True, on_delete=models.PROTECT, related_name='payments')
    status = models.CharField(max_length=24, choices=PAYMENT_STATUSES, default=P_PENDING, db_index=True)
    submitted_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    verified_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    verified_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.CharField(max_length=300, blank=True)
    review_reason = models.CharField(max_length=300, blank=True)
    internal_note = models.CharField(max_length=500, blank=True)
    allocated_amount = MoneyField(editable=False, help_text='Cash + TDS allocated to invoices.')
    refunded_amount = MoneyField(editable=False)
    settlement = models.ForeignKey('Settlement', null=True, blank=True, on_delete=models.PROTECT, related_name='payments')

    class Meta:
        ordering = ['-payment_date', '-id']
        constraints = [
            models.CheckConstraint(check=Q(amount__gt=0), name='crm_payment_amount_positive'),
            models.UniqueConstraint(fields=['provider', 'provider_payment_id'], condition=~Q(provider_payment_id=''),
                                    name='crm_payment_provider_unique'),
            models.UniqueConstraint(fields=['deposit_account', 'reference_number'],
                                    condition=~Q(reference_number='') & ~Q(status='rejected'),
                                    name='crm_payment_reference_unique'),
            models.UniqueConstraint(fields=['receipt_number'], condition=~Q(receipt_number=''),
                                    name='crm_payment_receipt_unique'),
        ]

    def __str__(self):
        return self.receipt_number or f'Payment #{self.pk}'

    @property
    def gross_settled(self):
        """What this payment clears against invoices: cash received plus TDS withheld."""
        return self.amount + self.tds_amount

    @property
    def unallocated(self):
        return self.gross_settled - self.allocated_amount

    @property
    def refundable(self):
        return self.amount - self.refunded_amount


class PaymentAllocation(models.Model):
    """Assignment of part of a payment to an invoice; reversible only by an approved reversal (Part 7.9)."""
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='allocations')
    invoice = models.ForeignKey('crm.Invoice', on_delete=models.PROTECT, related_name='allocations')
    amount = MoneyField(help_text='Cash portion.')
    tds_amount = MoneyField(help_text='TDS portion.')
    is_advance_adjustment = models.BooleanField(default=False,
                                                help_text='Allocated after receipt from a held customer advance.')
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    reversal_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.CheckConstraint(check=Q(amount__gte=0) & Q(tds_amount__gte=0), name='crm_alloc_non_negative'),
            models.CheckConstraint(check=Q(amount__gt=0) | Q(tds_amount__gt=0), name='crm_alloc_positive'),
        ]

    @property
    def total(self):
        return self.amount + self.tds_amount


class Refund(TimeStamped):
    STATUSES = [('requested', 'Requested'), ('approved', 'Approved'), ('rejected', 'Rejected'),
                ('initiated', 'Initiated'), ('processed', 'Processed'), ('failed', 'Failed')]
    REASONS = [('cancellation', 'Order cancelled'), ('duplicate', 'Duplicate payment'), ('overpaid', 'Overpayment'),
               ('service_issue', 'Service issue'), ('goodwill', 'Goodwill'), ('late_payment', 'Paid after cancellation/expiry'),
               ('other', 'Other')]
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='refunds')
    amount = MoneyField()
    reason = models.CharField(max_length=20, choices=REASONS)
    notes = models.TextField(blank=True)
    method = models.CharField(max_length=10, choices=[('gateway', 'Gateway refund to original source'), ('manual', 'Manual')],
                              default='gateway')
    status = models.CharField(max_length=12, choices=STATUSES, default='requested', db_index=True)
    tax_portion = MoneyField(editable=False)
    gateway_refund_id = models.CharField(max_length=80, blank=True)
    manual_reference = models.CharField(max_length=80, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    credit_note = models.ForeignKey('crm.CreditNote', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    approved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['-id']
        constraints = [models.CheckConstraint(check=Q(amount__gt=0), name='crm_refund_positive')]


class Dispute(TimeStamped):
    STATUSES = [('open', 'Open'), ('evidence_submitted', 'Evidence submitted'), ('won', 'Won'), ('lost', 'Lost')]
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='disputes')
    provider_dispute_id = models.CharField(max_length=80, blank=True)
    amount = MoneyField()
    reason = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='open')
    due_by = models.DateField(null=True, blank=True)
    outcome_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-id']


class Settlement(TimeStamped):
    """Gateway pays out collected money to the bank net of fees (glossary)."""
    provider = models.CharField(max_length=20)
    settlement_id = models.CharField(max_length=80)
    settled_on = models.DateField()
    gross = MoneyField()
    fees = MoneyField()
    tax_on_fees = MoneyField()
    tds = MoneyField()
    net = MoneyField()
    bank_account = models.ForeignKey('crm.BankAccount', on_delete=models.PROTECT, related_name='settlements')
    status = models.CharField(max_length=12, default='recorded', choices=[('recorded', 'Recorded'), ('posted', 'Posted'), ('matched', 'Matched to bank')])

    class Meta:
        ordering = ['-settled_on']
        constraints = [models.UniqueConstraint(fields=['provider', 'settlement_id'], name='crm_settlement_unique')]


class TdsReceivable(models.Model):
    STATUSES = [('expected', 'Expected'), ('received', 'Certificate received'), ('claimed', 'Claimed in return'),
                ('short', 'Short or not received'), ('written_off', 'Written off')]
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, related_name='tds_receivable')
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='tds_receivables')
    amount = MoneyField()
    section = models.CharField(max_length=10, blank=True)
    deductor_tan = models.CharField(max_length=10, blank=True)
    fiscal_year = models.CharField(max_length=7)
    quarter = models.CharField(max_length=2)
    status = models.CharField(max_length=12, choices=STATUSES, default='expected', db_index=True)
    certificate_number = models.CharField('Form 16A number', max_length=40, blank=True)
    certificate_date = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    reminded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']
