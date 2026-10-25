"""Administration forms (Part 13.6)."""
from __future__ import annotations

import re

from django import forms
from django.contrib.auth import get_user_model

from crm.core import india
from crm.core.forms import CrmForm, CrmModelForm, VersionedModelFormMixin
from crm.models import CompanySettings, Department, ExpenseCategory, MessageTemplate, Role, SequenceSeries, Team


def _users():
    return get_user_model().objects.filter(is_active=True, crm_profile__isnull=False).order_by('first_name', 'username')


class UserChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name() or obj.username


class CompanyForm(VersionedModelFormMixin, CrmModelForm):
    reason = forms.CharField(required=False, max_length=300, label='Reason (needed for legal, tax and bank details)')

    class Meta:
        model = CompanySettings
        fields = ['legal_name', 'display_name', 'gstin', 'pan', 'state_code', 'address_line1', 'address_line2', 'city', 'pincode',
                  'phone', 'email', 'website', 'bank_name', 'bank_account_name', 'bank_account_number', 'bank_ifsc', 'bank_branch',
                  'upi_id', 'signatory_name', 'signatory_designation', 'quotation_validity_days', 'invoice_due_days',
                  'quotation_terms', 'invoice_terms', 'refund_policy']
        widgets = {'quotation_terms': forms.Textarea(attrs={'rows': 4}), 'invoice_terms': forms.Textarea(attrs={'rows': 3}),
                   'refund_policy': forms.Textarea(attrs={'rows': 3})}

    def clean_gstin(self):
        v = (self.cleaned_data.get('gstin') or '').strip().upper()
        if v:
            err = india.validate_gstin(v)
            if err:
                raise forms.ValidationError(err)
            state = self.data.get('state_code') or self.instance.state_code
            if v[:2] != state:
                raise forms.ValidationError('The first two digits of the GSTIN must match the company state.')
        return v

    def clean_pan(self):
        v = (self.cleaned_data.get('pan') or '').strip().upper()
        if v:
            err = india.validate_pan(v)
            if err:
                raise forms.ValidationError(err)
        return v

    def clean_bank_ifsc(self):
        v = (self.cleaned_data.get('bank_ifsc') or '').strip().upper()
        if v and not re.fullmatch(r'[A-Z]{4}0[A-Z0-9]{6}', v):
            raise forms.ValidationError('Enter a valid 11-character IFSC, for example SBIN0001234.')
        return v

    def clean(self):
        data = super().clean()
        gstin, pan = data.get('gstin'), data.get('pan')
        if gstin and pan and gstin[2:12] != pan:
            self.add_error('pan', 'The PAN must match characters 3 to 12 of the GSTIN.')
        return data


POLICY_FIELDS = ['first_response_minutes', 'unassigned_manager_alert_minutes', 'unassigned_owner_alert_minutes', 'overdue_manager_hours',
                 'overdue_owner_hours', 'stale_days', 'stale_escalate_days', 'aging_qualified_days', 'aging_quotation_sent_days',
                 'aging_negotiation_days', 'postpone_review_count', 'work_start_hour', 'work_end_hour', 'default_open_lead_cap',
                 'lost_approval_threshold', 'win_back_days', 'exec_discount_limit', 'manager_discount_limit', 'special_terms_days',
                 'exec_payment_link_limit', 'payment_link_default_expiry_days', 'payment_link_max_expiry_days', 'min_partial_payment',
                 'lead_export_approval_rows', 'manual_payment_proof_above', 'expense_proof_above', 'expense_manager_limit',
                 'credit_note_approval_above', 'journal_approval_above', 'margin_floor_percent', 'idle_timeout_minutes']


