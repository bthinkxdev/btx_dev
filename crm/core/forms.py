"""Form infrastructure (Part 2.5): template styling, normalisation, optimistic locking, safe uploads."""
from __future__ import annotations

import mimetypes
import os
from datetime import datetime

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from . import india

INPUT = 'form-input'
SELECT = 'form-select'
TEXTAREA = 'form-textarea'
CHECKBOX = 'form-checkbox-box'
MULTI = 'form-multiselect'


class DateInput(forms.DateInput):
    input_type = 'date'

    def __init__(self, attrs=None):
        super().__init__(attrs, format='%Y-%m-%d')


class DateTimeInput(forms.DateTimeInput):
    input_type = 'datetime-local'

    def __init__(self, attrs=None):
        super().__init__(attrs, format='%Y-%m-%dT%H:%M')


class StyledFormMixin:
    """Applies the template's field classes and accessibility attributes to every widget."""

    def _style(self):
        for name, f in self.fields.items():
            w = f.widget
            css = w.attrs.get('class', '')
            if isinstance(w, (forms.CheckboxInput,)):
                base = CHECKBOX
            elif isinstance(w, (forms.CheckboxSelectMultiple, forms.RadioSelect)):
                base = ''
            elif isinstance(w, forms.SelectMultiple):
                base = MULTI
            elif isinstance(w, forms.Select):
                base = SELECT
            elif isinstance(w, forms.Textarea):
                base = TEXTAREA
                w.attrs.setdefault('rows', 3)
            elif isinstance(w, forms.ClearableFileInput) or isinstance(w, forms.FileInput):
                base = 'block w-full text-sm text-gray-700 file:mr-4 file:rounded-lg file:border-0 file:bg-brand-50 file:px-4 file:py-2.5 file:text-sm file:font-medium file:text-brand-500 hover:file:bg-brand-100 dark:text-gray-400 dark:file:bg-brand-500/15'
            else:
                base = INPUT
            if isinstance(w, forms.DateTimeInput) and not isinstance(w, DateTimeInput):
                f.widget = w = DateTimeInput(attrs=w.attrs)
            elif isinstance(w, forms.DateInput) and not isinstance(w, (DateInput, forms.DateTimeInput)):
                f.widget = w = DateInput(attrs=w.attrs)
            if base and base not in css:
                w.attrs['class'] = (base + ' ' + css).strip()
            if f.required and not isinstance(w, forms.CheckboxInput):
                w.attrs['aria-required'] = 'true'
            if self.is_bound and name in self.errors:
                w.attrs['aria-invalid'] = 'true'
                w.attrs['aria-describedby'] = f'{self.add_prefix(name)}-error'
                if 'form-invalid' not in w.attrs.get('class', ''):
                    w.attrs['class'] += ' form-invalid'
            if isinstance(f, forms.DecimalField) and getattr(f, 'decimal_places', None) == 2:
                w.attrs.setdefault('step', '0.01')
                w.attrs.setdefault('inputmode', 'decimal')

    def full_clean(self):
        super().full_clean()
        self._style()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class CrmForm(StyledFormMixin, forms.Form):
    pass


class CrmModelForm(StyledFormMixin, forms.ModelForm):
    pass


class VersionedModelFormMixin(forms.Form):
    """Optimistic locking (Part 2.11): the second save of a stale form is rejected."""
    row_version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        inst = getattr(self, 'instance', None)
        if inst is not None and inst.pk and not self.is_bound:
            self.initial['row_version'] = inst.row_version

    def clean(self):
        cleaned = super().clean()
        inst = getattr(self, 'instance', None)
        if inst is not None and inst.pk:
            submitted = cleaned.get('row_version')
            current = type(inst).objects.filter(pk=inst.pk).values_list('row_version', 'updated_by', 'updated_at').first()
            if current and submitted is not None and int(submitted) != current[0]:
                from django.contrib.auth import get_user_model
                who = get_user_model().objects.filter(pk=current[1]).first() if current[1] else None
                when = timezone.localtime(current[2]).strftime('%H:%M') if current[2] else ''
                name = (who.get_full_name() or who.get_username()) if who else 'someone else'
                raise ValidationError(f'This record was changed by {name} at {when}. Reload to see the latest.',
                                      code='stale')
        return cleaned

    def save(self, commit=True):
        inst = super().save(commit=False)
        if inst.pk:
            inst.row_version = (inst.row_version or 0) + 1
        if commit:
            inst.save()
            self.save_m2m()
        return inst


# ---------------------------------------------------------------- fields

