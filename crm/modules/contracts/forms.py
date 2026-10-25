"""Contract builder and lifecycle popups (Part 9.5)."""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from crm.core import india
from crm.core.forms import CrmForm, CrmModelForm, DateInput, MoneyFormField, StyledFormMixin, VersionedModelFormMixin
from crm.core.gst import GST_RATE_CHOICES
from crm.models import Contract, ContractLine, Service


class ContractForm(VersionedModelFormMixin, CrmModelForm):
    class Meta:
        model = Contract
        fields = ['title', 'owner', 'start_date', 'end_date', 'auto_renew', 'notice_days', 'invoice_policy', 'payment_terms_days',
                  'scope', 'notes']
        widgets = {'start_date': DateInput, 'end_date': DateInput, 'scope': forms.Textarea(attrs={'rows': 4}),
                   'notes': forms.Textarea(attrs={'rows': 2})}
        labels = {'end_date': 'End date (blank = open-ended)', 'notice_days': 'Notice period (days)'}

    def __init__(self, *args, user=None, can_change_owner=False, **kwargs):
        super().__init__(*args, **kwargs)
        from django.contrib.auth import get_user_model
        from crm.modules.leads.forms import assignable_users
        self.fields['owner'].queryset = assignable_users(user) if can_change_owner else get_user_model().objects.filter(pk=user.pk)
        self.fields['owner'].label_from_instance = lambda u: u.get_full_name() or u.username
        self.fields['owner'].empty_label = None

    def clean(self):
        c = super().clean()
        if c.get('start_date') and c.get('end_date') and c['end_date'] < c['start_date']:
            self.add_error('end_date', 'The end date cannot be before the start date.')
        return c


class ContractLineForm(StyledFormMixin, forms.ModelForm):
    tax_rate = forms.TypedChoiceField(choices=GST_RATE_CHOICES, coerce=Decimal, initial='18')

    class Meta:
        model = ContractLine
        fields = ['service', 'description', 'quantity', 'unit_price', 'billing_model', 'start_date', 'end_date', 'taxability',
                  'tax_rate', 'sac_code', 'is_paused']
        widgets = {'start_date': DateInput, 'end_date': DateInput}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['service'].queryset = Service.objects.filter(is_active=True)
        self.fields['service'].required = False
        if self.instance.pk and not self.is_bound:
            self.initial['tax_rate'] = f'{self.instance.tax_rate.normalize():f}'

    def clean(self):
        c = super().clean()
        if c.get('DELETE'):
            return c
        if c.get('quantity') is not None and c['quantity'] <= 0:
            self.add_error('quantity', 'Quantity must be more than 0.')
        if c.get('unit_price') is not None and c['unit_price'] <= 0:
            self.add_error('unit_price', 'Price must be more than 0.')
        if c.get('start_date') and c.get('end_date') and c['end_date'] < c['start_date']:
            self.add_error('end_date', 'Cannot end before it starts.')
        s = c.get('service')
        if s is not None:
            c['description'] = c.get('description') or s.name
            c['sac_code'] = c.get('sac_code') or s.sac_code
        return c


class BaseContractLines(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if not [f for f in self.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]:
            raise forms.ValidationError('Add at least one contract line.')


ContractLineFormSet = inlineformset_factory(Contract, ContractLine, form=ContractLineForm, formset=BaseContractLines, extra=0,
                                            can_delete=True)


class AmendForm(CrmForm):
    effective_from = forms.DateField(widget=DateInput, label='Effective from')
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), min_length=10)


class RenewForm(CrmForm):
    new_end = forms.DateField(widget=DateInput, label='New end date')
    price_change_percent = forms.DecimalField(max_digits=5, decimal_places=2, required=False, initial=0, label='Price change %',
                                              help_text='For example 8 for an 8% increase. Applies to recurring lines.')
    note = forms.CharField(required=False, max_length=300, label='Acceptance note')


class TerminateForm(CrmForm):
    on = forms.DateField(widget=DateInput, label='Effective date')
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), min_length=10)


class ScheduleRowForm(CrmForm):
    invoice_date = forms.DateField(widget=DateInput)
    amount = MoneyFormField()

    def clean_invoice_date(self):
        d = self.cleaned_data['invoice_date']
        if d < india.today():
            raise forms.ValidationError('Cannot be in the past.')
        return d