class PolicyForm(VersionedModelFormMixin, CrmModelForm):
    reason = forms.CharField(max_length=300, min_length=10, label='Reason for the change')

    class Meta:
        model = CompanySettings
        fields = POLICY_FIELDS

    def clean(self):
        d = super().clean()
        if d.get('work_start_hour') is not None and d.get('work_end_hour') is not None and d['work_start_hour'] >= d['work_end_hour']:
            self.add_error('work_end_hour', 'Working hours must end after they start.')
        for f in ('work_start_hour', 'work_end_hour'):
            if d.get(f) is not None and d[f] > 23:
                self.add_error(f, 'Use an hour from 0 to 23.')
        if d.get('exec_discount_limit') is not None and d.get('manager_discount_limit') is not None \
                and d['exec_discount_limit'] > d['manager_discount_limit']:
            self.add_error('exec_discount_limit', 'The executive limit cannot be above the manager limit.')
        if d.get('payment_link_default_expiry_days') and d.get('payment_link_max_expiry_days') \
                and d['payment_link_default_expiry_days'] > d['payment_link_max_expiry_days']:
            self.add_error('payment_link_default_expiry_days', 'The default expiry cannot exceed the maximum.')
        if d.get('idle_timeout_minutes') is not None and not 5 <= d['idle_timeout_minutes'] <= 480:
            self.add_error('idle_timeout_minutes', 'Use 5 to 480 minutes.')
        for f in ('exec_discount_limit', 'manager_discount_limit', 'margin_floor_percent'):
            if d.get(f) is not None and not 0 <= d[f] <= 100:
                self.add_error(f, 'Use a percentage from 0 to 100.')
        if d.get('stale_days') and d.get('stale_escalate_days') and d['stale_escalate_days'] < d['stale_days']:
            self.add_error('stale_escalate_days', 'Escalation must come after the stale warning.')
        return d


TAX_FIELDS = ['gst_registered', 'lut_enabled', 'tax_on_advance', 'advance_allocation_rule', 'round_off_quotation', 'round_off_invoice']


class TaxSettingsForm(VersionedModelFormMixin, CrmModelForm):
    reason = forms.CharField(max_length=300, min_length=10, label='Reason for the change')

    class Meta:
        model = CompanySettings
        fields = TAX_FIELDS


class SeriesForm(CrmModelForm):
    class Meta:
        model = SequenceSeries
        fields = ['prefix', 'padding']

    def clean_prefix(self):
        v = self.cleaned_data['prefix'].strip().upper()
        if not re.fullmatch(r'[A-Z0-9-]{1,10}', v):
            raise forms.ValidationError('Use 1 to 10 capital letters, digits or hyphens.')
        return v

    def clean_padding(self):
        v = self.cleaned_data['padding']
        if not 3 <= v <= 8:
            raise forms.ValidationError('Use 3 to 8 digits.')
        return v


class CategoryTdsForm(CrmModelForm):
    reason = forms.CharField(max_length=300, min_length=10, label='Reason for the change')

    class Meta:
        model = ExpenseCategory
        fields = ['tds_section', 'tds_rate', 'tds_threshold', 'itc_default']

    def clean(self):
        d = super().clean()
        if d.get('tds_section') and not d.get('tds_rate'):
            self.add_error('tds_rate', 'Enter the TDS rate for this section.')
        if d.get('tds_rate') and not d.get('tds_section'):
            self.add_error('tds_section', 'Choose the TDS section.')
        return d


class GatewayForm(CrmForm):
    provider = forms.ChoiceField(choices=CompanySettings._meta.get_field('gateway_provider').choices)
    app_id = forms.CharField(required=False, max_length=120, label='App ID / key ID')
    secret = forms.CharField(required=False, max_length=300, widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'new-password'}),
                             label='Secret key', help_text='Write-only. Leave blank to keep the current secret.')
    webhook_secret = forms.CharField(required=False, max_length=300, widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'new-password'}),
                                     label='Webhook secret', help_text='Write-only. Leave blank to keep the current one.')
    methods = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple,
                                        choices=[('upi', 'UPI'), ('card', 'Card'), ('netbanking', 'Netbanking'), ('wallet', 'Wallet')])
    reason = forms.CharField(required=False, max_length=300, label='Reason (needed to switch mode)')