class PhoneField(forms.CharField):
    """Normalises to E.164 on clean (Part 1.5)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_length', 20)
        super().__init__(*args, **kwargs)
        self.widget.attrs.update({'type': 'tel', 'inputmode': 'tel', 'autocomplete': 'tel', 'placeholder': '98765 43210'})

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return ''
        try:
            return india.normalize_phone(value)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc


class GSTINField(forms.CharField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_length', 15)
        kwargs.setdefault('required', False)
        super().__init__(*args, **kwargs)
        self.widget.attrs.update({'placeholder': '32AAAAA0000A1Z5', 'style': 'text-transform:uppercase'})

    def clean(self, value):
        value = (super().clean(value) or '').strip().upper()
        err = india.validate_gstin(value)
        if err:
            raise ValidationError(err)
        return value


class PANField(forms.CharField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_length', 10)
        kwargs.setdefault('required', False)
        super().__init__(*args, **kwargs)
        self.widget.attrs.update({'placeholder': 'ABCDE1234F', 'style': 'text-transform:uppercase'})

    def clean(self, value):
        value = (super().clean(value) or '').strip().upper()
        err = india.validate_pan(value)
        if err:
            raise ValidationError(err)
        return value


class MoneyFormField(forms.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_digits', 15)
        kwargs.setdefault('decimal_places', 2)
        kwargs.setdefault('min_value', 0)
        super().__init__(*args, **kwargs)
        self.widget.attrs.update({'step': '0.01', 'inputmode': 'decimal', 'placeholder': '0.00'})


def future_datetime(value):
    from datetime import timedelta
    if value is not None and value < timezone.now() - timedelta(minutes=1):
        raise ValidationError('Choose a time that is not in the past.')


def future_date(value):
    if value is not None and value < timezone.localdate():
        raise ValidationError('Choose a date that is not in the past.')


# ---------------------------------------------------------------- uploads (Part 15.1)

ALLOWED_UPLOADS = {
    '.pdf': ['application/pdf'],
    '.png': ['image/png'],
    '.jpg': ['image/jpeg'],
    '.jpeg': ['image/jpeg'],
    '.webp': ['image/webp'],
    '.csv': ['text/csv', 'text/plain', 'application/vnd.ms-excel'],
    '.xlsx': ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/zip'],
    '.docx': ['application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/zip'],
    '.txt': ['text/plain'],
}
MAGIC = {
    '.pdf': [b'%PDF'],
    '.png': [b'\x89PNG'],
    '.jpg': [b'\xff\xd8\xff'],
    '.jpeg': [b'\xff\xd8\xff'],
    '.webp': [b'RIFF'],
    '.xlsx': [b'PK\x03\x04'],
    '.docx': [b'PK\x03\x04'],
}
EXECUTABLE_SIGNATURES = [b'MZ', b'\x7fELF', b'#!', b'\xca\xfe\xba\xbe', b'\xcf\xfa\xed\xfe']


def validate_upload(f, allowed: tuple[str, ...] | None = None):
    """Allow-list of extensions, content sniffing, size limit; rejects executables renamed to PDF."""
    if f is None:
        return
    limit = getattr(settings, 'CRM_MAX_UPLOAD_MB', 10) * 1024 * 1024
    if f.size > limit:
        raise ValidationError(f'This file is larger than {limit // (1024 * 1024)} MB.')
    if f.size == 0:
        raise ValidationError('This file is empty.')
    ext = os.path.splitext(f.name)[1].lower()
    allowed = allowed or tuple(ALLOWED_UPLOADS)
    if ext not in allowed or ext not in ALLOWED_UPLOADS:
        raise ValidationError('This file type is not allowed. Use ' + ', '.join(a.lstrip('.').upper() for a in allowed) + '.')
    head = f.read(16)
    f.seek(0)
    if any(head.startswith(sig) for sig in EXECUTABLE_SIGNATURES):
        raise ValidationError('This file looks like a program, not a document. It was rejected.')
    signatures = MAGIC.get(ext)
    if signatures and not any(head.startswith(s) for s in signatures):
        raise ValidationError('The file contents do not match its extension.')
    if ext in ('.csv', '.txt'):
        try:
            f.read(4096).decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValidationError('This text file is not UTF-8 encoded.') from exc
        finally:
            f.seek(0)


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or 'application/octet-stream'


class SafeFileField(forms.FileField):
    def __init__(self, *args, allowed: tuple[str, ...] | None = None, **kwargs):
        self.allowed = allowed
        super().__init__(*args, **kwargs)
        if allowed:
            self.widget.attrs['accept'] = ','.join(allowed)

    def clean(self, data, initial=None):
        f = super().clean(data, initial)
        if f and hasattr(f, 'size'):
            validate_upload(f, self.allowed)
        return f


class ReasonForm(CrmForm):
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), min_length=10, max_length=500,
                             help_text='At least 10 characters.')
