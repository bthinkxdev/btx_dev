"""Double-entry ledger, posting outbox, periods, banking (Part 8)."""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import USER, MoneyField, TimeStamped

ACCOUNT_TYPES = [('asset', 'Asset'), ('liability', 'Liability'), ('equity', 'Equity'),
                 ('income', 'Income'), ('direct_cost', 'Direct cost'), ('expense', 'Expense')]
DEBIT_NORMAL = {'asset', 'direct_cost', 'expense'}


class Account(models.Model):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=120)
    account_type = models.CharField(max_length=12, choices=ACCOUNT_TYPES)
    group = models.CharField(max_length=60, blank=True)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='children')
    system_code = models.CharField(max_length=40, blank=True, db_index=True,
                                   help_text='Used by the posting map, for example AR or OUTPUT_CGST.')
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    allow_manual = models.BooleanField('Allow manual journals', default=True)
    description = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=['system_code'], condition=~Q(system_code=''),
                                               name='crm_account_system_code_unique')]

    def __str__(self):
        return f'{self.code} {self.name}'

    @property
    def is_debit_normal(self) -> bool:
        return self.account_type in DEBIT_NORMAL


class Period(models.Model):
    STATUSES = [('open', 'Open'), ('soft_closed', 'Soft-closed'), ('closed', 'Closed')]
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=12, choices=STATUSES, default='open')
    closed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    closed_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    reopened_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-year', '-month']
        constraints = [models.UniqueConstraint(fields=['year', 'month'], name='crm_period_unique')]

    def __str__(self):
        import calendar
        return f'{calendar.month_abbr[self.month]}-{self.year}'


class JournalEntry(models.Model):
    STATUSES = [('draft', 'Draft'), ('pending_approval', 'Pending approval'), ('posted', 'Posted'), ('reversed', 'Reversed')]
    number = models.CharField(max_length=30, blank=True, db_index=True)
    entry_date = models.DateField(db_index=True)
    narration = models.CharField(max_length=500)
    source_type = models.CharField(max_length=40, db_index=True)
    source_id = models.CharField(max_length=64, blank=True)
    event_key = models.CharField(max_length=80)
    status = models.CharField(max_length=20, choices=STATUSES, default='draft', db_index=True)
    is_manual = models.BooleanField(default=False)
    total_debit = MoneyField(editable=False)
    total_credit = MoneyField(editable=False)
    reversal_of = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT, related_name='reversed_by_entry')
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-entry_date', '-id']
        verbose_name_plural = 'journal entries'
        constraints = [
            models.UniqueConstraint(fields=['source_type', 'source_id', 'event_key'], name='crm_journal_source_event_unique'),
            models.UniqueConstraint(fields=['number'], condition=~Q(number=''), name='crm_journal_number_unique'),
            models.CheckConstraint(check=~Q(status='posted') | Q(total_debit=models.F('total_credit')),
                                   name='crm_journal_posted_balanced'),
        ]

    def __str__(self):
        return self.number or f'Journal draft #{self.pk}'

    def delete(self, *args, **kwargs):
        if self.status in ('posted', 'reversed'):
            raise ValidationError('Posted journals cannot be deleted. Reverse the entry instead.')
        super().delete(*args, **kwargs)


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='lines')
    debit = MoneyField()
    credit = MoneyField()
    description = models.CharField(max_length=300, blank=True)
    client = models.ForeignKey('crm.Client', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    project = models.ForeignKey('crm.Project', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    service = models.ForeignKey('crm.Service', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    department = models.ForeignKey('crm.Department', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    entry_date = models.DateField(db_index=True, editable=False)
    posted = models.BooleanField(default=False, db_index=True, editable=False)

    class Meta:
        ordering = ['id']
        indexes = [models.Index(fields=['account', 'posted', 'entry_date'])]
        constraints = [
            models.CheckConstraint(check=Q(debit__gte=0) & Q(credit__gte=0), name='crm_jl_non_negative'),
            models.CheckConstraint(check=(Q(debit__gt=0) & Q(credit=0)) | (Q(credit__gt=0) & Q(debit=0)),
                                   name='crm_jl_one_side'),
        ]


class PostingOutbox(models.Model):
    """Business transaction and outbox row commit together; a worker posts and retries (Part 8.1 #3)."""
    STATUSES = [('pending', 'Pending'), ('posted', 'Posted'), ('failed', 'Failed'), ('skipped', 'Skipped')]
    event_key = models.CharField(max_length=160, unique=True)
    event_type = models.CharField(max_length=40)
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default='pending', db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    journal = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']
        verbose_name_plural = 'posting outbox'


class DeferredRevenueSchedule(models.Model):
    """Release of deferred revenue by period; idempotent per contract line and period (Part 8.6)."""
    invoice_line = models.ForeignKey('crm.InvoiceLine', on_delete=models.PROTECT, related_name='deferral_rows')
    period_start = models.DateField()
    amount = MoneyField()
    posted = models.BooleanField(default=False)
    journal = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['period_start']
        constraints = [models.UniqueConstraint(fields=['invoice_line', 'period_start'], name='crm_deferral_unique')]


class Budget(models.Model):
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='budgets')
    department = models.ForeignKey('crm.Department', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    month = models.DateField()
    amount = MoneyField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'department', 'month'], name='crm_budget_unique')]


# ---------------------------------------------------------------- Banking (Part 8.5)

class BankAccount(TimeStamped):
    KINDS = [('bank', 'Bank'), ('cash', 'Cash / petty cash'), ('gateway', 'Payment gateway clearing')]
    name = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=10, choices=KINDS, default='bank')
    bank_name = models.CharField(max_length=120, blank=True)
    account_number = models.CharField(max_length=30, blank=True)
    ifsc = models.CharField('IFSC', max_length=11, blank=True)
    ledger_account = models.OneToOneField(Account, on_delete=models.PROTECT, related_name='bank_account')
    is_active = models.BooleanField(default=True)
    is_default_receipt = models.BooleanField('Default for manual receipts', default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class BankStatementImport(models.Model):
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='imports')
    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64)
    imported_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    line_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-id']


