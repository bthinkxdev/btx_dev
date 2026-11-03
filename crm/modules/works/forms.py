from __future__ import annotations

import re

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.text import slugify

from crm.core.forms import CrmModelForm, validate_upload
from pages.models import Project, TechTag

IMAGE_TYPES = ('.png', '.jpg', '.jpeg', '.webp')
MAX_IMAGE_MB = 5


class WorkForm(CrmModelForm):
    stack = forms.ModelMultipleChoiceField(
        queryset=TechTag.objects.order_by('name'), required=False, label='Technologies used',
        help_text='Hold Ctrl (or Cmd) to pick more than one. Missing one? Add it below.')
    new_tags = forms.CharField(
        required=False, max_length=200, label='Add new technologies',
        help_text='Comma separated, for example: Razorpay, Tailwind, Redis.')
    image = forms.ImageField(
        required=False, label='Cover image', widget=forms.FileInput(attrs={'accept': 'image/png,image/jpeg,image/webp'}),
        help_text=f'JPG, PNG or WebP, up to {MAX_IMAGE_MB} MB. A landscape screenshot (16:10) looks best.')

    class Meta:
        model = Project
        fields = ['title', 'category', 'year', 'short_description', 'image', 'stack', 'is_featured',
                  'show_on_homepage', 'is_published', 'sort_order']
        widgets = {'short_description': forms.Textarea(attrs={'rows': 3}),
                   'stack': forms.SelectMultiple(attrs={'size': 8})}
        labels = {
            'short_description': 'Short description',
            'is_published': 'Show on the website',
            'is_featured': 'Feature on the Our Work page',
            'show_on_homepage': 'Show in the homepage strip',
            'sort_order': 'Display order',
        }
        help_texts = {
            'short_description': 'One or two sentences shown on the work card.',
            'is_featured': 'Shown as the large card at the top. Only one work can be featured, so this replaces the current one.',
            'show_on_homepage': 'The first four homepage works appear on the landing page.',
            'sort_order': 'Lower numbers are listed first.',
            'year': 'Year the work went live.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['short_description'].widget.attrs['maxlength'] = 400
        self.fields['year'].widget.attrs.update({'min': 2000, 'max': timezone.localdate().year + 1})
        self.fields['year'].initial = self.fields['year'].initial or timezone.localdate().year
        if self.instance.pk:
            self.fields['image'].help_text = 'Leave empty to keep the current image.'

    def clean_title(self):
        title = self.cleaned_data['title'].strip()
        if not title:
            raise ValidationError('Enter the work title.')
        return title

    def clean_short_description(self):
        text = self.cleaned_data['short_description'].strip()
        if not text:
            raise ValidationError('Add a short description.')
        if len(text) > 400:
            raise ValidationError('Keep it under 400 characters.')
        return text

    def clean_year(self):
        year = self.cleaned_data.get('year')
        if year is not None and not 2000 <= year <= timezone.localdate().year + 1:
            raise ValidationError('Enter a valid year.')
        return year

    def clean_image(self):
        image = self.files.get(self.add_prefix('image'))
        if not image:
            if not self.instance.image:
                raise ValidationError('Upload a cover image. Works without one are not shown on the website.')
            return self.instance.image  # unchanged
        image = self.cleaned_data.get('image') or image
        if image.size > MAX_IMAGE_MB * 1024 * 1024:
            raise ValidationError(f'This image is larger than {MAX_IMAGE_MB} MB.')
        validate_upload(image, IMAGE_TYPES)
        return image

    def clean_new_tags(self):
        names, seen = [], set()
        for raw in re.split(r'[,\n]', self.cleaned_data.get('new_tags', '')):
            name = ' '.join(raw.split())
            if not name:
                continue
            if len(name) > 50:
                raise ValidationError(f'"{name[:20]}…" is too long (50 characters max).')
            if name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
        return names

    def save(self, commit=True):
        work = super().save(commit=False)
        if not work.slug:
            base = slugify(work.title)[:180] or 'work'
            slug, n = base, 2
            while Project.objects.filter(slug=slug).exclude(pk=work.pk).exists():
                slug, n = f'{base}-{n}', n + 1
            work.slug = slug
        if commit:
            work.save()
            if work.is_featured:
                Project.objects.exclude(pk=work.pk).filter(is_featured=True).update(is_featured=False)
            tags = list(self.cleaned_data.get('stack') or [])
            for name in self.cleaned_data.get('new_tags') or []:
                tag = TechTag.objects.filter(name__iexact=name).first() or TechTag.objects.create(name=name)
                if tag not in tags:
                    tags.append(tag)
            work.stack.set(tags)
        return work