class UserCreateForm(CrmForm):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    username = forms.CharField(max_length=150, help_text='Letters, digits and . _ - only.')
    email = forms.EmailField()
    roles = forms.ModelMultipleChoiceField(queryset=Role.objects.all(), widget=forms.CheckboxSelectMultiple)
    team = forms.ModelChoiceField(queryset=Team.objects.filter(is_active=True), required=False)
    department = forms.ModelChoiceField(queryset=Department.objects.filter(is_active=True), required=False)
    manager = UserChoice(queryset=_users(), required=False)
    designation = forms.CharField(max_length=120, required=False)
    receives_leads = forms.BooleanField(required=False, label='Receives auto-assigned leads')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['manager'].queryset = _users()

    def clean_username(self):
        v = self.cleaned_data['username'].strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]{3,150}', v):
            raise forms.ValidationError('Use 3 or more letters, digits, dots, hyphens or underscores.')
        return v


class UserAccessForm(CrmForm):
    roles = forms.ModelMultipleChoiceField(queryset=Role.objects.all(), widget=forms.CheckboxSelectMultiple, required=False)
    team = forms.ModelChoiceField(queryset=Team.objects.all(), required=False)
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)
    manager = UserChoice(queryset=_users(), required=False)
    designation = forms.CharField(max_length=120, required=False)
    receives_leads = forms.BooleanField(required=False, label='Receives auto-assigned leads')
    open_lead_cap = forms.IntegerField(required=False, min_value=1, max_value=5000, label='Open-lead cap (blank = company default)')
    reason = forms.CharField(required=False, max_length=300, label='Reason (needed for a role change)')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['manager'].queryset = _users()


class ReasonForm(CrmForm):
    reason = forms.CharField(min_length=10, max_length=300)


class ResetAccessForm(CrmForm):
    reset_two_factor = forms.BooleanField(required=False, label='Also turn off two-factor sign-in (lost phone)')
    reason = forms.CharField(min_length=10, max_length=300)


class RoleCreateForm(CrmForm):
    name = forms.CharField(max_length=80)
    code = forms.SlugField(max_length=40, help_text='Short id, e.g. support_lead.')
    description = forms.CharField(max_length=300, required=False)
    copy_from = forms.ModelChoiceField(queryset=Role.objects.all(), required=False, label='Start from')


class TeamForm(CrmModelForm):
    manager = UserChoice(queryset=_users(), required=False)

    class Meta:
        model = Team
        fields = ['name', 'department', 'manager', 'is_sales', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['manager'].queryset = _users()


class DepartmentForm(CrmModelForm):
    head = UserChoice(queryset=_users(), required=False)

    class Meta:
        model = Department
        fields = ['name', 'parent', 'head', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['head'].queryset = _users()
        if self.instance.pk:
            # Never let a department sit under itself or its own children.
            blocked = {self.instance.pk}
            frontier = [self.instance.pk]
            while frontier:
                kids = list(Department.objects.filter(parent_id__in=frontier).values_list('pk', flat=True))
                frontier = [k for k in kids if k not in blocked]
                blocked.update(frontier)
            self.fields['parent'].queryset = Department.objects.exclude(pk__in=blocked)


class TemplateForm(CrmModelForm):
    reason = forms.CharField(max_length=300, min_length=10, label='What changed and why')

    class Meta:
        model = MessageTemplate
        fields = ['name', 'subject', 'body']
        widgets = {'body': forms.Textarea(attrs={'rows': 12})}

    def clean_body(self):
        from django.template import Template, TemplateSyntaxError
        body = self.cleaned_data['body']
        try:
            Template(body)
        except TemplateSyntaxError as exc:
            raise forms.ValidationError(f'The template has a syntax error: {exc}')
        if '{%' in body and any(t in body for t in ('load ', 'include ', 'extends ', 'debug', 'ssi')):
            raise forms.ValidationError('Templates may use variables and simple if/for blocks only.')
        return body