class BankTransaction(models.Model):
    STATUSES = [('unmatched', 'Unmatched'), ('suggested', 'Suggested'), ('matched', 'Matched'),
                ('reconciled', 'Reconciled'), ('ignored', 'Ignored')]
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='transactions')
    statement_import = models.ForeignKey(BankStatementImport, null=True, blank=True, on_delete=models.PROTECT, related_name='transactions')
    txn_date = models.DateField(db_index=True)
    description = models.CharField(max_length=500)
    reference = models.CharField(max_length=120, blank=True)
    amount = MoneyField(help_text='Positive for money in, negative for money out.')
    running_balance = MoneyField(null=True, blank=True, default=None)
    line_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=STATUSES, default='unmatched', db_index=True)
    ignore_reason = models.CharField(max_length=300, blank=True)
    suggestion = models.JSONField(default=dict, blank=True)
    reconciliation = models.ForeignKey('BankReconciliation', null=True, blank=True, on_delete=models.PROTECT, related_name='transactions')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-txn_date', '-id']
        constraints = [models.UniqueConstraint(fields=['bank_account', 'line_hash'], name='crm_bank_line_unique')]

    @property
    def matched_total(self):
        from django.db.models import Sum
        from crm.core.money import ZERO
        return self.matches.aggregate(s=Sum('amount'))['s'] or ZERO


class ReconciliationMatch(models.Model):
    bank_transaction = models.ForeignKey(BankTransaction, on_delete=models.PROTECT, related_name='matches')
    journal_line = models.OneToOneField(JournalLine, on_delete=models.PROTECT, related_name='bank_match')
    amount = MoneyField(help_text='Signed like the bank line.')
    matched_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    rule = models.CharField(max_length=30, default='manual')
    created_at = models.DateTimeField(default=timezone.now)


class BankReconciliation(models.Model):
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='reconciliations')
    statement_date = models.DateField()
    statement_balance = MoneyField()
    book_balance = MoneyField()
    outstanding = MoneyField()
    difference = MoneyField()
    finalised_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-statement_date']
