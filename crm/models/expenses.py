"""Expenses, vendors, vendor bills, reimbursements, TDS payable, ITC (Part 11)."""
from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone

from crm.core.india import STATE_CHOICES

from .base import USER, MoneyField, RateField, TimeStamped, Versioned
from .clients import TDS_SECTIONS


class Vendor(TimeStamped, Versioned):
    STATUSES = [('active', 'Active'), ('on_hold', 'On hold'), ('blocked', 'Blocked')]
    name = models.CharField(max_length=200, unique=True)
    pan = models.CharField('PAN', max_length=10, blank=True)
    gstin = models.CharField('GSTIN', max_length=15, blank=True)
    state = models.CharField(max_length=2, choices=STATE_CHOICES, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    bank_account_number = models.CharField(max_length=30, blank=True)
    bank_ifsc = models.CharField('IFSC', max_length=11, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    tds_section = models.CharField(max_length=10, choices=TDS_SECTIONS, blank=True)
    tds_rate = RateField()
    status = models.CharField(max_length=10, choices=STATUSES, default='active')
    status_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class ExpenseCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    account = models.ForeignKey('crm.Account', on_delete=models.PROTECT, related_name='+')
    itc_default = models.CharField(max_length=15, default='eligible',
                                   choices=[('eligible', 'Eligible'), ('blocked', 'Blocked'), ('na', 'Not applicable')])
    tds_section = models.CharField(max_length=10, choices=TDS_SECTIONS, blank=True)
    tds_rate = RateField()
    tds_threshold = MoneyField(help_text='Deduct TDS only when the bill is above this amount.')
    is_direct_cost = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'expense categories'

    def __str__(self):
        return self.name


EXPENSE_KINDS = [('expense', 'Expense'), ('vendor_bill', 'Vendor bill'), ('reimbursement', 'Reimbursement')]
EXPENSE_STATUSES = [('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'),
                    ('rejected', 'Rejected'), ('correction_requested', 'Correction requested'),
                    ('partially_paid', 'Partially paid'), ('paid', 'Paid'), ('reversed', 'Reversed')]
EXPENSE_PAYABLE = ['approved', 'partially_paid']


class Expense(TimeStamped, Versioned):
    number = models.CharField(max_length=30, blank=True, db_index=True)
    kind = models.CharField(max_length=15, choices=EXPENSE_KINDS, default='expense')
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.PROTECT, related_name='expenses')
    employee = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_reimbursements',
                                 help_text='For reimbursements: who is paid back.')
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name='expenses')
    classification = models.CharField(max_length=10, default='overhead',
                                      choices=[('direct', 'Direct (project)'), ('shared', 'Shared'), ('overhead', 'Overhead')])
    description = models.CharField(max_length=300)
    bill_number = models.CharField('Vendor invoice number', max_length=60, blank=True)
    vendor_gstin = models.CharField(max_length=15, blank=True)
    bill_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    amount_before_tax = MoneyField()
    cgst = MoneyField()
    sgst = MoneyField()
    igst = MoneyField()
    total = MoneyField(editable=False)
    itc_eligibility = models.CharField(max_length=15, default='eligible',
                                       choices=[('eligible', 'Eligible'), ('blocked', 'Blocked'), ('na', 'Not applicable')])
    tds_section = models.CharField(max_length=10, choices=TDS_SECTIONS, blank=True)
    tds_amount = MoneyField()
    payable_amount = MoneyField(editable=False)
    paid_amount = MoneyField(editable=False)
    status = models.CharField(max_length=24, choices=EXPENSE_STATUSES, default='draft', db_index=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.CharField(max_length=500, blank=True)
    duplicate_override_reason = models.CharField(max_length=300, blank=True)
    recurring = models.ForeignKey('RecurringExpense', null=True, blank=True, on_delete=models.PROTECT, related_name='generated')
    recurring_period = models.DateField(null=True, blank=True)
    project = models.ForeignKey('crm.Project', null=True, blank=True, on_delete=models.PROTECT, related_name='expenses')

    class Meta:
        ordering = ['-bill_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['vendor', 'bill_number'], condition=~Q(bill_number='') & ~Q(status='reversed'),
                                    name='crm_expense_vendor_bill_unique'),
            models.UniqueConstraint(fields=['recurring', 'recurring_period'], condition=Q(recurring__isnull=False),
                                    name='crm_expense_recurring_period_unique'),
            models.CheckConstraint(check=Q(amount_before_tax__gte=0), name='crm_expense_amount_non_negative'),
        ]

    def __str__(self):
        return self.number or f'Expense draft #{self.pk}'

    @property
    def tax_total(self):
        return self.cgst + self.sgst + self.igst

    @property
    def balance(self):
        return self.payable_amount - self.paid_amount

    @property
    def allocated_percent(self):
        return sum((a.percent for a in self.allocations.all()), Decimal('0'))


class ExpenseAllocation(models.Model):
    """Spreads one expense across projects/clients/departments without creating another expense."""
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name='allocations')
    project = models.ForeignKey('crm.Project', null=True, blank=True, on_delete=models.PROTECT, related_name='expense_allocations')
    client = models.ForeignKey('crm.Client', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    department = models.ForeignKey('crm.Department', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    percent = RateField()
    amount = MoneyField()

    class Meta:
        constraints = [models.CheckConstraint(check=Q(percent__gt=0) & Q(percent__lte=100), name='crm_alloc_percent_range')]


class ExpensePayment(models.Model):
    expense = models.ForeignKey(Expense, on_delete=models.PROTECT, related_name='payments')
    amount = MoneyField()
    payment_date = models.DateField()
    bank_account = models.ForeignKey('crm.BankAccount', on_delete=models.PROTECT, related_name='+')
    method = models.CharField(max_length=20, default='bank_transfer')
    reference = models.CharField(max_length=80, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=['bank_account', 'reference'], condition=~Q(reference=''),
                                    name='crm_expense_payment_reference_unique'),
            models.CheckConstraint(check=Q(amount__gt=0), name='crm_expense_payment_positive'),
        ]


class RecurringExpense(TimeStamped):
    STATUSES = [('active', 'Active'), ('paused', 'Paused'), ('ended', 'Ended')]
    name = models.CharField(max_length=150)
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name='+')
    amount_before_tax = MoneyField()
    tax_rate = RateField(default=Decimal('18.00'))
    frequency = models.CharField(max_length=10, default='monthly',
                                 choices=[('monthly', 'Monthly'), ('quarterly', 'Quarterly'), ('annual', 'Annual')])
    next_run = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=STATUSES, default='active')

    class Meta:
        ordering = ['next_run']

    def __str__(self):
        return self.name


class TdsChallan(TimeStamped):
    section = models.CharField(max_length=10, choices=TDS_SECTIONS)
    amount = MoneyField()
    paid_on = models.DateField()
    cin = models.CharField('Challan identification number (CIN)', max_length=40, unique=True)
    quarter = models.CharField(max_length=2)
    fiscal_year = models.CharField(max_length=7)
    bank_account = models.ForeignKey('crm.BankAccount', on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['-paid_on']
