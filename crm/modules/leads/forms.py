"""Lead forms (Part 4.5 and the popups in 4.7)."""
from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from crm.core import india, permissions
from crm.core.forms import (CrmForm, CrmModelForm, DateInput, DateTimeInput, MoneyFormField, PhoneField,
                            SafeFileField, VersionedModelFormMixin, future_date, future_datetime)
from crm.models import Campaign, Lead, LeadActivity, LeadSource, LeadTag, Service
from crm.models.leads import LOST_REASONS, OPEN_STAGES, PRIORITIES, STAGES, STAGE_WON, STAGE_LOST

FOLLOW_UP_KINDS = [('call', 'Call'), ('meeting', 'Meeting'), ('email', 'Email'), ('whatsapp', 'WhatsApp')]
POSTPONE_REASONS = [('customer_request', 'Customer asked to call later'), ('no_answer', 'Could not reach'),
                    ('busy', 'I was busy with another client'), ('waiting_info', 'Waiting for information'),
                    ('holiday', 'Holiday / leave'), ('other', 'Other')]


def user_label(u):
    return u.get_full_name() or u.get_username()


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return user_label(obj)


def assignable_users(user):
    """Users this person may assign leads to, limited by their assign scope."""
    User = get_user_model()
    qs = User.objects.filter(is_active=True).select_related('crm_profile').order_by('first_name', 'username')
    scope = permissions.get_scope(user, 'leads', 'assign')
    if scope is None:
        return qs.filter(pk=user.pk)
    if scope == 'company':
        return qs
    profile = getattr(user, 'crm_profile', None)
    q = Q(pk=user.pk)
    if profile and profile.team_id:
        q |= Q(crm_profile__team_id=profile.team_id)
    if scope == 'department' and profile and profile.department_id:
        q |= Q(crm_profile__department_id=profile.department_id)
    return qs.filter(q)


def default_follow_up():
    return india.next_working_day_at(10, 30)


