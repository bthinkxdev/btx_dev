from django import forms
from .models import ContactSubmission


class ContactForm(forms.ModelForm):
    """Form for the contact page, matches the template field names."""

    class Meta:
        model = ContactSubmission
        fields = ['name', 'email', 'company', 'budget', 'project_type', 'message', 'timeline']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'John Smith', 'class': 'form-control', 'id': 'name'}),
            'email': forms.EmailInput(attrs={'placeholder': 'john@company.com', 'class': 'form-control', 'id': 'email'}),
            'company': forms.TextInput(attrs={'placeholder': 'Your Company', 'class': 'form-control', 'id': 'company'}),
            'budget': forms.Select(attrs={'class': 'form-control', 'id': 'budget'}),
            'project_type': forms.Select(attrs={'class': 'form-control', 'id': 'projectType'}),
            'message': forms.Textarea(attrs={'placeholder': "Tell us about your project...", 'class': 'form-control', 'id': 'message', 'rows': 5}),
            'timeline': forms.Select(attrs={'class': 'form-control', 'id': 'timeline'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['budget'].required = False
        self.fields['company'].required = False
        self.fields['timeline'].required = False
