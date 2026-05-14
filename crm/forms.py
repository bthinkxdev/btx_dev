from decimal import Decimal
import re

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import (
    Achievement,
    AuditEntry,
    ChangeRequest,
    Client,
    FollowUp,
    Lead,
    OnboardingSubmission,
    Package,
    PackageScope,
    Project,
    ProjectCredential,
    ProjectHandover,
    ProvisioningStep,
    RenewalTracker,
    Task,
)
from .services.scope import FEATURE_KEY_MAP, FEATURE_LABELS

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
        if employee:
            self.fields['package'].queryset = Package.objects.filter(employee=employee)


class FollowUpForm(forms.ModelForm):
    class Meta:
        model = FollowUp
        fields = ('datetime', 'note')
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
        fields = ('title', 'due_date')
        widgets = {
            'due_date': forms.DateInput(attrs={'type': 'date'}),
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


class QuickNoteForm(forms.Form):
    quick_note = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={'placeholder': 'Quick note…'}),
    )


class AchievementForm(forms.ModelForm):
    class Meta:
        model = Achievement
        fields = ('lead', 'package', 'amount', 'achieved_date', 'notes')
        widgets = {
            'achieved_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee is not None:
            self.fields['lead'].queryset = Lead.objects.filter(employee=employee)
            self.fields['package'].queryset = Package.objects.filter(employee=employee)


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = (
            'business_name',
            'contact_person',
            'phone',
            'email',
            'gst_number',
            'pan_number',
            'address',
        )
        widgets = {
            'address': forms.Textarea(attrs={'rows': 3}),
        }


class ProjectForm(forms.ModelForm):
    lead = forms.ModelChoiceField(
        queryset=Lead.objects.none(),
        label='Lead',
    )

    class Meta:
        model = Project
        fields = (
            'package',
            'deal_value',
            'advance_received',
            'assigned_to',
            'notes',
        )
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    field_order = (
        'lead',
        'package',
        'deal_value',
        'advance_received',
        'assigned_to',
        'notes',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            eligible = Q(
                status__in=(
                    Lead.Status.CLOSED,
                    Lead.Status.ADVANCE_RECEIVED_PROJECT_STARTED,
                )
            )
            if user.is_superuser or user.is_staff:
                lead_qs = Lead.objects.filter(eligible)
                pkg_qs = Package.objects.all()
            else:
                lead_qs = Lead.objects.filter(eligible, employee=user)
                pkg_qs = Package.objects.filter(employee=user)
            self.fields['lead'].queryset = lead_qs.order_by('-updated_at')
            self.fields['package'].queryset = pkg_qs.order_by('name')
            self.fields['assigned_to'].queryset = User.objects.filter(
                is_active=True
            ).order_by('username')
            self.fields['assigned_to'].required = False

    def clean_lead(self):
        lead = self.cleaned_data.get('lead')
        if lead is None:
            return lead
        allowed = (
            Lead.Status.CLOSED,
            Lead.Status.ADVANCE_RECEIVED_PROJECT_STARTED,
        )
        if lead.status not in allowed:
            raise forms.ValidationError(
                'Lead must be in Closed or Advance received & project started state.'
            )
        return lead

    def clean(self):
        cleaned = super().clean()
        deal = cleaned.get('deal_value')
        adv = cleaned.get('advance_received')
        if deal is not None and adv is not None and adv > deal:
            raise forms.ValidationError(
                'Advance received cannot be greater than deal value.'
            )
        return cleaned


WEBSITE_REQUIREMENT_KEYS = (
    'login',
    'cart',
    'wishlist',
    'payment_gateway',
    'booking',
    'blog',
    'chat',
    'admin_dashboard',
    'reports',
    'subscription',
)


class OnboardingBusinessInfoForm(forms.ModelForm):
    class Meta:
        model = OnboardingSubmission
        fields = (
            'business_name',
            'tagline',
            'business_description',
            'years_in_business',
            'industry',
            'target_audience',
            'competitors',
            'usp',
        )
        widgets = {
            'business_description': forms.Textarea(attrs={'rows': 4}),
            'target_audience': forms.Textarea(attrs={'rows': 3}),
            'competitors': forms.Textarea(attrs={'rows': 3}),
            'usp': forms.Textarea(attrs={'rows': 3}),
        }


class OnboardingContactForm(forms.ModelForm):
    class Meta:
        model = OnboardingSubmission
        fields = (
            'contact_phone',
            'whatsapp_number',
            'contact_email',
            'office_address',
            'google_maps_url',
            'instagram_url',
            'facebook_url',
            'youtube_url',
            'website_url',
        )
        widgets = {
            'office_address': forms.Textarea(attrs={'rows': 3}),
        }


class OnboardingBrandingForm(forms.ModelForm):
    class Meta:
        model = OnboardingSubmission
        fields = ('brand_colors', 'brand_fonts', 'brand_notes')
        widgets = {
            'brand_notes': forms.Textarea(attrs={'rows': 3}),
        }


class OnboardingRequirementsForm(forms.ModelForm):
    class Meta:
        model = OnboardingSubmission
        fields = ('reference_websites',)
        widgets = {
            'reference_websites': forms.Textarea(attrs={'rows': 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        raw = {}
        if self.instance.pk:
            raw = self.instance.website_requirements or {}
        if not isinstance(raw, dict):
            raw = {}
        for key in WEBSITE_REQUIREMENT_KEYS:
            self.fields[f'req_{key}'] = forms.BooleanField(
                required=False,
                label=key.replace('_', ' ').title(),
                initial=bool(raw.get(key)),
            )

    def clean(self):
        cleaned = super().clean()
        packed = {
            key: bool(cleaned.get(f'req_{key}')) for key in WEBSITE_REQUIREMENT_KEYS
        }
        cleaned['website_requirements'] = packed
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data is not None:
            instance.website_requirements = self.cleaned_data.get(
                'website_requirements', {}
            )
        if commit:
            instance.save()
        return instance


class OnboardingContentForm(forms.ModelForm):
    class Meta:
        model = OnboardingSubmission
        fields = (
            'about_us',
            'privacy_policy',
            'refund_policy',
            'shipping_policy',
            'terms_and_conditions',
            'faq',
        )
        widgets = {
            'about_us': forms.Textarea(attrs={'rows': 4}),
            'privacy_policy': forms.Textarea(attrs={'rows': 4}),
            'refund_policy': forms.Textarea(attrs={'rows': 4}),
            'shipping_policy': forms.Textarea(attrs={'rows': 4}),
            'terms_and_conditions': forms.Textarea(attrs={'rows': 4}),
            'faq': forms.Textarea(attrs={'rows': 4}),
        }


class OnboardingDocumentForm(forms.ModelForm):
    class Meta:
        model = OnboardingSubmission
        fields = (
            'logo_file',
            'gst_certificate',
            'pan_document',
            'business_registration',
            'brand_guidelines',
        )
        widgets = {
            'logo_file': forms.ClearableFileInput(
                attrs={
                    'class': 'ob-file-input',
                    'accept': 'image/*,.svg,.webp',
                }
            ),
            'gst_certificate': forms.ClearableFileInput(
                attrs={
                    'class': 'ob-file-input',
                    'accept': 'image/*,.pdf,.doc,.docx,application/pdf',
                }
            ),
            'pan_document': forms.ClearableFileInput(
                attrs={
                    'class': 'ob-file-input',
                    'accept': 'image/*,.pdf,.doc,.docx,application/pdf',
                }
            ),
            'business_registration': forms.ClearableFileInput(
                attrs={
                    'class': 'ob-file-input',
                    'accept': 'image/*,.pdf,.doc,.docx,application/pdf',
                }
            ),
            'brand_guidelines': forms.ClearableFileInput(
                attrs={
                    'class': 'ob-file-input',
                    'accept': 'image/*,.pdf,.doc,.docx,application/pdf',
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for _name, field in self.fields.items():
            w = field.widget
            if isinstance(w, forms.ClearableFileInput):
                w.clear_checkbox_label = 'Remove uploaded file'


class OnboardingPaymentKycForm(forms.ModelForm):
    """Owner Aadhaar, bank details, registered phone numbers — payment gateway KYC."""

    class Meta:
        model = OnboardingSubmission
        fields = (
            'owner_aadhaar_front',
            'owner_aadhaar_back',
            'bank_name',
            'bank_account_number',
            'bank_ifsc',
            'bank_branch_name',
            'phone_registered_bank',
            'phone_registered_aadhaar',
            'phone_registered_pan',
            'kyc_privacy_acknowledged',
        )
        labels = {
            'owner_aadhaar_front': "Owner's Aadhaar (front)",
            'owner_aadhaar_back': "Owner's Aadhaar (back)",
            'bank_name': 'Bank name',
            'bank_account_number': 'Bank account number',
            'bank_ifsc': 'IFSC code',
            'bank_branch_name': 'Branch name',
            'phone_registered_bank': 'Phone number registered with bank',
            'phone_registered_aadhaar': 'Phone number registered with Aadhaar',
            'phone_registered_pan': 'Phone number registered with PAN',
            'kyc_privacy_acknowledged': 'I confirm I have read the notice and undertaking below',
        }
        help_texts = {
            'bank_ifsc': '11-character Indian Financial System Code (e.g. SBIN0001234).',
            'kyc_privacy_acknowledged': (
                'Required before final onboarding submission (you can save the section first, '
                'then tick this when you are ready).'
            ),
        }
        widgets = {
            'owner_aadhaar_front': forms.ClearableFileInput(
                attrs={
                    'class': 'ob-file-input',
                    'accept': 'image/*,.pdf,application/pdf',
                }
            ),
            'owner_aadhaar_back': forms.ClearableFileInput(
                attrs={
                    'class': 'ob-file-input',
                    'accept': 'image/*,.pdf,application/pdf',
                }
            ),
            'bank_name': forms.TextInput(attrs={'maxlength': 120, 'autocomplete': 'organization'}),
            'bank_account_number': forms.TextInput(attrs={'maxlength': 34, 'autocomplete': 'off'}),
            'bank_ifsc': forms.TextInput(attrs={'maxlength': 11, 'autocomplete': 'off', 'class': 'ob-ifsc-input'}),
            'bank_branch_name': forms.TextInput(attrs={'maxlength': 200}),
            'phone_registered_bank': forms.TextInput(attrs={'maxlength': 40, 'inputmode': 'tel', 'autocomplete': 'tel'}),
            'phone_registered_aadhaar': forms.TextInput(
                attrs={'maxlength': 40, 'inputmode': 'tel', 'autocomplete': 'tel'}
            ),
            'phone_registered_pan': forms.TextInput(attrs={'maxlength': 40, 'inputmode': 'tel', 'autocomplete': 'tel'}),
            'kyc_privacy_acknowledged': forms.CheckboxInput(attrs={'class': 'ob-kyc-check'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['kyc_privacy_acknowledged'].required = False
        for _name, field in self.fields.items():
            w = field.widget
            if isinstance(w, forms.ClearableFileInput):
                w.clear_checkbox_label = 'Remove uploaded file'

    def clean_bank_ifsc(self):
        raw = (self.cleaned_data.get('bank_ifsc') or '').strip().upper()
        if not raw:
            return raw
        if not re.match(r'^[A-Z]{4}0[A-Z0-9]{6}$', raw):
            raise forms.ValidationError(
                'Enter a valid 11-character IFSC (four letters, digit 0, then six alphanumeric).'
            )
        return raw

    def clean_bank_account_number(self):
        raw = (self.cleaned_data.get('bank_account_number') or '').strip()
        if not raw:
            return raw
        if len(raw) < 5:
            raise forms.ValidationError('Account number looks too short.')
        return raw


class OnboardingAgreementForm(forms.Form):
    terms_accepted = forms.BooleanField(required=True)
    terms_version = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.HiddenInput(),
        initial='1.0',
    )


class LeadConvertForm(forms.Form):
    package = forms.ModelChoiceField(queryset=Package.objects.none())
    deal_value = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0'))
    advance_received = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal('0'),
        initial=Decimal('0'),
        required=False,
    )
    assigned_to = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2}),
    )

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee is not None:
            self.fields['package'].queryset = Package.objects.filter(
                employee=employee
            ).order_by('name')
        self.fields['assigned_to'].queryset = User.objects.filter(
            is_active=True
        ).order_by('username')

    def clean(self):
        cleaned = super().clean()
        deal = cleaned.get('deal_value')
        adv = cleaned.get('advance_received') or Decimal('0')
        cleaned['advance_received'] = adv
        if deal is not None and adv > deal:
            raise forms.ValidationError(
                'Advance received cannot be greater than deal value.'
            )
        return cleaned


class ProvisioningStepStatusForm(forms.Form):
    step_key = forms.CharField(max_length=80, widget=forms.HiddenInput())
    status = forms.ChoiceField(choices=ProvisioningStep.Status.choices)
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Optional notes'}),
    )


class ProjectCredentialForm(forms.ModelForm):
    password_plain = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text='Leave blank to keep existing password when editing.',
    )
    secret_plain = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text='Leave blank to keep existing secret when editing.',
    )

    class Meta:
        model = ProjectCredential
        fields = (
            'label',
            'category',
            'credential_type',
            'username',
            'login_url',
            'is_client_visible',
            'visibility_level',
            'provider_type',
            'provider_name',
            'expires_at',
            'notes',
        )
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 2}),
            'expires_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['expires_at'].required = False
        if self.fields.get('expires_at'):
            self.fields['expires_at'].input_formats = [
                '%Y-%m-%dT%H:%M',
                '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M',
            ]
        self.fields['visibility_level'].help_text = (
            'Use “Shared (tenant portal)” for credentials that should appear in the client handover portal.'
        )


class ProjectHandoverForm(forms.ModelForm):
    class Meta:
        model = ProjectHandover
        fields = (
            'handover_notes',
            'client_notified',
            'tenant_visibility_enabled',
            'live_site_url',
            'admin_site_url',
            'support_contact',
            'sla_summary',
            'handover_pdf',
        )
        widgets = {
            'handover_notes': forms.Textarea(attrs={'rows': 4}),
            'sla_summary': forms.TextInput(attrs={'placeholder': 'Short SLA summary'}),
        }


class RenewalTrackerForm(forms.ModelForm):
    class Meta:
        model = RenewalTracker
        fields = ('subject_type', 'title', 'expires_at', 'renewal_url', 'notes')
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 2}),
            'expires_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['expires_at'].input_formats = [
            '%Y-%m-%dT%H:%M',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
        ]


