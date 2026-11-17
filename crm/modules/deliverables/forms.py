"""Forms for plans, items, templates and the small actions on a deliverable."""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model

from crm.core import india
from crm.core.forms import CrmForm, CrmModelForm, DateInput
from crm.models import PlanItem, ServiceDeliverable
from crm.models.deliverables import FREQUENCIES, WEEKDAYS


def staff_users():
    return get_user_model().objects.filter(is_active=True, image_access__isnull=True).exclude(username__startswith='business_manager').order_by('first_name', 'username')


class UserChoice(forms.ModelChoiceField):
    def label_from_instance(self, u):
        return u.get_full_name() or u.get_username()


class TimeInput(forms.TimeInput):
    input_type = 'time'

    def __init__(self, attrs=None):
        super().__init__(attrs, format='%H:%M')


class ScheduleMixin(forms.Form):
    """The shared "when does this fall due" fields, used for both a client's plan item and a service template."""
    frequency = forms.ChoiceField(choices=FREQUENCIES, label='How often', widget=forms.Select(attrs={'x-model': 'f'}))
    weekdays = forms.MultipleChoiceField(choices=WEEKDAYS, required=False, label='On which days',
                                         widget=forms.CheckboxSelectMultiple)
    month_days = forms.CharField(required=False, label='Day of the month', max_length=40,
                                 help_text='One or more days, e.g. 5 or 5, 20. A 31 means the last day of shorter months.')
    category = forms.CharField(required=False, max_length=60, widget=forms.TextInput(attrs={'list': 'dl-categories'}))

    def clean_month_days(self):
        raw = (self.cleaned_data.get('month_days') or '').replace(';', ',').replace(' ', ',')
        days = []
        for part in [p for p in raw.split(',') if p]:
            if not part.isdigit() or not 1 <= int(part) <= 31:
                raise forms.ValidationError('Use whole days from 1 to 31, separated by commas.')
            if int(part) not in days:
                days.append(int(part))
        return ','.join(str(d) for d in sorted(days))

    def clean(self):
        cleaned = super().clean()
        freq = cleaned.get('frequency')
        if freq == 'weekly' and not cleaned.get('weekdays'):
            self.add_error('weekdays', 'Choose at least one day.')
        if freq in ('monthly', 'quarterly') and not cleaned.get('month_days') and 'month_days' not in self.errors:
            self.add_error('month_days', 'Enter the day of the month.')
        if freq != 'weekly':
            cleaned['weekdays'] = []
        if freq not in ('monthly', 'quarterly'):
            cleaned['month_days'] = ''
        return cleaned

    def _csv_weekdays(self):
        return ','.join(str(d) for d in sorted(int(x) for x in self.cleaned_data.get('weekdays', [])))


class PlanItemForm(ScheduleMixin, CrmModelForm):
    assignee = UserChoice(queryset=staff_users(), required=False, empty_label='Plan default')
    reviewer = UserChoice(queryset=staff_users(), required=False, empty_label="Assignee's manager")
    field_order = ['title', 'category', 'frequency', 'weekdays', 'month_days', 'due_time', 'quantity', 'handled_by',
                   'start_date', 'end_date', 'assignee', 'reviewer', 'requires_review', 'needs_client_approval', 'preferences']

    class Meta:
        model = PlanItem
        fields = ['title', 'category', 'frequency', 'due_time', 'quantity', 'handled_by', 'start_date',
                  'end_date', 'assignee', 'reviewer', 'requires_review', 'needs_client_approval', 'preferences']
        widgets = {'due_time': TimeInput(), 'start_date': DateInput(), 'end_date': DateInput(),
                   'preferences': forms.Textarea(attrs={'rows': 3})}
        labels = {'title': 'What is promised', 'due_time': 'Due by', 'quantity': 'How many each time',
                  'handled_by': 'Handled by (team)', 'start_date': 'Starts', 'end_date': 'Ends (optional)',
                  'requires_review': 'Needs an internal review before it goes out',
                  'needs_client_approval': 'Needs the client to approve it',
                  'preferences': 'What the client prefers for this item'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].required = False
        self.fields['handled_by'].required = False
        self.fields['handled_by'].widget.attrs['list'] = 'dl-teams'
        if self.instance.pk and not self.is_bound:
            self.initial['weekdays'] = self.instance.weekday_list
        elif not self.instance.pk:
            self.initial.setdefault('start_date', india.today())
            self.initial.setdefault('due_time', '18:00')
            self.initial.setdefault('quantity', 1)

    def clean(self):
        cleaned = super().clean()
        s, e = cleaned.get('start_date'), cleaned.get('end_date')
        if s and e and e < s:
            self.add_error('end_date', 'The end date is before the start date.')
        return cleaned

    def save(self, commit=True):
        inst = super().save(commit=False)
        inst.weekdays = self._csv_weekdays()
        inst.month_days = self.cleaned_data.get('month_days', '')
        if commit:
            inst.save()
        return inst


