"""Project, task, timesheet, change request and ticket forms (Part 10.3, 10.5)."""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model

from crm.core import india
from crm.core.forms import CrmForm, CrmModelForm, DateInput, MoneyFormField, VersionedModelFormMixin
from crm.models import ChangeRequest, Client, Milestone, Project, Service, Task, Ticket
from crm.models.delivery import TASK_PRIORITIES

User = get_user_model()


def staff_users():
    return User.objects.filter(is_active=True, crm_profile__isnull=False).order_by('first_name', 'username')


def _label(u):
    return u.get_full_name() or u.username


class ProjectForm(VersionedModelFormMixin, CrmModelForm):
    class Meta:
        model = Project
        fields = ['name', 'client', 'service', 'manager', 'start_date', 'due_date', 'budget_hours', 'budget_amount', 'scope',
                  'acceptance_criteria']
        widgets = {'start_date': DateInput, 'due_date': DateInput, 'scope': forms.Textarea(attrs={'rows': 4}),
                   'acceptance_criteria': forms.Textarea(attrs={'rows': 3})}
        labels = {'budget_amount': 'Budget (₹)'}

    def __init__(self, *args, clients=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['client'].queryset = clients if clients is not None else Client.objects.filter(archived_at__isnull=True)
        self.fields['service'].queryset = Service.objects.filter(is_active=True)
        self.fields['service'].required = False
        self.fields['manager'].queryset = staff_users()
        self.fields['manager'].label_from_instance = _label
        if self.instance.pk:
            self.fields['client'].disabled = True

    def clean(self):
        c = super().clean()
        if c.get('start_date') and c.get('due_date') and c['due_date'] < c['start_date']:
            self.add_error('due_date', 'The due date must be after the start date.')
        return c


class TaskForm(VersionedModelFormMixin, CrmModelForm):
    depends_on = forms.ModelMultipleChoiceField(queryset=Task.objects.none(), required=False, label='Depends on')

    class Meta:
        model = Task
        fields = ['title', 'description', 'assignee', 'reviewer', 'due_date', 'priority', 'estimate_hours', 'milestone']
        widgets = {'due_date': DateInput, 'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        project = project or self.instance.project
        members = User.objects.filter(pk__in=list(project.members.values_list('user_id', flat=True)) + [project.manager_id])
        for f in ('assignee', 'reviewer'):
            self.fields[f].queryset = members
            self.fields[f].label_from_instance = _label
            self.fields[f].required = False
        self.fields['milestone'].queryset = project.milestones.all()
        self.fields['milestone'].required = False
        self.fields['depends_on'].queryset = project.tasks.exclude(pk=self.instance.pk or 0)
        if self.instance.pk and not self.is_bound:
            self.initial['depends_on'] = list(self.instance.depends_on.values_list('pk', flat=True))


class MilestoneForm(CrmModelForm):
    class Meta:
        model = Milestone
        fields = ['name', 'due_date', 'amount']
        widgets = {'due_date': DateInput}
        labels = {'amount': 'Billable amount on acceptance (ex. tax)'}


class MemberForm(CrmForm):
    user = forms.ModelChoiceField(queryset=User.objects.none(), label='Member')
    role = forms.CharField(max_length=60, required=False)
    allocation_percent = forms.IntegerField(min_value=0, max_value=100, initial=50, label='Allocation %')
    start_date = forms.DateField(widget=DateInput, required=False)
    end_date = forms.DateField(widget=DateInput, required=False)
    cost_rate = MoneyFormField(required=False, label='Cost per hour (₹)', help_text='Visible only with the cost permission.')

    def __init__(self, *args, show_cost=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['user'].queryset = staff_users()
        self.fields['user'].label_from_instance = _label
        if not show_cost:
            del self.fields['cost_rate']


class DeadlineForm(CrmForm):
    new_date = forms.DateField(widget=DateInput, label='New due date')
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), min_length=10)


class AcceptMilestoneForm(CrmForm):
    evidence = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}), min_length=5, label='Acceptance evidence',
                               help_text='Who accepted and how (email, call, sign-off).')
    invoice_action = forms.ChoiceField(choices=[('none', 'No invoice now'), ('draft_invoice', 'Create a draft invoice for the milestone amount')],
                                       initial='draft_invoice', label='Billing')


class TimeForm(CrmForm):
    project = forms.ModelChoiceField(queryset=Project.objects.none())
    task = forms.ModelChoiceField(queryset=Task.objects.none(), required=False)
    entry_date = forms.DateField(widget=DateInput, label='Date')
    hours = forms.DecimalField(min_value=Decimal('0.25'), max_value=24, max_digits=4, decimal_places=2, widget=forms.NumberInput(attrs={'step': '0.25'}))
    billable = forms.BooleanField(required=False, initial=True)
    note = forms.CharField(required=False, max_length=300)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        projects = Project.objects.filter(status__in=['planned', 'active', 'on_hold']).filter(
            models_q(user)).distinct().order_by('code')
        self.fields['project'].queryset = projects
        self.fields['task'].queryset = Task.objects.filter(project__in=projects).exclude(status__in=['done', 'cancelled'])
        self.fields['task'].label_from_instance = lambda t: f'{t.project.code} · {t.title}'
        self.initial.setdefault('entry_date', india.today())

    def clean(self):
        c = super().clean()
        if c.get('task') and c.get('project') and c['task'].project_id != c['project'].pk:
            self.add_error('task', 'This task belongs to another project.')
        return c


def models_q(user):
    from django.db.models import Q
    return Q(members__user=user) | Q(manager=user)


class ChangeRequestForm(CrmModelForm):
    tax_rate = forms.DecimalField(max_digits=5, decimal_places=2, initial=Decimal('18'))

    class Meta:
        model = ChangeRequest
        fields = ['title', 'description', 'effort_hours', 'price', 'tax_rate', 'client_evidence']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}
        labels = {'price': 'Price (ex. tax)', 'client_evidence': 'Client approval evidence'}


class TicketForm(CrmModelForm):
    class Meta:
        model = Ticket
        fields = ['client', 'project', 'subject', 'description', 'priority', 'assignee']
        widgets = {'description': forms.Textarea(attrs={'rows': 4})}

    def __init__(self, *args, clients=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['client'].queryset = clients if clients is not None else Client.objects.filter(archived_at__isnull=True)
        self.fields['project'].queryset = Project.objects.exclude(status__in=['closed', 'cancelled'])
        self.fields['project'].required = False
        self.fields['assignee'].queryset = staff_users()
        self.fields['assignee'].label_from_instance = _label
        self.fields['assignee'].required = False

    def clean(self):
        c = super().clean()
        if c.get('project') and c.get('client') and c['project'].client_id != c['client'].pk:
            self.add_error('project', 'This project belongs to another client.')
        return c


PRIORITY_CHOICES = TASK_PRIORITIES
