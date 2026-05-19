from decimal import Decimal

from django import forms

from .models import BillPayment

_BILLING_INPUT = 'billing-input'
_BILLING_SELECT = 'billing-input billing-select'
_BILLING_TEXTAREA = 'billing-input billing-textarea'


class RecordPaymentForm(forms.Form):
    """Unified: record payment → receipt bill + PDF + email."""

    amount = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal('0.01'),
        label='Amount received (Rs.)',
        widget=forms.NumberInput(attrs={
            'step': '0.01',
            'placeholder': '16500.00',
            'class': _BILLING_INPUT,
        }),
    )
    description = forms.CharField(
        max_length=500,
        label='For (service / milestone)',
        widget=forms.TextInput(attrs={
            'placeholder': 'Ecommerce Website — milestone payment',
            'class': _BILLING_INPUT,
        }),
    )
    transaction_id = forms.CharField(
        max_length=120,
        label='Transaction ID',
        widget=forms.TextInput(attrs={
            'placeholder': 'UPI / bank reference number',
            'class': _BILLING_INPUT,
        }),
    )
    payment_method = forms.ChoiceField(
        choices=BillPayment.PaymentMethod.choices,
        initial=BillPayment.PaymentMethod.UPI,
        widget=forms.Select(attrs={'class': _BILLING_SELECT}),
    )
    payment_date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date', 'class': _BILLING_INPUT}),
    )
    proof_file = forms.FileField(
        required=False,
        label='Payment proof',
        help_text='Screenshot or PDF of the transaction',
        widget=forms.FileInput(attrs={'class': 'billing-file'}),
    )
    notes = forms.CharField(
        required=False,
        label='Notes on receipt',
        widget=forms.Textarea(attrs={
            'rows': 2,
            'placeholder': 'Optional note printed on receipt',
            'class': _BILLING_TEXTAREA,
        }),
    )
    gst_percent = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        initial=Decimal('0'),
        required=False,
        label='GST % (0 if not applicable)',
        widget=forms.NumberInput(attrs={'step': '0.01', 'class': _BILLING_INPUT}),
    )
    send_email = forms.BooleanField(
        required=False,
        initial=True,
        label='Email receipt to client',
        widget=forms.CheckboxInput(attrs={'class': 'billing-option-card__input'}),
    )
    invoice_only = forms.BooleanField(
        required=False,
        initial=False,
        label='Invoice only',
        widget=forms.CheckboxInput(attrs={'class': 'billing-option-card__input'}),
    )

    def clean(self):
        cleaned = super().clean()
        invoice_only = cleaned.get('invoice_only')
        if invoice_only:
            return cleaned
        if not cleaned.get('proof_file'):
            self.add_error(
                'proof_file',
                'Upload payment proof (screenshot or PDF), or tick “Invoice only”.',
            )
        if not (cleaned.get('transaction_id') or '').strip():
            self.add_error(
                'transaction_id',
                'Transaction ID is required (UPI / bank reference).',
            )
        if self.errors:
            raise forms.ValidationError(
                'Please fix the highlighted fields below.'
            )
        return cleaned


class StatementFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False,
        label='From',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'billing-input'}),
    )
    date_to = forms.DateField(
        required=False,
        label='To',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'billing-input'}),
    )

    def clean(self):
        cleaned = super().clean()
        d_from = cleaned.get('date_from')
        d_to = cleaned.get('date_to')
        if d_from and d_to and d_from > d_to:
            raise forms.ValidationError('"From" date must be before "To" date.')
        return cleaned
