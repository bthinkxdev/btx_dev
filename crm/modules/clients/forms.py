from __future__ import annotations

from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model

from crm.core.forms import (CrmModelForm, DateInput, GSTINField, MoneyFormField, PANField, PhoneField,
                            VersionedModelFormMixin)
from crm.models import Client, Contact, Service, ServiceTaxRule


class ClientForm(VersionedModelFormMixin, CrmModelForm):
    gstin = GSTINField(label='GSTIN')
    pan = PANField(label='PAN')
    phone = PhoneField(required=False)
    credit_limit = MoneyFormField(required=False)

    class Meta:
        model = Client
        fields = ['legal_name', 'display_name', 'client_type', 'industry', 'website', 'phone', 'email', 'gstin', 'pan',
                  'customer_type', 'tds_applicable', 'tds_section', 'billing_line1', 'billing_line2', 'billing_city',
                  'billing_state', 'billing_pincode', 'billing_country', 'shipping_same', 'shipping_line1',
                  'shipping_line2', 'shipping_city', 'shipping_state', 'shipping_pincode', 'owner', 'credit_limit',
                  'payment_terms_days', 'dunning_paused', 'notes']
        widgets = {'notes': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, user=None, can_see_tax=True, **kwargs):
        super().__init__(*args, **kwargs)
        User = get_user_model()
        self.fields['owner'].queryset = User.objects.filter(is_active=True)
        self.fields['owner'].label_from_instance = lambda u: u.get_full_name() or u.username
        if not self.instance.pk and user is not None:
            self.initial.setdefault('owner', user.pk)
        self.fields['display_name'].required = False
        self.fields['display_name'].help_text = 'Defaults to the legal name.'
        if not can_see_tax and self.instance.pk:
            # Tax identifiers stay as they are for users without the tax_ids field group.
            for f in ('gstin', 'pan'):
                self.fields[f].disabled = True
                self.fields[f].help_text = 'Hidden for your role.'

    def clean(self):
        c = super().clean()
        if not c.get('display_name'):
            c['display_name'] = c.get('legal_name', '')
        ct, gstin, state = c.get('customer_type'), c.get('gstin'), c.get('billing_state')
        if ct == 'registered' and not gstin:
            self.add_error('gstin', 'A registered (B2B) customer needs a GSTIN.')
        if gstin and state and gstin[:2] != state:
            self.add_error('billing_state', 'The billing state must match the first two digits of the GSTIN.')
        if ct == 'overseas':
            c['billing_state'] = c.get('billing_state') or '96'
        if not c.get('shipping_same') and not c.get('shipping_line1'):
            self.add_error('shipping_line1', 'Add the shipping address or tick "same as billing".')
        return c


class ContactForm(CrmModelForm):
    phone = PhoneField(required=False)

    class Meta:
        model = Contact
        fields = ['name', 'designation', 'phone', 'email', 'preferred_channel', 'is_primary', 'is_billing',
                  'is_decision_maker']

    def clean(self):
        c = super().clean()
        if not c.get('phone') and not c.get('email'):
            self.add_error('phone', 'Enter a phone number or an email address.')
        return c


class ServiceForm(VersionedModelFormMixin, CrmModelForm):
    default_price = MoneyFormField()
    floor_price = MoneyFormField(required=False)
    cost_estimate = MoneyFormField(required=False)

    class Meta:
        model = Service
        fields = ['name', 'code', 'category', 'description', 'deliverables', 'sac_code', 'taxability', 'tax_rate',
                  'default_price', 'price_mode', 'floor_price', 'cost_estimate', 'unit', 'billing_model',
                  'revenue_recognition', 'estimated_hours', 'task_template', 'is_active']
        widgets = {'description': forms.Textarea(attrs={'rows': 3}), 'deliverables': forms.Textarea(attrs={'rows': 3}),
                   'task_template': forms.Textarea(attrs={'rows': 4})}

    def __init__(self, *args, can_see_cost=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_see_cost:
            del self.fields['cost_estimate']

    def clean_sac_code(self):
        from crm.core.india import SAC_RE
        v = (self.cleaned_data.get('sac_code') or '').strip()
        if v and not SAC_RE.match(v):
            raise forms.ValidationError('Enter a 4 to 8 digit SAC or HSN code.')
        return v

    def clean(self):
        c = super().clean()
        if c.get('taxability') == 'taxable' and not c.get('sac_code'):
            self.add_error('sac_code', 'A taxable service needs a SAC or HSN code.')
        if c.get('floor_price') and c.get('default_price') is not None and c['floor_price'] > c['default_price']:
            self.add_error('floor_price', 'The floor price cannot be above the default price.')
        if c.get('floor_price') is None:
            c['floor_price'] = Decimal('0.00')
        if 'cost_estimate' in self.fields and c.get('cost_estimate') is None:
            c['cost_estimate'] = Decimal('0.00')
        return c


class TaxRuleForm(CrmModelForm):
    class Meta:
        model = ServiceTaxRule
        fields = ['taxability', 'rate', 'effective_from', 'effective_to', 'reason']
        widgets = {'effective_from': DateInput, 'effective_to': DateInput}

    def clean(self):
        c = super().clean()
        if c.get('effective_to') and c.get('effective_from') and c['effective_to'] < c['effective_from']:
            self.add_error('effective_to', 'The end date cannot be before the start date.')
        if len((c.get('reason') or '').strip()) < 5:
            self.add_error('reason', 'Tax rule changes need a reason (CA sign-off reference).')
        return c
