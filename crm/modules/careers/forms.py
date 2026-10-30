from __future__ import annotations

from django import forms
from django.utils.text import slugify

from crm.core.forms import CrmModelForm
from pages.models import JobPosting


class JobPostingForm(CrmModelForm):
    class Meta:
        model = JobPosting
        fields = ['title', 'department', 'location', 'employment_type', 'summary', 'description', 'sort_order',
                  'is_published']
        widgets = {'description': forms.Textarea(attrs={'rows': 12}), 'summary': forms.Textarea(attrs={'rows': 2})}
        labels = {'is_published': 'Published on the website careers page'}

    def clean_title(self):
        return self.cleaned_data['title'].strip()

    def save(self, commit=True):
        job = super().save(commit=False)
        if not job.slug:
            base = slugify(job.title)[:200] or 'role'
            slug, n = base, 2
            while JobPosting.objects.filter(slug=slug).exclude(pk=job.pk).exists():
                slug, n = f'{base}-{n}', n + 1
            job.slug = slug
        if commit:
            job.save()
        return job
