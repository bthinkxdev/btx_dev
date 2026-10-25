"""Quotation builder forms (Part 5.6-5.7)."""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from crm.core import india
from crm.core.forms import CrmForm, CrmModelForm, DateInput, GSTINField, MoneyFormField, PhoneField, StyledFormMixin, VersionedModelFormMixin
from crm.core.gst import GST_RATE_CHOICES, TAXABILITY_CHOICES
from crm.models import Quotation, QuotationLine, QuotationMilestone, Service
from crm.models.quotations import REJECTION_REASONS


class QuotationForm(VersionedModelFormMixin, CrmModelForm):
    customer_gstin = GSTINField(label='Customer GSTIN')
    contact_phone = PhoneField(required=False, label='Contact phone')
    discount_value = forms.DecimalField(required=False, min_value=0, max_digits=15, decimal_places=2, label='Overall discount')
    cost_estimate = MoneyFormField(required=False, label='Estimated delivery cost')

    class Meta:
        model = Quotation
        fields = ['title', 'reference', 'quotation_date', 'valid_until', 'owner', 'contact_name', 'contact_email',
                  'contact_phone', 'customer_name', 'customer_type', 'customer_gstin', 'customer_state', 'billing_address',
                  'place_of_supply', 'price_mode', 'discount_mode', 'discount_value', 'discount_reason', 'advance_percent',
                  'payment_terms_days', 'balance_terms', 'late_fee_text', 'deliverables', 'exclusions', 'assumptions',
                  'timeline', 'revision_limits', 'terms', 'signatory_name', 'internal_notes', 'cost_estimate']
        widgets = {
            'quotation_date': DateInput, 'valid_until': DateInput,
            'billing_address': forms.Textarea(attrs={'rows': 2}), 'deliverables': forms.Textarea(attrs={'rows': 3}),
            'exclusions': forms.Textarea(attrs={'rows': 2}), 'assumptions': forms.Textarea(attrs={'rows': 2}),
            'terms': forms.Textarea(attrs={'rows': 5}), 'internal_notes': forms.Textarea(attrs={'rows': 2}),
        }
        labels = {'place_of_supply': 'Place of supply', 'customer_state': 'Customer state', 'advance_percent': 'Advance %'}

    def __init__(self, *args, user=None, can_see_cost=True, can_change_owner=False, standard_terms='', **kwargs):
        super().__init__(*args, **kwargs)
        from django.contrib.auth import get_user_model
        from crm.modules.leads.forms import assignable_users
        self.standard_terms = standard_terms
        self.fields['owner'].queryset = assignable_users(user) if can_change_owner else get_user_model().objects.filter(pk=user.pk)
        self.fields['owner'].label_from_instance = lambda u: u.get_full_name() or u.username
        self.fields['owner'].empty_label = None
        if not can_see_cost:
            del self.fields['cost_estimate']
        self.fields['place_of_supply'].required = False
        self.fields['place_of_supply'].help_text = 'Defaults to the customer state.'

    def clean(self):
        c = super().clean()
        if c.get('valid_until') and c.get('quotation_date') and c['valid_until'] < c['quotation_date']:
            self.add_error('valid_until', 'Validity cannot end before the quotation date.')
        if c.get('valid_until') and c['valid_until'] < india.today():
            self.add_error('valid_until', 'Choose a validity date that is not in the past.')
        if c.get('customer_type') == 'registered' and not c.get('customer_gstin'):
            self.add_error('customer_gstin', 'A registered (B2B) customer needs a GSTIN.')
        gstin, state = c.get('customer_gstin'), c.get('customer_state')
        if gstin and state and gstin[:2] != state:
            self.add_error('customer_state', 'The state must match the first two digits of the GSTIN.')
        if not c.get('place_of_supply'):
            c['place_of_supply'] = c.get('customer_state') or ('96' if c.get('customer_type') == 'overseas' else '')
        if c.get('discount_value') is None:
            c['discount_value'] = Decimal('0')
        if c.get('discount_mode') == 'percent' and c['discount_value'] > 100:
            self.add_error('discount_value', 'Discount cannot be more than 100 percent.')
        if c.get('discount_value') and not c.get('discount_mode'):
            self.add_error('discount_mode', 'Choose percent or amount.')
        adv = c.get('advance_percent')
        if adv is not None and not (0 <= adv <= 100):
            self.add_error('advance_percent', 'Advance must be between 0 and 100 percent.')
        return c


