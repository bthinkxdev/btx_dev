from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify

from crm.core.forms import CrmModelForm
from pages.models import Interview, JobPosting


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


# ---------------------------------------------------------------- interviews and candidate actions

DEFAULT_INVITE_NOTE = ('We enjoyed going through your application and would like to know more about your experience '
                       'and the work you have done. The conversation will be relaxed and practical.')


class InterviewForm(CrmModelForm):
    notify_candidate = forms.BooleanField(
        required=False, initial=True, label='Email the candidate the interview details now',
        help_text='Sends the date, time, joining link and a calendar file. Untick to save without emailing.')
    interviewers = forms.ModelMultipleChoiceField(
        queryset=None, required=False, label='Interviewers',
        help_text='They are notified in the CRM and get a reminder. Hold Ctrl (or Cmd) to pick more than one.')

    class Meta:
        model = Interview
        fields = ['round_name', 'scheduled_at', 'duration_minutes', 'mode', 'meeting_link', 'location', 'interviewers',
                  'message']
        widgets = {'message': forms.Textarea(attrs={'rows': 4}),
                   'interviewers': forms.SelectMultiple(attrs={'size': 5})}
        labels = {'round_name': 'Interview round', 'scheduled_at': 'Date and time (IST)',
                  'duration_minutes': 'Duration (minutes)', 'mode': 'Format', 'meeting_link': 'Meeting link',
                  'location': 'Phone number or address', 'message': 'Note to the candidate'}
        help_texts = {
            'round_name': 'For example: First interview, Technical round, HR round.',
            'meeting_link': 'Paste the Google Meet, Zoom or Teams link. Use "Create a Google Meet link" to get one.',
            'location': 'For a phone call: the number we will call. In person: the office address.',
            'message': 'Shown in the email as a personal note. Keep it short and friendly.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        User = get_user_model()
        self.fields['interviewers'].queryset = User.objects.filter(is_active=True).order_by('first_name', 'username')
        self.fields['interviewers'].label_from_instance = lambda u: u.get_full_name() or u.get_username()
        self.fields['duration_minutes'].widget.attrs.update({'min': 10, 'max': 480, 'step': 5})
        if not self.instance.pk and not self.is_bound:
            tomorrow = (timezone.localtime() + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
            self.initial.setdefault('scheduled_at', tomorrow)
            self.initial.setdefault('message', DEFAULT_INVITE_NOTE)
        if self.instance.pk:
            self.fields['notify_candidate'].label = 'Email the candidate the updated details'

    def clean_duration_minutes(self):
        n = self.cleaned_data['duration_minutes']
        if not 10 <= n <= 480:
            raise forms.ValidationError('Choose a duration between 10 minutes and 8 hours.')
        return n

    def clean(self):
        data = super().clean()
        when = data.get('scheduled_at')
        if when and 'scheduled_at' in self.changed_data and when < timezone.now() - timedelta(minutes=5):
            self.add_error('scheduled_at', 'Pick a time in the future.')
        mode = data.get('mode')
        link = (data.get('meeting_link') or '').strip()
        place = (data.get('location') or '').strip()
        if mode in Interview.ONLINE_MODES and not link:
            self.add_error('meeting_link', 'Add the meeting link so the candidate can join.')
        if mode == Interview.MODE_PHONE and not place:
            self.add_error('location', 'Add the phone number we will call.')
        if mode == Interview.MODE_IN_PERSON and not place:
            self.add_error('location', 'Add the address where the candidate should come.')
        if mode not in Interview.ONLINE_MODES:
            data['meeting_link'] = ''
        else:
            data['location'] = ''
        return data


class ApplicationActionForm(forms.Form):
    ACTIONS = ('review', 'shortlist', 'reject', 'hire')
    action = forms.ChoiceField(choices=[(a, a) for a in ACTIONS])
    send_email = forms.BooleanField(required=False)
    note = forms.CharField(required=False, max_length=1000)

    def clean_note(self):
        return self.cleaned_data['note'].strip()


class InterviewOutcomeForm(forms.Form):
    ACTIONS = ('complete', 'no_show', 'cancel')
    action = forms.ChoiceField(choices=[(a, a) for a in ACTIONS])
    note = forms.CharField(required=False, max_length=2000)
    send_email = forms.BooleanField(required=False)

    def clean_note(self):
        return self.cleaned_data['note'].strip()
