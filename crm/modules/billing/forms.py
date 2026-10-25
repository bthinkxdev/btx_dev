"""Invoice builder and billing popups (Part 7.4, 7.6-7.8)."""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory

from crm.core import india
from crm.core.forms import CrmForm, CrmModelForm, DateInput, GSTINField, MoneyFormField, VersionedModelFormMixin
from crm.models import Invoice, InvoiceLine, TdsReceivable
from crm.models.billing import CREDIT_REASONS
from crm.modules.quotations.forms import BaseLineFormSet, LineForm


class InvoiceForm(VersionedModelFormMixin, CrmModelForm):
    customer_gstin = GSTINField(label='Customer GSTIN')
    discount_value = forms.DecimalField(required=False, min_value=0, max_digits=15, decimal_places=2, label='Overall discount')
    tds_expected = MoneyFormField(required=False, label='TDS the customer will deduct')

    class Meta:
        model = Invoice
        fields = ['owner', 'invoice_date', 'due_date', 'po_number', 'milestone_ref', 'customer_name', 'customer_type',
                  'customer_gstin', 'customer_state', 'billing_address', 'shipping_address', 'place_of_supply', 'price_mode',
                  'discount_mode', 'discount_value', 'discount_reason', 'tds_expected', 'notes', 'terms', 'internal_notes']
        widgets = {'invoice_date': DateInput, 'due_date': DateInput, 'billing_address': forms.Textarea(attrs={'rows': 2}),
                   'shipping_address': forms.Textarea(attrs={'rows': 2}), 'notes': forms.Textarea(attrs={'rows': 2}),
                   'terms': forms.Textarea(attrs={'rows': 3}), 'internal_notes': forms.Textarea(attrs={'rows': 2})}
        labels = {'milestone_ref': 'Milestone / reference', 'place_of_supply': 'Place of supply'}

    def __init__(self, *args, user=None, can_change_owner=False, **kwargs):
        super().__init__(*args, **kwargs)
        from django.contrib.auth import get_user_model
        from crm.modules.leads.forms import assignable_users
        self.fields['owner'].queryset = assignable_users(user) if can_change_owner else get_user_model().objects.filter(pk=user.pk)
        self.fields['owner'].label_from_instance = lambda u: u.get_full_name() or u.username
        self.fields['owner'].empty_label = None
        self.fields['place_of_supply'].required = False

    def clean(self):
        c = super().clean()
        if c.get('due_date') and c.get('invoice_date') and c['due_date'] < c['invoice_date']:
            self.add_error('due_date', 'The due date cannot be before the invoice date.')
        if c.get('customer_type') == 'registered' and not c.get('customer_gstin'):
            self.add_error('customer_gstin', 'A registered (B2B) customer needs a GSTIN.')
        gstin, state = c.get('customer_gstin'), c.get('customer_state')
        if gstin and state and gstin[:2] != state:
            self.add_error('customer_state', 'The state must match the first two digits of the GSTIN.')
        if not c.get('place_of_supply'):
            c['place_of_supply'] = c.get('customer_state') or ('96' if c.get('customer_type') == 'overseas' else '')
        c['discount_value'] = c.get('discount_value') or Decimal('0')
        c['tds_expected'] = c.get('tds_expected') or Decimal('0')
        if c.get('discount_mode') == 'percent' and c['discount_value'] > 100:
            self.add_error('discount_value', 'Discount cannot be more than 100 percent.')
        if c['discount_value'] and not c.get('discount_mode'):
            self.add_error('discount_mode', 'Choose percent or amount.')
        from crm.core.posting import period_status
        if c.get('invoice_date') and period_status(c['invoice_date']) == 'closed':
            self.add_error('invoice_date', 'This date is in a closed accounting period.')
        return c


class InvoiceLineForm(LineForm):
    class Meta(LineForm.Meta):
        model = InvoiceLine


InvoiceLineFormSet = inlineformset_factory(Invoice, InvoiceLine, form=InvoiceLineForm, formset=BaseLineFormSet, extra=0,
                                           can_delete=True, min_num=0)


class SendInvoiceForm(CrmForm):
    to = forms.EmailField(label='To')
    cc = forms.CharField(required=False, label='CC', help_text='Separate addresses with commas.')
    message = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))
    include_link = forms.BooleanField(required=False, initial=True, label='Include the active payment link')

    def clean_cc(self):
        from django.core.validators import validate_email
        raw = [x.strip() for x in (self.cleaned_data.get('cc') or '').split(',') if x.strip()]
        for e in raw:
            try:
                validate_email(e)
            except forms.ValidationError:
                raise forms.ValidationError(f'"{e}" is not a valid email address.')
        return raw


class CreditNoteForm(CrmForm):
    taxable = MoneyFormField(label='Taxable value to credit', help_text='Tax is recomputed in the same proportion as the invoice.')
    reason = forms.ChoiceField(choices=[('', 'Choose a reason')] + CREDIT_REASONS)
    treatment = forms.ChoiceField(choices=[('reduce_balance', 'Reduce the open balance'), ('refund', 'Refund money already paid')],
                                  initial='reduce_balance', widget=forms.RadioSelect, label='Treatment')
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, invoice=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.invoice = invoice

    def clean(self):
        c = super().clean()
        t = c.get('taxable')
        if t is not None and t <= 0:
            self.add_error('taxable', 'Enter an amount above ₹0.')
        if self.invoice is not None and t and t > self.invoice.taxable_total:
            self.add_error('taxable', f'Cannot exceed the invoice taxable value (₹{self.invoice.taxable_total:,.2f}).')
        if c.get('reason') == 'other' and len((c.get('notes') or '').strip()) < 5:
            self.add_error('notes', 'Add a note when the reason is Other.')
        return c


class PromiseForm(CrmForm):
    date = forms.DateField(widget=DateInput, label='Promised date')
    amount = MoneyFormField(label='Promised amount')
    note = forms.CharField(required=False, max_length=300, widget=forms.Textarea(attrs={'rows': 2}))

    def clean_date(self):
        d = self.cleaned_data['date']
        if d < india.today():
            raise forms.ValidationError('The promise date cannot be in the past.')
        return d


class DisputeForm(CrmForm):
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), min_length=5, max_length=300, label='What is disputed')


class StatementForm(CrmForm):
    start = forms.DateField(widget=DateInput, label='From')
    end = forms.DateField(widget=DateInput, label='To')

    def clean(self):
        c = super().clean()
        if c.get('start') and c.get('end') and c['end'] < c['start']:
            self.add_error('end', 'The end date cannot be before the start date.')
        return c


class TdsUpdateForm(CrmForm):
    status = forms.ChoiceField(choices=[s for s in TdsReceivable.STATUSES if s[0] != 'written_off'])
    certificate_number = forms.CharField(required=False, max_length=40, label='Form 16A number')
    certificate_date = forms.DateField(required=False, widget=DateInput, label='Certificate date')
    note = forms.CharField(required=False, max_length=300)

    def clean(self):
        c = super().clean()
        if c.get('status') in ('received', 'claimed') and not c.get('certificate_number'):
            self.add_error('certificate_number', 'Enter the Form 16A number.')
        if c.get('certificate_date') and c['certificate_date'] > india.today():
            self.add_error('certificate_date', 'The date cannot be in the future.')
        return c