class TemplateItemForm(ScheduleMixin, CrmModelForm):
    field_order = ['title', 'category', 'frequency', 'weekdays', 'month_days', 'due_time', 'quantity', 'handled_by',
                   'requires_review', 'needs_client_approval', 'preferences']

    class Meta:
        model = ServiceDeliverable
        fields = ['title', 'category', 'frequency', 'due_time', 'quantity', 'handled_by',
                  'requires_review', 'needs_client_approval', 'preferences']
        widgets = {'due_time': TimeInput(), 'preferences': forms.Textarea(attrs={'rows': 2})}
        labels = {'title': 'What is promised', 'due_time': 'Due by', 'quantity': 'How many each time',
                  'handled_by': 'Handled by (team)', 'requires_review': 'Needs an internal review',
                  'needs_client_approval': 'Needs the client to approve it', 'preferences': 'Default preference note'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].required = False
        self.fields['handled_by'].required = False
        self.fields['handled_by'].widget.attrs['list'] = 'dl-teams'
        if self.instance.pk and not self.is_bound:
            self.initial['weekdays'] = [int(x) for x in self.instance.weekdays.split(',') if x.isdigit()]
        elif not self.instance.pk:
            self.initial.setdefault('due_time', '18:00')
            self.initial.setdefault('quantity', 1)

    def save(self, commit=True):
        inst = super().save(commit=False)
        inst.weekdays = self._csv_weekdays()
        inst.month_days = self.cleaned_data.get('month_days', '')
        if commit:
            inst.save()
        return inst


class AdhocForm(CrmForm):
    title = forms.CharField(max_length=200, label='What needs to be delivered')
    handled_by = forms.CharField(required=False, max_length=60, label='Handled by (team)', widget=forms.TextInput(attrs={'list': 'dl-teams'}))
    due = forms.DateField(widget=DateInput(), label='Due on')
    due_time = forms.TimeField(widget=TimeInput(), required=False, label='Due by')
    quantity = forms.IntegerField(min_value=1, max_value=200, initial=1, label='How many')
    category = forms.CharField(required=False, max_length=60, widget=forms.TextInput(attrs={'list': 'dl-categories'}))
    assignee = UserChoice(queryset=staff_users(), required=False, empty_label='Plan default')
    instructions = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}), label='What the client asked for')


class PreferencesForm(CrmForm):
    client_preferences = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={'rows': 5}),
                                         label='Client preferences',
                                         help_text='Tone, brand colours, fonts, things to avoid, festival days to skip. Shown on every deliverable.')


class ReassignForm(CrmForm):
    to = UserChoice(queryset=staff_users(), label='Give to')
    account_owner = forms.BooleanField(required=False, label='Make them the account owner too')
    note = forms.CharField(required=False, max_length=300, label='Note (optional)')
