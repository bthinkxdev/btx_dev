from django import forms

from .models import Achievement, FollowUp, Lead, Package, Task


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