class LineForm(StyledFormMixin, forms.ModelForm):
    tax_rate = forms.TypedChoiceField(choices=GST_RATE_CHOICES, coerce=Decimal, initial='18')
    discount_value = forms.DecimalField(required=False, min_value=0, max_digits=15, decimal_places=2)

    class Meta:
        model = QuotationLine
        fields = ['service', 'description', 'sac_code', 'quantity', 'unit', 'unit_price', 'discount_mode',
                  'discount_value', 'taxability', 'tax_rate']
        widgets = {'description': forms.TextInput(attrs={'placeholder': 'What is being delivered'})}

    def __init__(self, *args, can_override=False, on_date=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.can_override = can_override
        self.on_date = on_date
        self.fields['service'].queryset = Service.objects.filter(is_active=True) if not self.instance.pk else \
            Service.objects.filter(pk=self.instance.service_id) | Service.objects.filter(is_active=True)
        self.fields['service'].required = False
        if self.instance.pk and not self.is_bound:
            # Stored 18.00 must select the "18" choice, never fall back to the first option.
            self.initial['tax_rate'] = f'{self.instance.tax_rate.normalize():f}'
        self.fields['quantity'].widget.attrs.update({'step': '0.01', 'min': '0.01'})
        self.fields['unit_price'].widget.attrs.update({'step': '0.01', 'min': '0'})

    def clean(self):
        c = super().clean()
        if c.get('DELETE'):
            return c
        svc = c.get('service')
        if c.get('quantity') is not None and c['quantity'] <= 0:
            self.add_error('quantity', 'Quantity must be more than 0.')
        if c.get('unit_price') is not None and c['unit_price'] < 0:
            self.add_error('unit_price', 'Price cannot be negative.')
        if c.get('discount_value') is None:
            c['discount_value'] = Decimal('0')
        if c.get('discount_value') and not c.get('discount_mode'):
            self.add_error('discount_mode', 'Choose percent or amount.')
        if svc is not None:
            if not c.get('description'):
                c['description'] = svc.name
            if not c.get('sac_code'):
                c['sac_code'] = svc.sac_code
            if not self.can_override:
                # Tax comes from the effective-dated service rule; only an override (with approval) changes it.
                taxability, rate = svc.tax_for(self.on_date)
                c['taxability'], c['tax_rate'] = taxability, rate
        if c.get('taxability') == 'taxable' and not c.get('sac_code'):
            self.add_error('sac_code', 'A taxable line needs a SAC or HSN code.')
        return c

    def save(self, commit=True):
        obj = super().save(commit=False)
        if obj.service_id and obj.catalogue_price is None:
            obj.catalogue_price = obj.service.default_price
        if commit:
            obj.save()
        return obj


class BaseLineFormSet(BaseInlineFormSet):
    def __init__(self, *args, can_override=False, on_date=None, **kwargs):
        self.can_override, self.on_date = can_override, on_date
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kw = super().get_form_kwargs(index)
        kw.update(can_override=self.can_override, on_date=self.on_date)
        return kw

    def clean(self):
        super().clean()
        live = [f for f in self.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        if not live:
            raise forms.ValidationError('Add at least one line item.')


LineFormSet = inlineformset_factory(Quotation, QuotationLine, form=LineForm, formset=BaseLineFormSet, extra=0,
                                    can_delete=True, min_num=0)


class MilestoneForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = QuotationMilestone
        fields = ['name', 'percent', 'trigger', 'due_rule']


class BaseMilestoneFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        live = [f.cleaned_data for f in self.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        if live:
            total = sum((d.get('percent') or Decimal('0') for d in live), Decimal('0'))
            if total != Decimal('100'):
                raise forms.ValidationError(f'Milestone percentages must add up to 100. They add up to {total.normalize():f}.')


MilestoneFormSet = inlineformset_factory(Quotation, QuotationMilestone, form=MilestoneForm, formset=BaseMilestoneFormSet,
                                         extra=0, can_delete=True)


class SendForm(CrmForm):
    channel = forms.ChoiceField(choices=[('email', 'Email with PDF and link'), ('link', 'Share link myself (mark as sent)')],
                                initial='email', widget=forms.RadioSelect)
    to = forms.EmailField(required=False, label='To')
    cc = forms.CharField(required=False, label='CC', help_text='Separate addresses with commas.')
    subject = forms.CharField(required=False, max_length=200)
    message = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}), label='Message')

    def clean_cc(self):
        from django.core.validators import validate_email
        raw = [x.strip() for x in (self.cleaned_data.get('cc') or '').split(',') if x.strip()]
        for e in raw:
            try:
                validate_email(e)
            except forms.ValidationError:
                raise forms.ValidationError(f'"{e}" is not a valid email address.')
        return raw

    def clean(self):
        c = super().clean()
        if c.get('channel') == 'email' and not c.get('to'):
            self.add_error('to', 'Enter the email address to send to.')
        return c


class AcceptForm(CrmForm):
    accepted_by = forms.CharField(max_length=120, label='Accepted by')
    accepted_on = forms.DateField(widget=DateInput, label='Date')
    evidence = forms.ChoiceField(choices=[('email', 'Email from customer'), ('signed_pdf', 'Signed PDF'), ('verbal', 'Verbal (note required)'),
                                          ('purchase_order', 'Purchase order')])
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def clean(self):
        c = super().clean()
        if c.get('evidence') == 'verbal' and len((c.get('note') or '').strip()) < 10:
            self.add_error('note', 'Describe the verbal acceptance (who and when).')
        if c.get('accepted_on') and c['accepted_on'] > india.today():
            self.add_error('accepted_on', 'The date cannot be in the future.')
        return c


class RejectForm(CrmForm):
    reason = forms.ChoiceField(choices=[('', 'Choose a reason')] + REJECTION_REASONS)
    competitor = forms.CharField(required=False, max_length=120)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))


class ApprovalRequestForm(CrmForm):
    message = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}), label='Message to approver')


class GstOverrideForm(CrmForm):
    taxability = forms.ChoiceField(choices=TAXABILITY_CHOICES, label='Tax treatment')
    rate = forms.TypedChoiceField(choices=GST_RATE_CHOICES, coerce=Decimal, label='Rate')
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), min_length=10, label='Reason (CA reference)')
