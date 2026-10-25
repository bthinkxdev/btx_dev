"""Employees, attendance, leave, payroll, commissions (Part 12)."""
from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import USER, MoneyField, RateField, TimeStamped, Versioned

EMPLOYEE_STATUSES = [('onboarding', 'Onboarding'), ('active', 'Active'), ('on_notice', 'On notice'),
                     ('on_leave', 'On leave (long)'), ('exited', 'Exited')]
EMPLOYMENT_TYPES = [('full_time', 'Full time'), ('part_time', 'Part time'), ('contract', 'Contract'), ('intern', 'Intern')]


class Shift(models.Model):
    name = models.CharField(max_length=60, unique=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    grace_minutes = models.PositiveSmallIntegerField(default=15)

    def __str__(self):
        return self.name


class Holiday(models.Model):
    holiday_date = models.DateField(unique=True)
    name = models.CharField(max_length=120)

    class Meta:
        ordering = ['holiday_date']

    def __str__(self):
        return f'{self.name} ({self.holiday_date:%d-%b-%Y})'


class Employee(TimeStamped, Versioned):
    user = models.OneToOneField(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_employee')
    code = models.CharField('Employee code', max_length=20, unique=True)
    name = models.CharField(max_length=150)
    work_email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True)
    department = models.ForeignKey('crm.Department', null=True, blank=True, on_delete=models.PROTECT, related_name='employees')
    designation = models.CharField(max_length=120, blank=True)
    manager = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='reports')
    employment_type = models.CharField(max_length=12, choices=EMPLOYMENT_TYPES, default='full_time')
    joining_date = models.DateField()
    last_working_day = models.DateField(null=True, blank=True)
    exit_reason = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=12, choices=EMPLOYEE_STATUSES, default='onboarding', db_index=True)
    location = models.CharField(max_length=80, blank=True)
    shift = models.ForeignKey(Shift, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    # Sensitive (field groups: tax_ids, bank_details, salary)
    pan = models.CharField('PAN', max_length=10, blank=True)
    uan = models.CharField('UAN', max_length=12, blank=True)
    bank_account_number = models.CharField(max_length=30, blank=True)
    bank_ifsc = models.CharField('IFSC', max_length=11, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    address = models.TextField(blank=True)
    emergency_contact = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.code})'

    @property
    def is_active_employee(self):
        return self.status in ('onboarding', 'active', 'on_notice', 'on_leave')


class ChecklistItem(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='checklist')
    kind = models.CharField(max_length=12, choices=[('onboarding', 'Onboarding'), ('offboarding', 'Offboarding')])
    title = models.CharField(max_length=200)
    owner = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    due_date = models.DateField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)
    done_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['kind', 'id']


ATTENDANCE_STATUSES = [('present', 'Present'), ('absent', 'Absent'), ('half_day', 'Half day'), ('leave', 'On leave'),
                       ('holiday', 'Holiday'), ('wfh', 'Work from home'), ('weekly_off', 'Weekly off')]


class Attendance(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='attendance')
    att_date = models.DateField(db_index=True)
    status = models.CharField(max_length=12, choices=ATTENDANCE_STATUSES, default='present')
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    is_late = models.BooleanField(default=False)
    overtime_minutes = models.PositiveIntegerField(default=0)
    source = models.CharField(max_length=10, default='manual', choices=[('manual', 'Manual'), ('import', 'Import'), ('self', 'Self check-in')])
    note = models.CharField(max_length=300, blank=True)
    marked_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['-att_date']
        constraints = [models.UniqueConstraint(fields=['employee', 'att_date'], name='crm_attendance_unique')]


class AttendanceCorrection(models.Model):
    STATUSES = [('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected')]
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='attendance_corrections')
    att_date = models.DateField()
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    status_requested = models.CharField(max_length=12, choices=ATTENDANCE_STATUSES, default='present')
    reason = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=STATUSES, default='pending')
    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    decided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-id']


class LeaveType(models.Model):
    code = models.SlugField(max_length=10, unique=True)
    name = models.CharField(max_length=60)
    annual_quota = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('12'))
    is_paid = models.BooleanField(default=True)
    carry_forward = models.BooleanField(default=False)
    attachment_after_days = models.PositiveSmallIntegerField(default=0, help_text='0 = never required.')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class LeaveBalance(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='leave_balances')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name='+')
    year = models.PositiveSmallIntegerField()
    allocated = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('0'))
    used = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('0'))
    adjusted = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('0'))

    class Meta:
        constraints = [models.UniqueConstraint(fields=['employee', 'leave_type', 'year'], name='crm_leave_balance_unique')]

    @property
    def available(self):
        return self.allocated + self.adjusted - self.used


class LeaveRequest(models.Model):
    STATUSES = [('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected'), ('cancelled', 'Cancelled')]
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='leave_requests')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name='+')
    start_date = models.DateField()
    end_date = models.DateField()
    half_day_start = models.BooleanField(default=False)
    half_day_end = models.BooleanField(default=False)
    days = models.DecimalField(max_digits=5, decimal_places=1)
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=10, choices=STATUSES, default='pending', db_index=True)
    approver = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-start_date']
        constraints = [models.CheckConstraint(check=Q(end_date__gte=models.F('start_date')), name='crm_leave_dates')]


