from django import forms
from django.core.exceptions import ValidationError

from .models import ContactSubmission, JobApplication, JobPosting, NewsletterSubscriber

MAX_RESUME_BYTES = 5 * 1024 * 1024


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


class JobApplicationForm(forms.ModelForm):
    """Careers page: apply with resume (multipart)."""

    class Meta:
        model = JobApplication
        fields = ['job', 'full_name', 'email', 'phone', 'linkedin_url', 'cover_message', 'resume']
        widgets = {
            'job': forms.Select(attrs={'class': 'form-control', 'id': 'careerJob'}),
            'full_name': forms.TextInput(attrs={'class': 'form-control', 'id': 'careerName', 'placeholder': 'Your full name'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'id': 'careerEmail', 'placeholder': 'you@email.com'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'id': 'careerPhone', 'placeholder': '+91 …'}),
            'linkedin_url': forms.URLInput(attrs={'class': 'form-control', 'id': 'careerLinkedin', 'placeholder': 'https://linkedin.com/in/…'}),
            'cover_message': forms.Textarea(attrs={'class': 'form-control', 'id': 'careerCover', 'rows': 4, 'placeholder': 'Why BThinkX? What draws you to this role?'}),
            'resume': forms.ClearableFileInput(attrs={'class': 'form-control career-file-input', 'id': 'careerResume', 'accept': '.pdf,.doc,.docx,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['job'].queryset = JobPosting.objects.filter(is_published=True).order_by(
            'sort_order', '-created_at'
        )
        self.fields['job'].required = False
        self.fields['job'].empty_label = 'General application (no specific role)'
        self.fields['linkedin_url'].required = False
        self.fields['cover_message'].required = False

    def clean_resume(self):
        f = self.cleaned_data['resume']
        if f.size > MAX_RESUME_BYTES:
            raise ValidationError('Resume must be 5MB or smaller.')
        return f


class NewsletterSubscribeForm(forms.ModelForm):
    class Meta:
        model = NewsletterSubscriber
        fields = ['email']
        widgets = {
            'email': forms.EmailInput(
                attrs={
                    'placeholder': 'your@email.com',
                    'autocomplete': 'email',
                    'class': 'newsletter-email-input',
                }
            ),
        }

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        return email

    def save(self, commit=True):
        email = self.cleaned_data['email']
        sub, created = NewsletterSubscriber.objects.get_or_create(
            email=email,
            defaults={'is_active': True, 'source': 'blog'},
        )
        if not created and not sub.is_active:
            sub.is_active = True
            sub.source = 'blog'
            if commit:
                sub.save(update_fields=['is_active', 'source'])
        return sub
