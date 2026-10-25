"""Forms for payment links, manual payments, refunds and settlements (Part 6.5, 6.9, 6.10)."""
from __future__ import annotations

from datetime import timedelta

from django import forms
from django.utils import timezone

from crm.core import india
from crm.core.forms import (CrmForm, DateInput, DateTimeInput, MoneyFormField, PhoneField, SafeFileField,
                            future_datetime)
from crm.models import BankAccount, Client, Invoice, Refund
from crm.models.clients import TDS_SECTIONS
from crm.models.payments import GATEWAY_METHODS, METHODS, PURPOSES

METHOD_LABELS = dict(METHODS)


class LinkForm(CrmForm):
    purpose = forms.ChoiceField(choices=PURPOSES, widget=forms.RadioSelect, label='Collection type')
    milestone = forms.ChoiceField(required=False, label='Milestone')
    amount = MoneyFormField(label='Amount to collect')
    description = forms.CharField(max_length=200, label='Customer-visible description')
    expires_at = forms.DateTimeField(widget=DateTimeInput, validators=[future_datetime], label='Link expiry')
    allow_partial = forms.BooleanField(required=False, label='Allow partial payments by customer')
    min_partial_amount = MoneyFormField(required=False, label='Minimum partial amount')
    methods = forms.MultipleChoiceField(choices=[], widget=forms.CheckboxSelectMultiple, label='Payment methods')
    contact_name = forms.CharField(max_length=120, required=False, label='Contact name')
    contact_email = forms.EmailField(required=False, label='Contact email')
    contact_phone = PhoneField(required=False, label='Contact phone')
    send_now = forms.BooleanField(required=False, initial=True, label='Email the link to the contact now')
    internal_note = forms.CharField(required=False, max_length=500, widget=forms.Textarea(attrs={'rows': 2}),
                                    label='Internal note', help_text='Never shown to the customer.')
    request_message = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}), label='Message to approver')
    allow_multiple = forms.BooleanField(required=False, label='Allow another active link for this source')
    confirm = forms.BooleanField(label='I confirm the customer will pay this amount')

    def __init__(self, *args, enabled_methods=None, milestones=None, collectable=None, can_multi=False, **kwargs):
        super().__init__(*args, **kwargs)
        enabled = enabled_methods or GATEWAY_METHODS
        self.fields['methods'].choices = [(m, METHOD_LABELS.get(m, m)) for m in enabled]
        self.fields['methods'].initial = list(enabled)
        self.milestones = milestones or []
        if self.milestones:
            self.fields['milestone'].choices = [('', 'Choose a milestone')] + [(str(m.pk), f'{m.name} ({m.percent.normalize():f}%)') for m in self.milestones]
        else:
            del self.fields['milestone']
            self.fields['purpose'].choices = [c for c in PURPOSES if c[0] != 'milestone']
        self.collectable = collectable
        if not can_multi:
            del self.fields['allow_multiple']

    def clean(self):
        c = super().clean()
        amt = c.get('amount')
        if amt is not None:
            if amt <= 0:
                self.add_error('amount', 'Enter an amount above ₹0.')
            elif self.collectable is not None and amt > self.collectable:
                self.add_error('amount', f'Maximum collectable is ₹{self.collectable:,.2f}.')
        if c.get('purpose') == 'milestone' and not c.get('milestone'):
            self.add_error('milestone', 'Choose the milestone.')
        if c.get('allow_partial'):
            mp = c.get('min_partial_amount')
            if mp is not None and amt is not None and mp > amt:
                self.add_error('min_partial_amount', 'The minimum cannot be more than the amount.')
        if not c.get('methods'):
            self.add_error('methods', 'Choose at least one payment method.')
        if c.get('send_now') and not c.get('contact_email'):
            self.add_error('contact_email', 'Enter an email to send the link now, or untick "Email the link".')
        if not (c.get('contact_email') or c.get('contact_phone')):
            self.add_error('contact_email', 'The contact must have a phone number or email.')
        return c


class SendLinkForm(CrmForm):
    to = forms.EmailField(label='To')
    message = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}), label='Message')


class ExtensionForm(CrmForm):
    new_expiry = forms.DateTimeField(widget=DateTimeInput, validators=[future_datetime], label='New expiry')
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), min_length=10, label='Reason')