class LeaveAdjustment(models.Model):
    balance = models.ForeignKey(LeaveBalance, on_delete=models.PROTECT, related_name='adjustments')
    days = models.DecimalField(max_digits=5, decimal_places=1)
    reason = models.CharField(max_length=300)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)


# ---------------------------------------------------------------- Payroll (Part 12.4)

class SalaryComponent(models.Model):
    KINDS = [('earning', 'Earning'), ('deduction', 'Deduction'), ('employer', 'Employer contribution')]
    code = models.SlugField(max_length=20, unique=True)
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=10, choices=KINDS)
    calculation = models.CharField(max_length=12, default='fixed',
                                   choices=[('fixed', 'Fixed amount'), ('pct_basic', 'Percent of basic'),
                                            ('pct_gross', 'Percent of gross')])
    default_value = MoneyField()
    prorate = models.BooleanField('Pro-rate by paid days', default=True)
    sort = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['kind', 'sort', 'id']

    def __str__(self):
        return self.name


class SalaryStructure(TimeStamped):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='salary_structures')
    effective_from = models.DateField()
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-effective_from']
        constraints = [models.UniqueConstraint(fields=['employee', 'effective_from'], name='crm_salary_effective_unique')]


class SalaryStructureLine(models.Model):
    structure = models.ForeignKey(SalaryStructure, on_delete=models.CASCADE, related_name='lines')
    component = models.ForeignKey(SalaryComponent, on_delete=models.PROTECT, related_name='+')
    value = MoneyField(help_text='Amount for fixed, percent for percentage components.')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['structure', 'component'], name='crm_salary_line_unique')]


PAYROLL_STATUSES = [('draft', 'Draft'), ('in_review', 'In review'), ('pending_approval', 'Pending approval'),
                    ('approved', 'Approved and locked'), ('paid', 'Paid'), ('partially_paid', 'Partially paid'),
                    ('reversed', 'Reversed')]


class PayrollRun(TimeStamped):
    period = models.DateField(help_text='First day of the payroll month.')
    status = models.CharField(max_length=20, choices=PAYROLL_STATUSES, default='draft', db_index=True)
    working_days = models.PositiveSmallIntegerField(default=0)
    gross_total = MoneyField(editable=False)
    deduction_total = MoneyField(editable=False)
    employer_total = MoneyField(editable=False)
    net_total = MoneyField(editable=False)
    prepared_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    reviewed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    approved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    payslips_released_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.CharField(max_length=300, blank=True)
    exceptions = models.JSONField(default=list, blank=True, editable=False)
    last_calculated_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ['-period']
        constraints = [models.UniqueConstraint(fields=['period'], condition=~Q(status='reversed'),
                                               name='crm_payroll_period_unique')]

    def __str__(self):
        return f'Payroll {self.period:%b-%Y}'

    @property
    def is_locked(self):
        return self.status in ('approved', 'paid', 'partially_paid', 'reversed')


class Payslip(models.Model):
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name='payslips')
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='payslips')
    working_days = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('0'))
    paid_days = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('0'))
    lop_days = models.DecimalField('Loss-of-pay days', max_digits=5, decimal_places=1, default=Decimal('0'))
    gross = MoneyField()
    deductions = MoneyField()
    employer_contribution = MoneyField()
    commissions = MoneyField()
    reimbursements = MoneyField()
    adjustments = MoneyField()
    net = MoneyField()
    lines = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, default='pending', choices=[('pending', 'Pending'), ('paid', 'Paid')])
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_reference = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ['employee__name']
        constraints = [models.UniqueConstraint(fields=['run', 'employee'], name='crm_payslip_unique')]


class PayrollAdjustment(TimeStamped):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='payroll_adjustments')
    amount = MoneyField(help_text='Positive adds to pay, negative deducts.')
    reason = models.CharField(max_length=300)
    applied_run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name='applied_adjustments')

    class Meta:
        ordering = ['-id']


class CommissionRule(TimeStamped):
    BASES = [('won', 'Won value'), ('invoiced', 'Invoiced value'), ('collected', 'Collected value')]
    name = models.CharField(max_length=120)
    basis = models.CharField(max_length=10, choices=BASES, default='collected')
    rate_percent = RateField()
    roles = models.ManyToManyField('crm.Role', blank=True, related_name='+')
    service = models.ForeignKey('crm.Service', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    clawback_on_refund = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-effective_from']

    def __str__(self):
        return self.name


class CommissionEntry(TimeStamped):
    STATUSES = [('accrued', 'Accrued'), ('approved', 'Approved'), ('paid', 'Paid'), ('clawed_back', 'Clawed back')]
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_commissions')
    rule = models.ForeignKey(CommissionRule, on_delete=models.PROTECT, related_name='entries')
    lead = models.ForeignKey('crm.Lead', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    payment = models.ForeignKey('crm.Payment', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    invoice = models.ForeignKey('crm.Invoice', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    event_key = models.CharField(max_length=120, unique=True)
    basis_amount = MoneyField()
    rate = RateField()
    amount = MoneyField(help_text='Negative for a clawback.')
    status = models.CharField(max_length=12, choices=STATUSES, default='accrued', db_index=True)
    approved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    payroll_run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name='commissions')

    class Meta:
        ordering = ['-id']
        verbose_name_plural = 'commission entries'
