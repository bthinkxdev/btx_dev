"""Accounting forms: accounts, manual journals, periods, bank accounts, transfers, statements (Part 8.4-8.5)."""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.forms import formset_factory

from crm.core import india
from crm.core.forms import CrmForm, CrmModelForm, DateInput, MoneyFormField, SafeFileField
from crm.models import Account, BankAccount, Client, Project
from crm.models.accounting import ACCOUNT_TYPES


class AccountForm(CrmModelForm):
    class Meta:
        model = Account
        fields = ['code', 'name', 'account_type', 'group', 'parent', 'allow_manual', 'description']

    def __init__(self, *args, locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['parent'].queryset = Account.objects.filter(is_active=True).exclude(pk=self.instance.pk or 0)
        self.fields['parent'].required = False
        if locked:
            for f in ('code', 'account_type'):
                self.fields[f].disabled = True
                self.fields[f].help_text = 'Fixed once the account has postings.'

    def clean_code(self):
        code = self.cleaned_data['code'].strip()
        if not code.isdigit():
            raise forms.ValidationError('Use digits only, e.g. 6450.')
        return code


class JournalHeaderForm(CrmForm):
    entry_date = forms.DateField(widget=DateInput, label='Date')
    narration = forms.CharField(max_length=500, widget=forms.Textarea(attrs={'rows': 2}))
    message = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}), label='Message to approver',
                              help_text='Used if the journal is above the approval limit.')


class JournalLineForm(CrmForm):
    account = forms.ModelChoiceField(queryset=Account.objects.none())
    debit = MoneyFormField(required=False)
    credit = MoneyFormField(required=False)
    description = forms.CharField(required=False, max_length=300)
    client = forms.ModelChoiceField(queryset=Client.objects.none(), required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.none(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = Account.objects.filter(is_active=True, allow_manual=True).order_by('code')
        self.fields['client'].queryset = Client.objects.filter(archived_at__isnull=True).order_by('display_name')
        self.fields['project'].queryset = Project.objects.order_by('-id')

    def clean(self):
        c = super().clean()
        dr, cr = c.get('debit') or Decimal('0'), c.get('credit') or Decimal('0')
        if not c.get('account') and not dr and not cr:
            return c
        if dr and cr:
            raise forms.ValidationError('A line is either a debit or a credit, not both.')
        if not dr and not cr:
            raise forms.ValidationError('Enter a debit or a credit.')
        c['debit'], c['credit'] = dr, cr
        return c


class BaseJournalLines(forms.BaseFormSet):
    def clean(self):
        if any(self.errors):
            return
        live = [f.cleaned_data for f in self.forms if f.cleaned_data and f.cleaned_data.get('account')]
        if len(live) < 2:
            raise forms.ValidationError('A journal needs at least two lines.')
        dr = sum((d['debit'] for d in live), Decimal('0'))
        cr = sum((d['credit'] for d in live), Decimal('0'))
        if dr != cr:
            raise forms.ValidationError(f'Debits (₹{dr:,.2f}) and credits (₹{cr:,.2f}) must be equal.')


JournalLineFormSet = formset_factory(JournalLineForm, formset=BaseJournalLines, extra=0, min_num=2, max_num=60)


class ReasonDateForm(CrmForm):
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), min_length=10)
    on = forms.DateField(widget=DateInput, required=False, label='Reversal date', help_text='Defaults to today.')


class BankAccountForm(CrmForm):
    name = forms.CharField(max_length=120)
    kind = forms.ChoiceField(choices=BankAccount.KINDS)
    bank_name = forms.CharField(max_length=120, required=False)
    account_number = forms.CharField(max_length=30, required=False)
    ifsc = forms.CharField(max_length=11, required=False, label='IFSC')
    opening_balance = MoneyFormField(required=False, label='Opening balance')
    opening_date = forms.DateField(widget=DateInput, required=False)
    is_default_receipt = forms.BooleanField(required=False, label='Default for manual receipts')

    def clean_name(self):
        n = self.cleaned_data['name'].strip()
        if BankAccount.objects.filter(name__iexact=n).exists():
            raise forms.ValidationError('An account with this name exists.')
        return n

    def clean(self):
        c = super().clean()
        if c.get('opening_balance') and not c.get('opening_date'):
            self.add_error('opening_date', 'Give the date of the opening balance.')
        return c


class TransferForm(CrmForm):
    from_acct = forms.ModelChoiceField(queryset=BankAccount.objects.filter(is_active=True), label='From')
    to_acct = forms.ModelChoiceField(queryset=BankAccount.objects.filter(is_active=True), label='To')
    amount = MoneyFormField()
    on = forms.DateField(widget=DateInput, label='Date')
    note = forms.CharField(required=False, max_length=200)

    def clean(self):
        c = super().clean()
        if c.get('from_acct') and c.get('from_acct') == c.get('to_acct'):
            self.add_error('to_acct', 'Choose a different account.')
        if c.get('on') and c['on'] > india.today():
            self.add_error('on', 'The date cannot be in the future.')
        return c


class StatementImportForm(CrmForm):
    file = SafeFileField(allowed=('.csv', '.xlsx'), label='Bank statement (CSV or Excel)')


class CreateFromLineForm(CrmForm):
    counter_account = forms.ModelChoiceField(queryset=Account.objects.none(), label='Other side')
    narration = forms.CharField(required=False, max_length=300)

    def __init__(self, *args, exclude_account=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Account.objects.filter(is_active=True, allow_manual=True).order_by('code')
        if exclude_account:
            qs = qs.exclude(pk=exclude_account)
        self.fields['counter_account'].queryset = qs


class FinaliseForm(CrmForm):
    as_of = forms.DateField(widget=DateInput, label='Statement date')
    statement_balance = forms.DecimalField(max_digits=15, decimal_places=2, label='Statement closing balance')
    note = forms.CharField(required=False, max_length=300)


ACCOUNT_TYPE_CHOICES = ACCOUNT_TYPES