class LeadForm(VersionedModelFormMixin, CrmModelForm):
    phone = PhoneField(required=False, label='Phone')
    budget_min = MoneyFormField(required=False, label='Budget from')
    budget_max = MoneyFormField(required=False, label='Budget to')
    value = MoneyFormField(required=False, label='Expected deal value', help_text='Excluding tax.')
    next_follow_up_at = forms.DateTimeField(label='Next follow-up', widget=DateTimeInput, required=False,
                                            validators=[future_datetime])
    services = forms.ModelMultipleChoiceField(queryset=Service.objects.filter(is_active=True), label='Service interested',
                                              widget=forms.CheckboxSelectMultiple)
    tags = forms.ModelMultipleChoiceField(queryset=LeadTag.objects.all(), required=False,
                                          widget=forms.CheckboxSelectMultiple)
    owner = UserChoiceField(queryset=None, required=False, label='Owner')

    class Meta:
        model = Lead
        fields = ['name', 'company', 'phone', 'email', 'website', 'city', 'state', 'services', 'budget_min',
                  'budget_max', 'value', 'expected_close_date', 'decision_maker', 'source', 'campaign', 'referrer',
                  'owner', 'priority', 'next_follow_up_at', 'notes', 'tags']
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3, 'maxlength': 2000}),
            'expected_close_date': DateInput,
            'name': forms.TextInput(attrs={'placeholder': 'Anita K.', 'autocomplete': 'name'}),
            'email': forms.EmailInput(attrs={'placeholder': 'anita@example.com', 'autocomplete': 'email'}),
            'company': forms.TextInput(attrs={'placeholder': 'Anita Textiles'}),
            'website': forms.URLInput(attrs={'placeholder': 'https://example.com'}),
            'city': forms.TextInput(attrs={'placeholder': 'Trivandrum'}),
        }
        labels = {'expected_close_date': 'Expected closing date', 'decision_maker': 'Decision-maker'}

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields['source'].queryset = LeadSource.objects.filter(is_active=True)
        self.fields['campaign'].queryset = Campaign.objects.filter(is_active=True).select_related('source')
        self.fields['campaign'].required = False
        can_assign = permissions.has_perm(user, 'leads', 'assign')
        self.fields['owner'].queryset = assignable_users(user)
        if can_assign:
            self.fields['owner'].empty_label = 'Assign automatically (round-robin)'
        else:
            self.fields['owner'].empty_label = None
            self.fields['owner'].required = True
            self.fields['owner'].initial = user.pk
            self.fields['owner'].disabled = True
            self.fields['owner'].help_text = 'Leads you create are owned by you.'
        if not self.instance.pk:
            self.fields['next_follow_up_at'].required = True
            self.initial.setdefault('next_follow_up_at', timezone.localtime(default_follow_up()).strftime('%Y-%m-%dT%H:%M'))
            self.initial.setdefault('priority', 'warm')
            manual = LeadSource.objects.filter(channel='manual').first()
            if manual:
                self.initial.setdefault('source', manual.pk)
        else:
            self.fields['next_follow_up_at'].required = self.instance.is_open
            self.fields['next_follow_up_at'].help_text = 'Changing it reschedules the earliest pending follow-up.'
            if self.instance.next_follow_up_at:
                self.initial['next_follow_up_at'] = timezone.localtime(self.instance.next_follow_up_at).strftime('%Y-%m-%dT%H:%M')
            if not can_assign:
                self.fields['owner'].initial = self.instance.owner_id
            # Owner changes go through Assign, which records history and notifies.
            del self.fields['owner']

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if len(name) < 2:
            raise forms.ValidationError('Enter a name of at least 2 characters.')
        return name

    def clean_email(self):
        return (self.cleaned_data.get('email') or '').strip().lower()

    def clean_expected_close_date(self):
        d = self.cleaned_data.get('expected_close_date')
        if d and d < india.today() and (not self.instance.pk or d != self.instance.expected_close_date):
            raise forms.ValidationError('Choose a date that is not in the past.')
        return d

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('phone') and not cleaned.get('email'):
            self.add_error('phone', 'Enter a phone number or an email address.')
        bmin, bmax = cleaned.get('budget_min'), cleaned.get('budget_max')
        if bmin is not None and bmax is not None and bmin > bmax:
            self.add_error('budget_min', 'The lower budget cannot be above the upper budget.')
        campaign, source = cleaned.get('campaign'), cleaned.get('source')
        if campaign and source and campaign.source_id != source.pk:
            self.add_error('campaign', 'This campaign belongs to a different source.')
        inst = self.instance
        if inst.pk and inst.stage not in ('new', 'contacted', 'nurturing', 'won', 'lost'):
            # Qualified and beyond must keep the qualification facts.
            if not cleaned.get('expected_close_date'):
                self.add_error('expected_close_date', 'Required from Qualified onward.')
            if not cleaned.get('decision_maker'):
                self.add_error('decision_maker', 'Required from Qualified onward.')
        return cleaned


class DuplicateDecisionForm(CrmForm):
    duplicate_action = forms.ChoiceField(choices=[('attach', 'Add as interaction to existing lead'),
                                                  ('create', 'Create anyway')], widget=forms.RadioSelect)
    target = forms.IntegerField(required=False, widget=forms.HiddenInput)
    duplicate_reason = forms.CharField(required=False, max_length=300, label='Reason for creating a separate lead')

    def clean(self):
        c = super().clean()
        if c.get('duplicate_action') == 'create' and len((c.get('duplicate_reason') or '').strip()) < 5:
            self.add_error('duplicate_reason', 'Give a reason. The pair is flagged for merge review.')
        return c


class NextFollowUpMixin(forms.Form):
    next_due = forms.DateTimeField(label='Next follow-up', widget=DateTimeInput, required=False,
                                   validators=[future_datetime])
    next_kind = forms.ChoiceField(choices=FOLLOW_UP_KINDS, initial='call', label='Next action type', required=False)
    next_purpose = forms.CharField(max_length=200, required=False, label='Purpose',
                                   widget=forms.TextInput(attrs={'placeholder': 'Share revised quote'}))


class LogCallForm(NextFollowUpMixin, CrmForm):
    direction = forms.ChoiceField(choices=[('out', 'Outgoing'), ('in', 'Incoming')], initial='out')
    outcome = forms.ChoiceField(choices=LeadActivity.CALL_OUTCOMES)
    duration_minutes = forms.IntegerField(min_value=0, max_value=600, required=False, label='Duration (minutes)')
    summary = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False, max_length=5000,
                              help_text='Required when connected.')

    def clean(self):
        c = super().clean()
        if c.get('outcome') == 'connected' and not (c.get('summary') or '').strip():
            self.add_error('summary', 'Add a short summary of the call.')
        return c


