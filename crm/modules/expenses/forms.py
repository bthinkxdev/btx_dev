"""Expense, vendor, payment, recurring and TDS challan forms (Part 11.2-11.5)."""
from __future__ import annotations

from decimal import Decimal

from django import forms

from crm.core import india
from crm.core.forms import (CrmForm, CrmModelForm, DateInput, GSTINField, MoneyFormField, PANField, PhoneField, SafeFileField,
                            VersionedModelFormMixin)
from crm.models import BankAccount, Expense, ExpenseCategory, RecurringExpense, Vendor
from crm.models.clients import TDS_SECTIONS


class ExpenseForm(VersionedModelFormMixin, CrmModelForm):
    vendor_gstin = GSTINField(label='Vendor GSTIN (for input credit)')
    cgst = MoneyFormField(required=False, label='CGST')
    sgst = MoneyFormField(required=False, label='SGST')
    igst = MoneyFormField(required=False, label='IGST')
    tds_amount = MoneyFormField(required=False, label='TDS deducted')
    proof = SafeFileField(required=False, allowed=('.pdf', '.png', '.jpg', '.jpeg', '.webp'), label='Bill or receipt')
    duplicate_override_reason = forms.CharField(required=False, max_length=300, label='Not a duplicate because')

    class Meta:
        model = Expense
        fields = ['kind', 'vendor', 'employee', 'category', 'classification', 'project', 'description', 'bill_number', 'vendor_gstin',
                  'bill_date', 'due_date', 'amount_before_tax', 'cgst', 'sgst', 'igst', 'itc_eligibility', 'tds_section', 'tds_amount',
                  'duplicate_override_reason']
        widgets = {'bill_date': DateInput, 'due_date': DateInput}
        labels = {'amount_before_tax': 'Amount before tax', 'bill_number': 'Vendor invoice number'}

    def __init__(self, *args, user=None, can_pick_employee=False, can_set_itc=False, **kwargs):
        super().__init__(*args, **kwargs)
        from django.contrib.auth import get_user_model
        U = get_user_model()
        self.fields['vendor'].queryset = Vendor.objects.exclude(status='blocked')
        self.fields['vendor'].required = False
        self.fields['category'].queryset = ExpenseCategory.objects.filter(is_active=True)
        self.fields['employee'].queryset = U.objects.filter(is_active=True) if can_pick_employee else U.objects.filter(pk=user.pk)
        self.fields['employee'].label_from_instance = lambda u: u.get_full_name() or u.username
        self.fields['employee'].required = False
        from crm.models import Project
        self.fields['project'].queryset = Project.objects.exclude(status__in=['closed', 'cancelled'])
        self.fields['project'].required = False
        if not can_set_itc:
            self.fields['itc_eligibility'].disabled = True
            self.fields['itc_eligibility'].help_text = 'Set by the category rule.'
        self.initial.setdefault('bill_date', india.today())

    def clean(self):
        c = super().clean()
        for k in ('cgst', 'sgst', 'igst', 'tds_amount'):
            c[k] = c.get(k) or Decimal('0')
        if c['igst'] and (c['cgst'] or c['sgst']):
            self.add_error('igst', 'Use either IGST or CGST + SGST, not both.')
        if abs(c['cgst'] - c['sgst']) > Decimal('0.01'):
            self.add_error('sgst', 'CGST and SGST are normally equal.')
        if c.get('bill_date') and c['bill_date'] > india.today():
            self.add_error('bill_date', 'The bill date cannot be in the future.')
        if c.get('due_date') and c.get('bill_date') and c['due_date'] < c['bill_date']:
            self.add_error('due_date', 'The due date cannot be before the bill date.')
        if c.get('kind') == 'vendor_bill' and not c.get('vendor'):
            self.add_error('vendor', 'Choose the vendor.')
        if c.get('kind') == 'reimbursement' and not c.get('employee'):
            self.add_error('employee', 'Choose who is reimbursed.')
        if c.get('tds_amount') and not c.get('tds_section'):
            self.add_error('tds_section', 'Choose the TDS section.')
        if c.get('classification') == 'direct' and not c.get('project'):
            self.add_error('project', 'A direct cost needs a project (or use an allocation after saving).')
        return c


class AllocationRowForm(forms.Form):
    project = forms.ModelChoiceField(queryset=None, required=False)
    client = forms.ModelChoiceField(queryset=None, required=False)
    department = forms.ModelChoiceField(queryset=None, required=False)
    percent = forms.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal('0.01'), max_value=100)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from crm.models import Client, Department, Project
        self.fields['project'].queryset = Project.objects.exclude(status='cancelled')
        self.fields['client'].queryset = Client.objects.filter(archived_at__isnull=True)
        self.fields['department'].queryset = Department.objects.all()

    def clean(self):
        c = super().clean()
        if not (c.get('project') or c.get('client') or c.get('department')):
            raise forms.ValidationError('Choose a project, client or department.')
        return c


AllocationFormSet = forms.formset_factory(AllocationRowForm, extra=0, min_num=1, max_num=30)


class PayForm(CrmForm):
    amount = MoneyFormField()
    payment_date = forms.DateField(widget=DateInput, label='Date')
    bank_account = forms.ModelChoiceField(queryset=BankAccount.objects.filter(is_active=True).exclude(kind='gateway'), label='Paid from')
    method = forms.ChoiceField(choices=[('bank_transfer', 'Bank transfer'), ('upi', 'UPI'), ('cheque', 'Cheque'), ('cash', 'Cash')])
    reference = forms.CharField(max_length=80, required=False, label='Reference / UTR')


class VendorForm(VersionedModelFormMixin, CrmModelForm):
    pan = PANField(label='PAN')
    gstin = GSTINField(label='GSTIN')
    phone = PhoneField(required=False)

    class Meta:
        model = Vendor
        fields = ['name', 'pan', 'gstin', 'state', 'email', 'phone', 'address', 'tds_section', 'tds_rate', 'bank_name',
                  'bank_account_number', 'bank_ifsc']
        widgets = {'address': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, show_bank=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not show_bank:
            for f in ('bank_name', 'bank_account_number', 'bank_ifsc'):
                del self.fields[f]

    def clean(self):
        c = super().clean()
        if c.get('gstin') and c.get('state') and c['gstin'][:2] != c['state']:
            self.add_error('state', 'The state must match the first two digits of the GSTIN.')
        return c


class RecurringForm(CrmModelForm):
    class Meta:
        model = RecurringExpense
        fields = ['name', 'vendor', 'category', 'amount_before_tax', 'tax_rate', 'frequency', 'next_run', 'end_date']
        widgets = {'next_run': DateInput, 'end_date': DateInput}
        labels = {'next_run': 'First bill date'}


class ChallanForm(CrmForm):
    section = forms.ChoiceField(choices=TDS_SECTIONS)
    amount = MoneyFormField()
    paid_on = forms.DateField(widget=DateInput, label='Paid on')
    cin = forms.CharField(max_length=40, label='Challan identification number (CIN)')
    bank_account = forms.ModelChoiceField(queryset=BankAccount.objects.filter(is_active=True, kind='bank'), label='Paid from')
