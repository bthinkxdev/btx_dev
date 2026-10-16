from django import forms

from .models import FollowUp, Lead, Package, Task


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


