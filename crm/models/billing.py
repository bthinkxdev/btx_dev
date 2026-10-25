"""Invoices, credit notes, receipt vouchers (Part 7)."""
from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import USER, MoneyField, TimeStamped, Versioned
from .core import private_storage
from .documents import TaxDocument, TaxLine

INV_DRAFT = 'draft'
INV_PENDING = 'pending_approval'
INV_ISSUED = 'issued'
INV_PARTIAL = 'partially_paid'
INV_PAID = 'paid'
INV_CREDIT_NOTED = 'credit_noted'
INV_CANCELLED = 'cancelled'
INV_WRITTEN_OFF = 'written_off'
INVOICE_STATUSES = [
    (INV_DRAFT, 'Draft'), (INV_PENDING, 'Pending approval'), (INV_ISSUED, 'Issued'),
    (INV_PARTIAL, 'Partially paid'), (INV_PAID, 'Paid'), (INV_CREDIT_NOTED, 'Credit noted'),
    (INV_CANCELLED, 'Cancelled'), (INV_WRITTEN_OFF, 'Written off'),
]
INV_OPEN = [INV_ISSUED, INV_PARTIAL]          # have a balance to collect
INV_POSTED = [INV_ISSUED, INV_PARTIAL, INV_PAID, INV_CREDIT_NOTED, INV_WRITTEN_OFF]


class Invoice(TaxDocument, TimeStamped, Versioned):
    number = models.CharField(max_length=30, blank=True, db_index=True)
    fiscal_year = models.CharField(max_length=7, blank=True)
    status = models.CharField(max_length=20, choices=INVOICE_STATUSES, default=INV_DRAFT, db_index=True)
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='invoices')
    owner = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_invoices')
    quotation = models.ForeignKey('crm.Quotation', null=True, blank=True, on_delete=models.PROTECT, related_name='invoices')
    contract = models.ForeignKey('crm.Contract', null=True, blank=True, on_delete=models.PROTECT, related_name='invoices')
    project = models.ForeignKey('crm.Project', null=True, blank=True, on_delete=models.PROTECT, related_name='invoices')
    billing_schedule = models.OneToOneField('crm.BillingSchedule', null=True, blank=True, on_delete=models.PROTECT,
                                            related_name='invoice')
    milestone_ref = models.CharField(max_length=120, blank=True)
    po_number = models.CharField('PO number', max_length=60, blank=True)
    invoice_date = models.DateField()
    due_date = models.DateField()
    shipping_address = models.TextField(blank=True)
    notes = models.TextField('Notes to customer', blank=True)
    terms = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    tds_expected = MoneyField(help_text='TDS the customer is expected to deduct.')

    advance_adjusted = MoneyField(editable=False)
    paid_amount = MoneyField(editable=False)
    tds_withheld = MoneyField(editable=False)
    credit_noted = MoneyField(editable=False)
    written_off = MoneyField(editable=False)
    balance = MoneyField(editable=False)

    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    sent_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)
    written_off_at = models.DateTimeField(null=True, blank=True)
    write_off_reason = models.CharField(max_length=300, blank=True)
    disputed = models.BooleanField(default=False)
    dispute_reason = models.CharField(max_length=300, blank=True)
    promise_date = models.DateField(null=True, blank=True)
    promise_amount = MoneyField(null=True, blank=True, default=None)
    promise_note = models.CharField(max_length=300, blank=True)
    last_reminder_step = models.CharField(max_length=10, blank=True, editable=False)
    last_reminder_at = models.DateTimeField(null=True, blank=True, editable=False)
    einvoice_status = models.CharField(max_length=15, default='not_required',
                                       choices=[('not_required', 'Not required'), ('pending', 'Pending'),
                                                ('generated', 'Generated'), ('failed', 'Failed'), ('cancelled', 'Cancelled')])
    irn = models.CharField('IRN', max_length=80, blank=True)
    pdf_file = models.FileField(upload_to='invoices/%Y/%m', storage=private_storage, blank=True, max_length=300,
                                editable=False)
    pdf_sha256 = models.CharField(max_length=64, blank=True, editable=False)

    class Meta:
        ordering = ['-invoice_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['number'], condition=~Q(number=''), name='crm_invoice_number_unique'),
            models.CheckConstraint(check=Q(due_date__gte=models.F('invoice_date')), name='crm_invoice_due_after_date'),
            models.CheckConstraint(check=Q(balance__gte=0), name='crm_invoice_balance_non_negative'),
        ]
        indexes = [models.Index(fields=['client', 'status'])]

    def __str__(self):
        return self.display_number

    @property
    def display_number(self) -> str:
        return self.number or f'Draft #{self.pk}'

    @property
    def is_overdue(self) -> bool:
        from crm.core.india import today
        return self.status in INV_OPEN and self.balance > 0 and self.due_date < today()

    @property
    def days_overdue(self) -> int:
        from crm.core.india import today
        return max(0, (today() - self.due_date).days)

    @property
    def age_bucket(self) -> str:
        if self.status not in INV_OPEN or self.balance <= 0:
            return ''
        d = self.days_overdue
        if d == 0:
            return 'Current'
        if d <= 30:
            return '1-30'
        if d <= 60:
            return '31-60'
        if d <= 90:
            return '61-90'
        return '90+'

    @property
    def display_status(self) -> str:
        """Overdue is computed (Part 7.2); stored status stays Issued/Partially paid."""
        return 'overdue' if self.is_overdue else self.status

    @property
    def amount_due(self):
        return self.balance

    @property
    def is_editable(self) -> bool:
        return self.status == INV_DRAFT


class InvoiceLine(TaxLine):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='lines')

    class Meta(TaxLine.Meta):
        pass


CREDIT_REASONS = [('return', 'Return'), ('cancellation', 'Cancellation'), ('discount', 'Discount after sale'),
                  ('billing_error', 'Billing error'), ('refund', 'Refund'), ('other', 'Other')]


class CreditNote(TimeStamped):
    STATUSES = [('draft', 'Draft'), ('pending_approval', 'Pending approval'), ('issued', 'Issued'), ('cancelled', 'Cancelled')]
    number = models.CharField(max_length=30, blank=True, db_index=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='credit_notes')
    status = models.CharField(max_length=20, choices=STATUSES, default='draft')
    note_date = models.DateField()
    reason = models.CharField(max_length=20, choices=CREDIT_REASONS)
    notes = models.TextField(blank=True)
    treatment = models.CharField(max_length=15, default='reduce_balance',
                                 choices=[('reduce_balance', 'Reduce balance'), ('refund', 'Refund money')])
    taxable_value = MoneyField()
    cgst = MoneyField()
    sgst = MoneyField()
    igst = MoneyField()
    total = MoneyField()
    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    refund = models.ForeignKey('crm.Refund', null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=['number'], condition=~Q(number=''), name='crm_creditnote_number_unique'),
            models.CheckConstraint(check=Q(total__gt=0), name='crm_creditnote_positive'),
        ]

    def __str__(self):
        return self.number or f'Credit note draft #{self.pk}'


class ReceiptVoucher(models.Model):
    """Advance receipt voucher (Part 7.5): records advance money before an invoice exists."""
    number = models.CharField(max_length=30, unique=True)
    payment = models.OneToOneField('crm.Payment', on_delete=models.PROTECT, related_name='receipt_voucher')
    client = models.ForeignKey('crm.Client', on_delete=models.PROTECT, related_name='receipt_vouchers')
    voucher_date = models.DateField()
    amount = MoneyField()
    taxable_value = MoneyField()
    cgst = MoneyField()
    sgst = MoneyField()
    igst = MoneyField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return self.number
