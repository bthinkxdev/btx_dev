"""Finance module forms (income & expense tracking)."""

from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.forms import modelformset_factory
from django.utils import timezone

from .models import (
    Client,
    Expense,
    ExpenseCategory,
    Founder,
    FounderWithdrawal,
    FundTransfer,
    Income,
    IncomeCategory,
    Project,
    RevenueAllocationBucket,
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
            'funding_bucket',
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
            'funding_bucket': forms.Select(attrs={'class': _INPUT}),
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

        bucket_qs = (
            RevenueAllocationBucket.objects.filter(active=True)
            .exclude(code='founder_pool')
            .exclude(name='Founder Pool')
            .order_by('display_order', 'name')
        )
        if self.instance and self.instance.pk and self.instance.funding_bucket_id:
            bucket_qs = RevenueAllocationBucket.objects.filter(
                Q(active=True) | Q(pk=self.instance.funding_bucket_id)
            ).order_by('display_order', 'name')
        self.fields['funding_bucket'].queryset = bucket_qs
        self.fields['funding_bucket'].required = False
        self.fields['funding_bucket'].empty_label = 'Other'
        self.fields['funding_bucket'].label = 'Funding Source'
        self.fields['funding_bucket'].help_text = (
            'Internal fund this expense draws from (Other = no fund reduction). '
            'Founder Pool payouts use Funds → Founder Withdraw.'
        )

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount is None or amount <= Decimal('0'):
            raise forms.ValidationError('Amount must be greater than zero.')
        return amount

    def clean(self):
        cleaned = super().clean()
        amount = cleaned.get('amount')
        bucket = cleaned.get('funding_bucket')
        if amount and bucket:
            if bucket.code == 'founder_pool' or bucket.name == 'Founder Pool':
                self.add_error(
                    'funding_bucket',
                    'Founder Pool payouts require a founder account. '
                    'Use Funds → Founder Withdraw instead.',
                )
                return cleaned
            from .services.fund_management import available_bucket_balance

            exclude = self.instance if self.instance and self.instance.pk else None
            available = available_bucket_balance(bucket, exclude_expense=exclude)
            if amount > available:
                self.add_error(
                    'amount',
                    f'Insufficient balance in {bucket.name}. '
                    f'Available Rs. {available:.2f}.',
                )
        return cleaned


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


class FinanceStatementFilterForm(forms.Form):
    """Filters for the Finance Statement (cashbook) page."""

    period = forms.ChoiceField(
        required=False,
        label='Period',
        choices=(
            ('today', 'Today'),
            ('yesterday', 'Yesterday'),
            ('week', 'This Week'),
            ('month', 'This Month'),
            ('last_month', 'Last Month'),
            ('custom', 'Custom Date Range'),
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
    income_category = forms.ModelChoiceField(
        queryset=IncomeCategory.objects.none(),
        required=False,
        label='Income category',
        empty_label='All income categories',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    expense_category = forms.ModelChoiceField(
        queryset=ExpenseCategory.objects.none(),
        required=False,
        label='Expense category',
        empty_label='All expense categories',
        widget=forms.Select(attrs={'class': _INPUT}),
    )
    q = forms.CharField(
        required=False,
        label='Search',
        widget=forms.TextInput(
            attrs={'class': _INPUT, 'placeholder': 'Reference, vendor, notes, client…'}
        ),
    )
    sort = forms.ChoiceField(
        required=False,
        label='Sort',
        choices=(
            ('newest', 'Newest'),
            ('oldest', 'Oldest'),
            ('amount', 'Amount'),
        ),
        initial='newest',
        widget=forms.Select(attrs={'class': _INPUT}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['project'].queryset = Project.objects.select_related('client').order_by(
            '-created_at'
        )
        self.fields['project'].label_from_instance = (
            lambda p: f'#{p.pk} — {p.client.business_name}'
        )
        self.fields['client'].queryset = Client.objects.order_by('business_name')
        self.fields['income_category'].queryset = IncomeCategory.objects.filter(
            active=True
        ).order_by('name')
        self.fields['expense_category'].queryset = ExpenseCategory.objects.filter(
            active=True
        ).order_by('name')


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
        if report_type in ('income', 'client_revenue', 'profit', 'project_profitability'):
            self.fields['category'].queryset = IncomeCategory.objects.filter(
                active=True
            ).order_by('name')
            self.fields['category'].help_text = 'Income category'
        else:
            self.fields['category'].queryset = ExpenseCategory.objects.filter(
                active=True
            ).order_by('name')
            self.fields['category'].help_text = 'Expense category'


class AllocationDashboardFilterForm(forms.Form):
    period = forms.ChoiceField(
        required=False,
        choices=(
            ('today', 'Today'),
            ('week', 'This Week'),
            ('month', 'This Month'),
            ('year', 'This Year'),
            ('custom', 'Custom Range'),
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
        queryset=IncomeCategory.objects.none(),
        required=False,
        label='Income category',
        empty_label='All categories',
        widget=forms.Select(attrs={'class': _INPUT}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['project'].queryset = Project.objects.select_related('client').order_by(
            '-created_at'
        )
        self.fields['project'].label_from_instance = (
            lambda p: f'#{p.pk} — {p.client.business_name}'
        )
        self.fields['client'].queryset = Client.objects.order_by('business_name')
        self.fields['category'].queryset = IncomeCategory.objects.filter(
            active=True
        ).order_by('name')


class RevenueAllocationBucketForm(forms.ModelForm):
    class Meta:
        model = RevenueAllocationBucket
        fields = (
            'name',
            'percentage',
            'color',
            'display_order',
            'active',
            'usage_label',
        )
        widgets = {
            'name': forms.TextInput(attrs={'class': _INPUT}),
            'percentage': forms.NumberInput(
                attrs={'class': _INPUT, 'step': '0.01', 'min': '0', 'max': '100'}
            ),
            'color': forms.TextInput(attrs={'class': _INPUT, 'type': 'color'}),
            'display_order': forms.NumberInput(attrs={'class': _INPUT, 'min': '0'}),
            'active': forms.CheckboxInput(),
            'usage_label': forms.Select(attrs={'class': _INPUT}),
        }


AllocationBucketFormSet = modelformset_factory(
    RevenueAllocationBucket,
    form=RevenueAllocationBucketForm,
    extra=1,
    can_delete=False,
)


class FounderForm(forms.ModelForm):
    class Meta:
        model = Founder
        fields = ('name', 'percentage', 'active', 'display_order')
        widgets = {
            'name': forms.TextInput(attrs={'class': _INPUT}),
            'percentage': forms.NumberInput(
                attrs={'class': _INPUT, 'step': '0.01', 'min': '0', 'max': '100'}
            ),
            'active': forms.CheckboxInput(),
            'display_order': forms.NumberInput(attrs={'class': _INPUT, 'min': '0'}),
        }


FounderFormSet = modelformset_factory(
    Founder,
    form=FounderForm,
    extra=1,
    can_delete=False,
)


class FounderWithdrawalForm(forms.ModelForm):
    class Meta:
        model = FounderWithdrawal
        fields = ('founder', 'amount', 'withdrawal_date', 'reference', 'notes')
        widgets = {
            'founder': forms.Select(attrs={'class': _INPUT}),
            'amount': forms.NumberInput(
                attrs={'class': _INPUT, 'step': '0.01', 'min': '0.01'}
            ),
            'withdrawal_date': forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
            'reference': forms.TextInput(attrs={
                'class': _INPUT,
                'placeholder': 'UTR / cheque / reference',
            }),
            'notes': forms.Textarea(attrs={'class': _INPUT, 'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['founder'].queryset = Founder.objects.filter(active=True).order_by(
            'display_order', 'name'
        )
        self.fields['founder'].label = 'Founder account'
        self.fields['founder'].help_text = (
            'Required — Founder Pool payouts are drawn from a specific founder share.'
        )
        self.fields['founder'].empty_label = '— Select founder account —'
        if not self.is_bound:
            self.fields['withdrawal_date'].initial = timezone.localdate()

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount is None or amount <= Decimal('0'):
            raise forms.ValidationError('Amount must be greater than zero.')
        return amount

    def clean(self):
        cleaned = super().clean()
        founder = cleaned.get('founder')
        amount = cleaned.get('amount')
        if founder and amount:
            from .services.fund_management import founder_balance

            remaining = founder_balance(founder)['remaining']
            if amount > remaining:
                self.add_error(
                    'amount',
                    f'Insufficient balance for {founder.name}. '
                    f'Available Rs. {remaining:.2f}.',
                )
        return cleaned


class FundTransferForm(forms.ModelForm):
    class Meta:
        model = FundTransfer
        fields = ('from_bucket', 'to_bucket', 'amount', 'transfer_date', 'reason')
        widgets = {
            'from_bucket': forms.Select(attrs={'class': _INPUT}),
            'to_bucket': forms.Select(attrs={'class': _INPUT}),
            'amount': forms.NumberInput(
                attrs={'class': _INPUT, 'step': '0.01', 'min': '0.01'}
            ),
            'transfer_date': forms.DateInput(attrs={'type': 'date', 'class': _INPUT}),
            'reason': forms.TextInput(attrs={
                'class': _INPUT,
                'placeholder': 'Reason for transfer',
            }),
        }
        labels = {
            'from_bucket': 'From Fund',
            'to_bucket': 'To Fund',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = (
            RevenueAllocationBucket.objects.filter(active=True)
            .exclude(code='founder_pool')
            .exclude(name='Founder Pool')
            .order_by('display_order', 'name')
        )
        # Allow transfer INTO founder pool; block FROM (avoids share/pool desync).
        all_qs = RevenueAllocationBucket.objects.filter(active=True).order_by(
            'display_order', 'name'
        )
        self.fields['from_bucket'].queryset = qs
        self.fields['to_bucket'].queryset = all_qs
        self.fields['from_bucket'].help_text = (
            'Founder Pool cannot be the source — use Founder Withdraw for payouts.'
        )
        if not self.is_bound:
            self.fields['transfer_date'].initial = timezone.localdate()

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount is None or amount <= Decimal('0'):
            raise forms.ValidationError('Amount must be greater than zero.')
        return amount

    def clean(self):
        cleaned = super().clean()
        frm = cleaned.get('from_bucket')
        to = cleaned.get('to_bucket')
        amount = cleaned.get('amount')
        if frm and (frm.code == 'founder_pool' or frm.name == 'Founder Pool'):
            self.add_error(
                'from_bucket',
                'Cannot transfer out of Founder Pool. Use Founder Withdraw.',
            )
        if frm and to and frm.pk == to.pk:
            self.add_error('to_bucket', 'Cannot transfer a fund to itself.')
        if frm and amount:
            from .services.fund_management import available_bucket_balance

            available = available_bucket_balance(frm)
            if amount > available:
                self.add_error(
                    'amount',
                    f'Insufficient balance in {frm.name}. '
                    f'Available Rs. {available:.2f}.',
                )
        return cleaned


class FundPeriodFilterForm(forms.Form):
    period = forms.ChoiceField(
        required=False,
        choices=(
            ('month', 'This Month'),
            ('today', 'Today'),
            ('week', 'This Week'),
            ('year', 'This Year'),
            ('all', 'All time'),
            ('custom', 'Custom'),
        ),
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
    founder = forms.ModelChoiceField(
        queryset=Founder.objects.none(),
        required=False,
        empty_label='Select founder',
        widget=forms.Select(attrs={'class': _INPUT}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['founder'].queryset = Founder.objects.filter(active=True).order_by(
            'display_order', 'name'
        )
        if not self.is_bound:
            self.fields['period'].initial = 'month'


class IncomeCategoryForm(forms.ModelForm):
    class Meta:
        model = IncomeCategory
        fields = ('name', 'active')
        widgets = {
            'name': forms.TextInput(attrs={'class': _INPUT, 'placeholder': 'Category name'}),
            'active': forms.CheckboxInput(),
        }


class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ('name', 'active')
        widgets = {
            'name': forms.TextInput(attrs={'class': _INPUT, 'placeholder': 'Category name'}),
            'active': forms.CheckboxInput(),
        }