class ManualPaymentForm(CrmForm):
    client = forms.ModelChoiceField(queryset=Client.objects.none(), label='Client')
    invoice = forms.ModelChoiceField(queryset=Invoice.objects.none(), required=False, label='Against invoice',
                                     help_text='Leave empty to hold as an advance.')
    amount = MoneyFormField(label='Amount received')
    payment_date = forms.DateField(widget=DateInput, label='Payment date')
    method = forms.ChoiceField(choices=[m for m in METHODS if m[0] not in ('card', 'wallet', 'netbanking')], initial='bank_transfer')
    reference_number = forms.CharField(max_length=80, required=False, label='Reference / UTR / cheque no.')
    deposit_account = forms.ModelChoiceField(queryset=BankAccount.objects.filter(is_active=True).exclude(kind='gateway'),
                                             label='Deposited to')
    tds_amount = MoneyFormField(required=False, label='TDS deducted by customer')
    tds_section = forms.ChoiceField(choices=[('', '—')] + list(TDS_SECTIONS), required=False, label='TDS section')
    deductor_tan = forms.CharField(max_length=10, required=False, label='Deductor TAN')
    proof = SafeFileField(required=False, allowed=('.pdf', '.png', '.jpg', '.jpeg', '.webp'), label='Proof')
    note = forms.CharField(required=False, max_length=500, widget=forms.Textarea(attrs={'rows': 2}), label='Internal note')

    def __init__(self, *args, clients=None, client=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['client'].queryset = clients if clients is not None else Client.objects.filter(archived_at__isnull=True)
        cid = self.data.get('client') if self.is_bound else (client.pk if client else self.initial.get('client'))
        if hasattr(cid, 'pk'):
            cid = cid.pk
        self.fields['invoice'].queryset = Invoice.objects.filter(client_id=cid, status__in=['issued', 'partially_paid'],
                                                                 balance__gt=0) if cid else Invoice.objects.none()
        self.fields['invoice'].label_from_instance = lambda i: f'{i.number} · balance ₹{i.balance:,.2f} · due {i.due_date:%d-%b-%Y}'
        default = BankAccount.objects.filter(is_default_receipt=True, is_active=True).first()
        if default and not self.is_bound:
            self.initial.setdefault('deposit_account', default.pk)
        self.initial.setdefault('payment_date', india.today())

    def clean(self):
        c = super().clean()
        if c.get('payment_date') and c['payment_date'] > india.today():
            self.add_error('payment_date', 'The payment date cannot be in the future.')
        if c.get('method') in ('bank_transfer', 'upi', 'cheque') and not c.get('reference_number'):
            self.add_error('reference_number', 'Required for bank transfer, UPI and cheque.')
        acct = c.get('deposit_account')
        if acct and c.get('method') == 'cash' and acct.kind != 'cash':
            self.add_error('deposit_account', 'Cash goes to a cash account.')
        if acct and c.get('method') != 'cash' and acct.kind == 'cash':
            self.add_error('deposit_account', 'Choose a bank account.')
        if c.get('tds_amount') and not c.get('tds_section'):
            self.add_error('tds_section', 'Choose the TDS section.')
        inv = c.get('invoice')
        if inv and c.get('amount') is not None:
            gross = c['amount'] + (c.get('tds_amount') or 0)
            if gross > inv.balance:
                self.add_error('amount', f'Amount plus TDS (₹{gross:,.2f}) is more than the invoice balance (₹{inv.balance:,.2f}). '
                                         'Leave the invoice empty to hold the extra as an advance, or split it later.')
        return c


class RefundForm(CrmForm):
    amount = MoneyFormField(label='Refund amount')
    reason = forms.ChoiceField(choices=[('', 'Choose a reason')] + Refund.REASONS)
    method = forms.ChoiceField(choices=Refund._meta.get_field('method').choices, initial='gateway', label='Refund method')
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, gateway_ok=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not gateway_ok:
            self.fields['method'].choices = [('manual', 'Manual')]
            self.fields['method'].initial = 'manual'


class ManualRefundForm(CrmForm):
    reference = forms.CharField(max_length=80, label='Bank / UPI reference of the refund')


class SettlementForm(CrmForm):
    settlement_id = forms.CharField(max_length=80, label='Settlement ID')
    settled_on = forms.DateField(widget=DateInput, label='Settled on')
    gross = MoneyFormField(label='Gross collected')
    fees = MoneyFormField(required=False, label='Gateway fees')
    tax_on_fees = MoneyFormField(required=False, label='GST on fees')
    tds = MoneyFormField(required=False, label='TDS / TCS deducted')
    bank_account = forms.ModelChoiceField(queryset=BankAccount.objects.filter(is_active=True, kind='bank'), label='Paid into')

    def clean(self):
        c = super().clean()
        for k in ('fees', 'tax_on_fees', 'tds'):
            c[k] = c.get(k) or 0
        if c.get('gross') is not None and c['fees'] + c['tax_on_fees'] + c['tds'] > c['gross']:
            self.add_error('fees', 'Fees and deductions cannot exceed the gross amount.')
        if c.get('settled_on') and c['settled_on'] > india.today():
            self.add_error('settled_on', 'The date cannot be in the future.')
        return c


class DisputeForm(CrmForm):
    status = forms.ChoiceField(choices=[('open', 'Open'), ('evidence_submitted', 'Evidence submitted'), ('won', 'Won'), ('lost', 'Lost')])
    note = forms.CharField(required=False, max_length=300, label='Note')


def default_expiry(days: int):
    return (timezone.localtime() + timedelta(days=days)).replace(second=0, microsecond=0)