class LogMeetingForm(NextFollowUpMixin, CrmForm):
    meeting_type = forms.ChoiceField(choices=[('in_person', 'In person'), ('video', 'Video call'), ('site_visit', 'Site visit')])
    occurred_at = forms.DateTimeField(label='Date and time', widget=DateTimeInput)
    attendees = forms.CharField(max_length=300, required=False)
    summary = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), label='Notes', max_length=5000)
    next_step = forms.CharField(max_length=200, required=False, label='Agreed next step')

    def clean_occurred_at(self):
        v = self.cleaned_data['occurred_at']
        if v > timezone.now() + timedelta(minutes=5):
            raise forms.ValidationError('A completed meeting cannot be in the future. Schedule a follow-up instead.')
        return v


class NoteForm(CrmForm):
    summary = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}), label='Note', max_length=5000)
    visibility = forms.ChoiceField(choices=[('team', 'Team'), ('private', 'Private to manager')], initial='team')


class MessageForm(NextFollowUpMixin, CrmForm):
    channel = forms.ChoiceField(choices=[('email', 'Email'), ('whatsapp', 'WhatsApp')])
    direction = forms.ChoiceField(choices=[('out', 'Sent'), ('in', 'Received')], initial='out')
    summary = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), label='Message summary', max_length=5000)


class ScheduleFollowUpForm(CrmForm):
    kind = forms.ChoiceField(choices=FOLLOW_UP_KINDS, label='Type')
    due_at = forms.DateTimeField(label='Date and time', widget=DateTimeInput, validators=[future_datetime])
    assignee = UserChoiceField(queryset=None)
    reminder_minutes = forms.ChoiceField(choices=[(0, 'At the time'), (15, '15 minutes before'), (60, '1 hour before'),
                                                  (1440, '1 day before')], initial=15, label='Reminder')
    purpose = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, user=None, lead=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assignee'].queryset = assignable_users(user)
        if lead is not None and lead.owner_id and not self.is_bound:
            self.initial['assignee'] = lead.owner_id
        self.initial.setdefault('due_at', timezone.localtime(default_follow_up()).strftime('%Y-%m-%dT%H:%M'))


class CompleteFollowUpForm(NextFollowUpMixin, CrmForm):
    follow_up = forms.IntegerField(widget=forms.HiddenInput)
    outcome = forms.CharField(max_length=200, widget=forms.TextInput(attrs={'placeholder': 'Discussed pricing, will decide Friday'}))
    notes = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), required=False)


class PostponeForm(CrmForm):
    follow_up = forms.IntegerField(widget=forms.HiddenInput)
    new_due = forms.DateTimeField(label='New date and time', widget=DateTimeInput, validators=[future_datetime])
    reason = forms.ChoiceField(choices=POSTPONE_REASONS)


class CancelFollowUpForm(CrmForm):
    follow_up = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(max_length=300)


class StageForm(NextFollowUpMixin, CrmForm):
    to = forms.ChoiceField(choices=[], label='New stage')
    reason = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={'rows': 2}),
                             help_text='Required for backward moves, skips and nurturing.')

    def __init__(self, *args, lead=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(s, l) for s, l in STAGES if s not in ('new', 'quotation_sent', STAGE_WON, STAGE_LOST)
                   and (lead is None or s != lead.stage)]
        self.fields['to'].choices = choices