class PortalActivateForm(forms.Form):
    """Confirmation-only POST for portal activation (CSRF protected)."""
    pass


class RenewalFilterForm(forms.Form):
    status = forms.ChoiceField(
        choices=[
            ('', 'All'),
            ('active', 'Active'),
            ('expired', 'Expired'),
            ('suspended', 'Suspended'),
        ],
        required=False,
        widget=forms.Select(attrs={'class': 'btn btn-outline btn-sm', 'style': 'padding:8px 12px;'}),
    )
    window = forms.ChoiceField(
        choices=[
            ('', 'All'),
            ('30d', 'Expiring in 30 days'),
            ('7d', 'Expiring in 7 days'),
            ('overdue', 'Overdue'),
        ],
        required=False,
        widget=forms.Select(attrs={'class': 'btn btn-outline btn-sm', 'style': 'padding:8px 12px;'}),
    )


class ChangeRequestPortalForm(forms.Form):
    title = forms.CharField(max_length=200)
    request_type = forms.ChoiceField(choices=ChangeRequest.RequestType.choices)
    description = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}))


class ChangeRequestStaffForm(forms.ModelForm):
    class Meta:
        model = ChangeRequest
        fields = ('title', 'request_type', 'description', 'assigned_to')
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assigned_to'].required = False
        self.fields['assigned_to'].queryset = User.objects.filter(is_active=True).order_by(
            'username'
        )


