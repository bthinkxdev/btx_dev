"""Finance module forms (income & expense tracking)."""

from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import (
    Client,
    Expense,
    ExpenseCategory,
    Income,
    IncomeCategory,
    Project,
)

User = get_user_model()
_INPUT = 'form-control'


class IncomeForm(forms.ModelForm):
    class Meta:
        model = Income
        fields = (
            'client',
            'project',
            'category',
            'amount',
            'payment_type',
            'payment_status',
            'payment_date',
            'bank_account',
            'reference',
            'notes',
        )
        widgets = {
            'client': forms.Select(attrs={'class': _INPUT}),
            'project': forms.Select(attrs={'class': _INPUT}),
            'category': forms.Select(attrs={'class': _INPUT}),
            'amount': forms.NumberInput(attrs={
                'class': _INPUT,
                'step': '0.01',
                'min': '0.01',
                'placeholder': '0.00',
            }),
            'payment_type': forms.Select(attrs={'class': _INPUT}),
            'payment_status': forms.Select(attrs={'class': _INPUT}),
            'payment_date': forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
            'bank_account': forms.TextInput(attrs={
                'class': _INPUT,
                'placeholder': 'Account / wallet label',
            }),
            'reference': forms.TextInput(attrs={
                'class': _INPUT,
                'placeholder': 'UPI / cheque / bank reference',
            }),
            'notes': forms.Textarea(attrs={
                'class': _INPUT,
                'rows': 3,
                'placeholder': 'Optional notes',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['client'].queryset = Client.objects.order_by('business_name')
        self.fields['client'].required = False
        self.fields['client'].empty_label = '— No client —'

        self.fields['project'].queryset = Project.objects.select_related('client').order_by(
            '-created_at'
        )
        self.fields['project'].required = False
        self.fields['project'].empty_label = '— No project —'
        self.fields['project'].label_from_instance = (
            lambda p: f'#{p.pk} — {p.client.business_name}'
            + (f' ({p.package.name})' if p.package_id else '')
        )

        cat_qs = IncomeCategory.objects.filter(active=True)
        if self.instance and self.instance.pk and self.instance.category_id:
            cat_qs = IncomeCategory.objects.filter(
                Q(active=True) | Q(pk=self.instance.category_id)
            )
        self.fields['category'].queryset = cat_qs.order_by('name')

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount is None or amount <= Decimal('0'):
            raise forms.ValidationError('Amount must be greater than zero.')
        return amount

    def clean(self):
        cleaned = super().clean()
        client = cleaned.get('client')
        project = cleaned.get('project')
        if project and client and project.client_id != client.pk:
            self.add_error(
                'project',
                'Selected project does not belong to the selected client.',
            )
        elif project and not client:
            cleaned['client'] = project.client
        return cleaned


class IncomeFilterForm(forms.Form):
    q = forms.CharField(
        required=False,
        label='Search',
        widget=forms.TextInput(attrs={
            'class': _INPUT,
            'placeholder': 'Reference, notes, client…',
        }),
    )
    category = forms.ModelChoiceField(
        queryset=IncomeCategory.objects.none(),
        required=False,
        empty_label='All categories',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    payment_type = forms.ChoiceField(
        required=False,
        choices=[('', 'All types')] + list(Income.PaymentType.choices),
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    payment_status = forms.ChoiceField(
        required=False,
        choices=[('', 'All statuses')] + list(Income.PaymentStatus.choices),
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    period = forms.ChoiceField(
        required=False,
        choices=(
            ('month', 'This Month'),
            ('today', 'Today'),
            ('week', 'This Week'),
            ('custom', 'Custom Date'),
            ('all', 'All time'),
        ),
        initial='month',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = IncomeCategory.objects.filter(
            active=True
        ).order_by('name')


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = (
            'project',
            'employee',
            'vendor',
            'category',
            'amount',
            'paid_from',
            'payment_method',
            'expense_date',
            'receipt',
            'notes',
        )
        widgets = {
            'project': forms.Select(attrs={'class': _INPUT}),
            'employee': forms.Select(attrs={'class': _INPUT}),
            'vendor': forms.TextInput(attrs={
                'class': _INPUT,
                'placeholder': 'Vendor / payee name',
            }),
            'category': forms.Select(attrs={'class': _INPUT}),
            'amount': forms.NumberInput(attrs={
                'class': _INPUT,
                'step': '0.01',
                'min': '0.01',
                'placeholder': '0.00',
            }),
            'paid_from': forms.TextInput(attrs={
                'class': _INPUT,
                'placeholder': 'Account / wallet paid from',
            }),
            'payment_method': forms.Select(attrs={'class': _INPUT}),
            'expense_date': forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
            'receipt': forms.ClearableFileInput(attrs={
                'class': _INPUT,
                'accept': 'image/*,.pdf',
            }),
            'notes': forms.Textarea(attrs={
                'class': _INPUT,
                'rows': 3,
                'placeholder': 'Optional notes',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['project'].queryset = Project.objects.select_related(
            'client', 'package'
        ).order_by('-created_at')
        self.fields['project'].required = False
        self.fields['project'].empty_label = '— No project —'
        self.fields['project'].label_from_instance = (
            lambda p: f'#{p.pk} — {p.client.business_name}'
            + (f' ({p.package.name})' if p.package_id else '')
        )

        self.fields['employee'].queryset = User.objects.filter(
            is_active=True
        ).order_by('first_name', 'username')
        self.fields['employee'].required = False
        self.fields['employee'].empty_label = '— No employee —'
        self.fields['employee'].label_from_instance = (
            lambda u: u.get_full_name() or u.get_username()
        )

        cat_qs = ExpenseCategory.objects.filter(active=True)
        if self.instance and self.instance.pk and self.instance.category_id:
            cat_qs = ExpenseCategory.objects.filter(
                Q(active=True) | Q(pk=self.instance.category_id)
            )
        self.fields['category'].queryset = cat_qs.order_by('name')
        self.fields['receipt'].required = False

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount is None or amount <= Decimal('0'):
            raise forms.ValidationError('Amount must be greater than zero.')
        return amount


class ExpenseFilterForm(forms.Form):
    q = forms.CharField(
        required=False,
        label='Search',
        widget=forms.TextInput(attrs={
            'class': _INPUT,
            'placeholder': 'Vendor, notes, paid from…',
        }),
    )
    category = forms.ModelChoiceField(
        queryset=ExpenseCategory.objects.none(),
        required=False,
        empty_label='All categories',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    payment_method = forms.ChoiceField(
        required=False,
        choices=[('', 'All methods')] + list(Expense.PaymentMethod.choices),
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    project = forms.ModelChoiceField(
        queryset=Project.objects.none(),
        required=False,
        empty_label='All projects',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    employee = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label='All employees',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    period = forms.ChoiceField(
        required=False,
        choices=(
            ('month', 'This Month'),
            ('today', 'Today'),
            ('week', 'This Week'),
            ('custom', 'Custom Date'),
            ('all', 'All time'),
        ),
        initial='month',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = ExpenseCategory.objects.filter(
            active=True
        ).order_by('name')
        self.fields['project'].queryset = Project.objects.select_related('client').order_by(
            '-created_at'
        )
        self.fields['project'].label_from_instance = (
            lambda p: f'#{p.pk} — {p.client.business_name}'
        )
        self.fields['employee'].queryset = User.objects.filter(is_active=True).order_by(
            'first_name', 'username'
        )
        self.fields['employee'].label_from_instance = (
            lambda u: u.get_full_name() or u.get_username()
        )


class FinancePeriodFilterForm(forms.Form):
    """Shared period filter for expense dashboard / reports."""

    period = forms.ChoiceField(
        required=False,
        choices=(
            ('month', 'This Month'),
            ('today', 'Today'),
            ('week', 'This Week'),
            ('custom', 'Custom Date'),
        ),
        initial='month',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
    )
    year = forms.IntegerField(
        required=False,
        widget=forms.NumberInput(attrs={'class': _INPUT, 'min': 2020, 'max': 2100}),
    )


class FinanceReportFilterForm(forms.Form):
    """Interactive filters for management reports."""

    period = forms.ChoiceField(
        required=False,
        label='Period',
        choices=(
            ('day', 'Daily'),
            ('week', 'Weekly'),
            ('month', 'Monthly'),
            ('year', 'Yearly'),
            ('custom', 'Custom Range'),
        ),
        initial='month',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    date_from = forms.DateField(
        required=False,
        label='From',
        widget=forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
    )
    date_to = forms.DateField(
        required=False,
        label='To',
        widget=forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
    )
    project = forms.ModelChoiceField(
        queryset=Project.objects.none(),
        required=False,
        empty_label='All projects',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    client = forms.ModelChoiceField(
        queryset=Client.objects.none(),
        required=False,
        empty_label='All clients',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    category = forms.ModelChoiceField(
        queryset=ExpenseCategory.objects.none(),
        required=False,
        empty_label='All categories',
        widget=forms.Select(attrs={'class': _INPUT}),
        help_text='Expense category (or income category on income reports)',
    )
    employee = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label='All employees',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    payment_method = forms.ChoiceField(
        required=False,
        label='Payment method',
        choices=[('', 'All methods')] + list(Expense.PaymentMethod.choices),
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    payment_type = forms.ChoiceField(
        required=False,
        label='Income type',
        choices=[('', 'All types')] + list(Income.PaymentType.choices),
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    payment_status = forms.ChoiceField(
        required=False,
        label='Payment status',
        choices=[('', 'All statuses')] + list(Income.PaymentStatus.choices),
        widget=forms.Select(attrs={'class': _INPUT}),
    )

    def __init__(self, *args, report_type: str = '', **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['project'].queryset = Project.objects.select_related('client').order_by(
            '-created_at'
        )
        self.fields['project'].label_from_instance = (
            lambda p: f'#{p.pk} — {p.client.business_name}'
        )
        self.fields['client'].queryset = Client.objects.order_by('business_name')
        self.fields['employee'].queryset = User.objects.filter(is_active=True).order_by(
            'first_name', 'username'
        )
        self.fields['employee'].label_from_instance = (
            lambda u: u.get_full_name() or u.get_username()
        )
        # Category queryset depends on report flavour
        if report_type in ('income', 'client_revenue', 'top_customers', 'top_projects', 'profit'):
            self.fields['category'].queryset = IncomeCategory.objects.filter(
                active=True
            ).order_by('name')
            self.fields['category'].help_text = 'Income category'
        else:
            self.fields['category'].queryset = ExpenseCategory.objects.filter(
                active=True
            ).order_by('name')
            self.fields['category'].help_text = 'Expense category'