class WonForm(CrmForm):
    won_value = MoneyFormField(label='Won value (excluding tax)')
    quotation = forms.ModelChoiceField(queryset=None, required=False, label='Accepted quotation')
    expected_start = forms.DateField(widget=DateInput, required=False, label='Expected start')
    note = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={'rows': 2}),
                           label='Confirmed order note', help_text='Required when there is no accepted quotation.')
    client_choice = forms.ChoiceField(required=False, widget=forms.RadioSelect, label='Client record')

    def __init__(self, *args, lead=None, matches=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['quotation'].queryset = lead.quotations.exclude(status__in=['void', 'superseded']) if lead else None
        choices = [(str(c.pk), f'Link to existing client: {c}') for c in matches]
        choices.append(('new', 'Create a new client from this lead'))
        self.fields['client_choice'].choices = choices
        self.fields['client_choice'].initial = choices[0][0]
        if lead is not None and not self.is_bound:
            q = lead.quotations.filter(status__in=['accepted', 'converted']).order_by('-id').first()
            self.initial['won_value'] = (q.taxable_total if q else lead.value) or None
            if q:
                self.initial['quotation'] = q.pk
        if lead is not None and lead.client_id:
            del self.fields['client_choice']

    def clean(self):
        c = super().clean()
        if c.get('won_value') is not None and c['won_value'] <= 0:
            self.add_error('won_value', 'Enter the won value (more than ₹0).')
        return c


class LostForm(CrmForm):
    reason = forms.ChoiceField(choices=[('', 'Choose a reason')] + LOST_REASONS)
    competitor = forms.CharField(max_length=120, required=False)
    price = MoneyFormField(required=False, label='Price they chose')
    notes = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), required=False, help_text='Required if the reason is Other.')
    win_back_date = forms.DateField(widget=DateInput, required=False, validators=[future_date],
                                    help_text='Defaults to 90 days from today.')

    def clean(self):
        c = super().clean()
        if c.get('reason') == 'other' and len((c.get('notes') or '').strip()) < 5:
            self.add_error('notes', 'Add notes explaining the reason when you choose Other.')
        return c


class AssignForm(CrmForm):
    owner = UserChoiceField(queryset=None, label='New owner')
    reason = forms.CharField(max_length=300, required=False)
    transfer_follow_ups = forms.BooleanField(required=False, initial=True, label='Transfer open follow-ups to the new owner')
    override = forms.BooleanField(required=False, label='Assign even if the person is on leave or unavailable')

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['owner'].queryset = assignable_users(user)


class ReopenForm(CrmForm):
    reason = forms.CharField(max_length=300)
    next_due = forms.DateTimeField(label='Next follow-up', widget=DateTimeInput, validators=[future_datetime])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault('next_due', timezone.localtime(default_follow_up()).strftime('%Y-%m-%dT%H:%M'))


class BulkForm(CrmForm):
    action = forms.ChoiceField(choices=[('assign', 'Assign selected'), ('priority', 'Change priority'), ('tag', 'Add tag')])
    owner = UserChoiceField(queryset=None, required=False)
    priority = forms.ChoiceField(choices=[('', '—')] + PRIORITIES, required=False)
    tag = forms.ModelChoiceField(queryset=LeadTag.objects.all(), required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['owner'].queryset = assignable_users(user)


class SourceForm(CrmModelForm):
    class Meta:
        model = LeadSource
        fields = ['name', 'channel', 'is_active', 'expected_min_per_day', 'silent_window_hours', 'default_owner',
                  'monthly_cost', 'consent_basis', 'field_mapping']
        widgets = {'field_mapping': forms.Textarea(attrs={'rows': 3, 'placeholder': '{"full_name": "name", "mobile": "phone"}'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        User = get_user_model()
        self.fields['default_owner'].queryset = User.objects.filter(is_active=True)
        self.fields['default_owner'].label_from_instance = user_label
        self.fields['default_owner'].help_text = 'Leave empty to use round-robin assignment.'


class CampaignForm(CrmModelForm):
    class Meta:
        model = Campaign
        fields = ['name', 'source', 'start_date', 'end_date', 'budget', 'is_active']
        widgets = {'start_date': DateInput, 'end_date': DateInput}


class ImportUploadForm(CrmForm):
    file = SafeFileField(allowed=('.csv', '.xlsx'), label='CSV or Excel file',
                         help_text='Up to 10 MB or 10,000 rows. First row must be the column headings.')


class ImportOptionsForm(CrmForm):
    duplicates = forms.ChoiceField(choices=[('skip', 'Skip duplicates'), ('interaction', 'Add as interaction to the existing lead'),
                                            ('create', 'Create anyway (flagged for merge review)')], initial='skip',
                                   widget=forms.RadioSelect, label='When a row matches an existing lead')
    owner = UserChoiceField(queryset=None, required=False, label='Assign to',
                            empty_label='Assign by rule (round-robin)')
    source = forms.ModelChoiceField(queryset=LeadSource.objects.filter(is_active=True))
    campaign = forms.ModelChoiceField(queryset=Campaign.objects.filter(is_active=True), required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['owner'].queryset = assignable_users(user)
        csv_src = LeadSource.objects.filter(channel='csv').first()
        if csv_src:
            self.initial.setdefault('source', csv_src.pk)