class TriageForm(forms.Form):
    requested_features = forms.MultipleChoiceField(
        choices=[
            (k, FEATURE_LABELS.get(k, k.replace('_', ' ').title()))
            for k in FEATURE_KEY_MAP
        ],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )


class QuoteForm(forms.Form):
    quoted_amount = forms.DecimalField(max_digits=12, decimal_places=2)
    quote_notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'rows': 2})
    )


class RejectForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}))


class CompleteForm(forms.Form):
    resolution_note = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}))


class PackageScopeForm(forms.ModelForm):
    class Meta:
        model = PackageScope
        exclude = ('package', 'updated_at')
        widgets = {
            'scope_notes': forms.Textarea(attrs={'rows': 2}),
            'exclusions': forms.Textarea(attrs={'rows': 2}),
        }


class AuditFilterForm(forms.Form):
    category = forms.ChoiceField(
        choices=[('', 'All')],
        required=False,
    )
    actor = forms.ModelChoiceField(queryset=None, required=False)
    date_from = forms.DateField(
        required=False, widget=forms.DateInput(attrs={'type': 'date'})
    )
    date_to = forms.DateField(
        required=False, widget=forms.DateInput(attrs={'type': 'date'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].choices = [('', 'All')] + list(
            AuditEntry.EventCategory.choices
        )
        self.fields['actor'].queryset = User.objects.filter(is_active=True).order_by(
            'username'
        )


class ChangeRequestFilterForm(forms.Form):
    status = forms.ChoiceField(
        choices=[('', 'All')],
        required=False,
    )
    request_type = forms.ChoiceField(
        choices=[('', 'All')],
        required=False,
    )
    project = forms.ModelChoiceField(queryset=None, required=False)

    def __init__(self, *args, project_qs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['status'].choices = [('', 'All')] + list(ChangeRequest.Status.choices)
        self.fields['request_type'].choices = [
            ('', 'All')
        ] + list(ChangeRequest.RequestType.choices)
        self.fields['project'].queryset = project_qs or Project.objects.none()
