from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from .models import EmployeeProfile, FollowUp, Lead, Package, Task

User = get_user_model()


class LeadForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ('name', 'phone', 'email', 'source', 'status', 'package', 'deal_value', 'notes')
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['package'].queryset = Package.objects.all().order_by('name')
        self.fields['status'].choices = Lead.PRIMARY_STATUS_CHOICES

    def clean_phone(self):
        # Duplicate phones are not blocked here — the view offers a
        # "possible duplicate / open existing or create new anyway" prompt
        # instead of a hard validation error (crm.views.lead_create).
        return (self.cleaned_data.get('phone') or '').strip()

    def find_duplicate(self):
        """Existing lead sharing this phone number, if any (global, not per-employee)."""
        phone = (self.data.get('phone') or '').strip()
        if not phone:
            return None
        dup = Lead.objects.filter(phone=phone)
        if self.instance.pk:
            dup = dup.exclude(pk=self.instance.pk)
        return dup.select_related('employee').first()


class FollowUpForm(forms.ModelForm):
    class Meta:
        model = FollowUp
        fields = ('datetime', 'type', 'note')
        widgets = {
            'datetime': forms.DateTimeInput(
                attrs={'type': 'datetime-local'},
                format='%Y-%m-%dT%H:%M',
            ),
            'note': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['datetime'].input_formats = [
            '%Y-%m-%dT%H:%M',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
        ]


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ('title', 'due_date', 'due_time')
        widgets = {
            'due_date': forms.DateInput(attrs={'type': 'date'}),
            'due_time': forms.TimeInput(attrs={'type': 'time'}),
        }


class PackageForm(forms.ModelForm):
    class Meta:
        model = Package
        fields = ('name', 'price')


class ExcelImportForm(forms.Form):
    file = forms.FileField(label='Excel file (.xlsx)')


class RescheduleFollowUpForm(forms.Form):
    new_datetime = forms.DateTimeField(
        input_formats=[
            '%Y-%m-%dT%H:%M',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
        ],
        widget=forms.DateTimeInput(
            attrs={'type': 'datetime-local'},
            format='%Y-%m-%dT%H:%M',
        ),
    )


_DT_FORMATS = [
    '%Y-%m-%dT%H:%M',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
]


class QuickFollowUpForm(forms.Form):
    fu_datetime = forms.DateTimeField(input_formats=_DT_FORMATS)
    fu_note = forms.CharField(required=False, max_length=500, widget=forms.TextInput(attrs={'placeholder': 'Optional'}))


class StaffCreateForm(forms.Form):
    """Creates the User and its EmployeeProfile together — staff don't exist
    as one without the other anywhere else in the CRM."""
    username = forms.CharField(max_length=150)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    password = forms.CharField(widget=forms.PasswordInput, min_length=8)
    crm_role = forms.ChoiceField(choices=EmployeeProfile._meta.get_field('crm_role').choices)
    target_amount = forms.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0)
    is_sales_manager = forms.BooleanField(required=False)
    eligible_for_leads = forms.BooleanField(required=False)
    whatsapp_bot_enabled = forms.BooleanField(required=False)
    photo = forms.ImageField(required=False)

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('That username is already taken.')
        return username

    def clean_password(self):
        password = self.cleaned_data['password']
        validate_password(password)
        return password


class StaffEditForm(forms.Form):
    """Everything about an existing staff member in one save — profile
    fields plus the handful of User fields the CRM actually surfaces
    (name/email/active); username and superuser status stay admin-only
    via Django admin, not this screen."""
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    is_active = forms.BooleanField(required=False)
    new_password = forms.CharField(required=False, widget=forms.PasswordInput)
    crm_role = forms.ChoiceField(choices=EmployeeProfile._meta.get_field('crm_role').choices)
    target_amount = forms.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0)
    is_sales_manager = forms.BooleanField(required=False)
    eligible_for_leads = forms.BooleanField(required=False)
    whatsapp_bot_enabled = forms.BooleanField(required=False)
    photo = forms.ImageField(required=False)

    def clean_new_password(self):
        password = self.cleaned_data.get('new_password')
        if password:
            validate_password(password)
        return password


