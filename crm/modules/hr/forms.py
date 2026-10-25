"""HR, attendance, leave and payroll forms (Part 12.3)."""
from __future__ import annotations

from decimal import Decimal

from django import forms

from crm.core import india
from crm.core.forms import CrmForm, CrmModelForm, DateInput, PANField, PhoneField, SafeFileField, VersionedModelFormMixin
from crm.models import (Department, Employee, Holiday, LeaveType, Role, SalaryComponent, Shift)
from crm.models.people import ATTENDANCE_STATUSES


class EmployeeForm(VersionedModelFormMixin, CrmModelForm):
    phone = PhoneField(required=False)
    pan = PANField(label='PAN')

    class Meta:
        model = Employee
        fields = ['code', 'name', 'work_email', 'phone', 'department', 'designation', 'manager', 'employment_type', 'joining_date',
                  'location', 'shift', 'pan', 'uan', 'bank_account_number', 'bank_ifsc', 'date_of_birth', 'address', 'emergency_contact']
        widgets = {'joining_date': DateInput, 'date_of_birth': DateInput, 'address': forms.Textarea(attrs={'rows': 2})}

    SENSITIVE = {'tax_ids': ('pan', 'uan'), 'bank_details': ('bank_account_number', 'bank_ifsc'),
                 'salary': ('date_of_birth', 'address', 'emergency_contact')}

    def __init__(self, *args, can_see=lambda g: False, **kwargs):
        super().__init__(*args, **kwargs)
        for group, fields in self.SENSITIVE.items():
            if not can_see(group):
                for f in fields:
                    self.fields.pop(f, None)
        self.fields['manager'].queryset = Employee.objects.exclude(status='exited').exclude(pk=self.instance.pk or 0)
        self.fields['manager'].required = False
        self.fields['department'].required = False
        self.fields['shift'].required = False


class NewLoginForm(CrmForm):
    create_login = forms.BooleanField(required=False, initial=True, label='Create a login for this employee')
    role = forms.ModelChoiceField(queryset=Role.objects.all(), required=False, label='Role')


class TransferForm(CrmForm):
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    manager = forms.ModelChoiceField(queryset=Employee.objects.exclude(status='exited'), required=False, label='Reporting manager')
    designation = forms.CharField(max_length=120, required=False)
    effective = forms.DateField(widget=DateInput, label='Effective date')


class OffboardForm(CrmForm):
    last_day = forms.DateField(widget=DateInput, label='Last working day')
    reason = forms.CharField(max_length=300, widget=forms.Textarea(attrs={'rows': 2}), label='Reason')


class RevealForm(CrmForm):
    group = forms.ChoiceField(choices=[('tax_ids', 'PAN / UAN'), ('bank_details', 'Bank details'), ('salary', 'Salary and personal')])
    reason = forms.CharField(max_length=200, min_length=5)


class AttendanceForm(CrmForm):
    employee = forms.ModelChoiceField(queryset=Employee.objects.exclude(status='exited'))
    on = forms.DateField(widget=DateInput, label='Date')
    status = forms.ChoiceField(choices=ATTENDANCE_STATUSES)
    check_in = forms.TimeField(required=False, widget=forms.TimeInput(attrs={'type': 'time'}))
    check_out = forms.TimeField(required=False, widget=forms.TimeInput(attrs={'type': 'time'}))
    note = forms.CharField(required=False, max_length=300)


class CorrectionForm(CrmForm):
    on = forms.DateField(widget=DateInput, label='Date')
    status = forms.ChoiceField(choices=ATTENDANCE_STATUSES)
    check_in = forms.TimeField(required=False, widget=forms.TimeInput(attrs={'type': 'time'}))
    check_out = forms.TimeField(required=False, widget=forms.TimeInput(attrs={'type': 'time'}))
    reason = forms.CharField(max_length=300, widget=forms.Textarea(attrs={'rows': 2}))


class ImportForm(CrmForm):
    file = SafeFileField(allowed=('.csv',), label='CSV with code, date, status, in, out')


class LeaveForm(CrmForm):
    leave_type = forms.ModelChoiceField(queryset=LeaveType.objects.filter(is_active=True), label='Type')
    start = forms.DateField(widget=DateInput, label='From')
    end = forms.DateField(widget=DateInput, label='To')
    half_start = forms.BooleanField(required=False, label='First day is a half day')
    half_end = forms.BooleanField(required=False, label='Last day is a half day')
    reason = forms.CharField(max_length=500, widget=forms.Textarea(attrs={'rows': 2}))
    attachment = SafeFileField(required=False, allowed=('.pdf', '.png', '.jpg', '.jpeg'), label='Attachment (if required)')

    def clean(self):
        c = super().clean()
        if c.get('start') and c.get('end') and c['end'] < c['start']:
            self.add_error('end', 'The end date cannot be before the start date.')
        return c


class AdjustForm(CrmForm):
    days = forms.DecimalField(max_digits=5, decimal_places=1, label='Days (+ adds, − removes)')
    reason = forms.CharField(max_length=300, min_length=10)


class HolidayForm(CrmModelForm):
    class Meta:
        model = Holiday
        fields = ['holiday_date', 'name']
        widgets = {'holiday_date': DateInput}


class ShiftForm(CrmModelForm):
    class Meta:
        model = Shift
        fields = ['name', 'start_time', 'end_time', 'grace_minutes']
        widgets = {'start_time': forms.TimeInput(attrs={'type': 'time'}), 'end_time': forms.TimeInput(attrs={'type': 'time'})}


class StructureForm(CrmForm):
    effective_from = forms.DateField(widget=DateInput)
    note = forms.CharField(required=False, max_length=300)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for c in SalaryComponent.objects.filter(is_active=True):
            label = c.name + (' (% of basic)' if c.calculation == 'pct_basic' else ' (% of gross)' if c.calculation == 'pct_gross' else ' (₹ per month)')
            self.fields[f'c_{c.code}'] = forms.DecimalField(required=False, min_value=0, max_digits=12, decimal_places=2, label=label,
                                                            initial=c.default_value or None)
        self._style()

    def component_values(self):
        out = {}
        for c in SalaryComponent.objects.filter(is_active=True):
            v = self.cleaned_data.get(f'c_{c.code}')
            if v not in (None, ''):
                out[c] = Decimal(v)
        return out


class AdjustmentForm(CrmForm):
    employee = forms.ModelChoiceField(queryset=Employee.objects.exclude(status='exited'))
    amount = forms.DecimalField(max_digits=12, decimal_places=2, label='Amount (+ pay, − recover)')
    reason = forms.CharField(max_length=300, min_length=10)


class MarkPaidForm(CrmForm):
    from crm.models import BankAccount
    bank_account = forms.ModelChoiceField(queryset=BankAccount.objects.filter(is_active=True, kind='bank'), label='Paid from')
    paid_on = forms.DateField(widget=DateInput, initial=india.today, label='Paid on')
    reference = forms.CharField(max_length=80, label='Bank batch reference')


class CommissionRuleForm(CrmModelForm):
    class Meta:
        from crm.models import CommissionRule
        model = CommissionRule
        fields = ['name', 'basis', 'rate_percent', 'roles', 'service', 'effective_from', 'effective_to', 'clawback_on_refund', 'is_active']
        widgets = {'effective_from': DateInput, 'effective_to': DateInput, 'roles': forms.CheckboxSelectMultiple}


