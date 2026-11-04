"""Hiring flow: apply on the website, automatic thank-you, shortlist, schedule / change / cancel interviews, reminders."""
from __future__ import annotations

from datetime import timedelta

from django.core import mail as django_mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .base import CrmTestCase

MEET = 'https://meet.google.com/abc-defg-hij'


def _pdf(name='cv.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 test', content_type='application/pdf')


@override_settings(ALLOWED_HOSTS=['testserver'])
class HiringFlowTests(CrmTestCase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()
        from pages.models import JobPosting
        self.job = JobPosting.objects.create(title='Django Developer', slug='django-developer', summary='Build things',
                                             description='Line one\nLine two', location='Remote / Trivandrum')

    # ---------------------------------------------------------------- helpers
    def apply(self, **extra):
        data = {'job': self.job.pk, 'full_name': 'anil kumar', 'email': 'Anil@X.test', 'phone': '98470 00000',
                'resume': _pdf()}
        data.update(extra)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('pages:career_apply'), data)

    def make_application(self, status='new'):
        from pages.models import JobApplication
        return JobApplication.objects.create(job=self.job, full_name='Anil Kumar', email='anil@x.test', phone='9847000000',
                                             resume='x.pdf', status=status)

    def interview_data(self, **extra):
        when = (timezone.localtime() + timedelta(days=3)).replace(hour=11, minute=0, second=0, microsecond=0)
        data = {'round_name': 'Technical round', 'scheduled_at': when.strftime('%Y-%m-%dT%H:%M'), 'duration_minutes': 45,
                'mode': 'google_meet', 'meeting_link': MEET, 'location': '', 'interviewers': [self.u['hr'].pk],
                'message': 'Looking forward to meeting you.', 'notify_candidate': 'on'}
        data.update(extra)
        return data

    def schedule(self, app, **extra):
        self.login('hr')
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('crm:career_interview_create', args=[app.pk]), self.interview_data(**extra))

    # ---------------------------------------------------------------- website: apply + thank-you
    def test_application_is_saved_and_candidate_gets_a_thank_you(self):
        from crm.models import DeliveryLog
        from pages.models import JobApplication
        r = self.apply()
        self.assertEqual(r.status_code, 200, r.content)
        app = JobApplication.objects.get()
        self.assertEqual((app.full_name, app.email, app.job_id), ('anil kumar', 'anil@x.test', self.job.pk))
        self.assertIsNotNone(app.acknowledged_at)
        thanks = [m for m in django_mail.outbox if m.to == ['anil@x.test']]
        self.assertEqual(len(thanks), 1)
        msg = thanks[0]
        self.assertIn('Thank you for applying to BThinkX: Django Developer', msg.subject)
        self.assertIn('Hi Anil,', msg.body)
        self.assertIn('What happens next', msg.body)
        self.assertIn(f'APP-{app.pk:05d}', msg.body)
        html = [c for c, mime in msg.alternatives if mime == 'text/html'][0]
        self.assertIn('We have received your application', html)
        self.assertTrue(DeliveryLog.objects.filter(object_type='jobapplication', object_id=str(app.pk),
                                                   template_code='career:received', status='sent').exists())

    def test_hr_is_alerted_in_the_crm(self):
        from crm.models import Notification
        self.apply()
        self.assertTrue(Notification.objects.filter(user=self.u['hr'], event_type='career_application').exists())

    def test_job_page_has_apply_form_for_that_role(self):
        from pages.models import JobApplication
        r = self.client.get(reverse('pages:career_detail', args=['django-developer']))
        self.assertContains(r, 'Django Developer')
        self.assertContains(r, 'name="job" value="%d"' % self.job.pk)
        self.assertContains(r, '"@type": "JobPosting"')
        self.assertContains(r, 'id="apply"')
        listing = self.client.get(reverse('pages:careers'))
        self.assertContains(listing, reverse('pages:career_detail', args=['django-developer']) + '#apply')
        self.assertEqual(self.apply().status_code, 200)
        self.assertEqual(JobApplication.objects.get().job_id, self.job.pk)

    def test_unpublished_role_has_no_page_and_is_not_in_the_sitemap(self):
        self.job.is_published = False
        self.job.save()
        self.assertEqual(self.client.get(reverse('pages:career_detail', args=['django-developer'])).status_code, 404)
        self.assertNotContains(self.client.get('/sitemap.xml'), 'careers/django-developer')

    def test_published_role_is_in_the_sitemap(self):
        self.assertContains(self.client.get('/sitemap.xml'), 'careers/django-developer')

    def test_bad_phone_and_honeypot(self):
        from pages.models import JobApplication
        self.assertEqual(self.apply(phone='12').status_code, 400)
        self.assertEqual(self.apply(company_website='spam').status_code, 200)
        self.assertFalse(JobApplication.objects.exists())
        self.assertEqual(django_mail.outbox, [])

    def test_failed_email_never_breaks_the_application(self):
        from unittest import mock

        from pages.models import JobApplication
        with mock.patch('crm.modules.careers.mail.notify.send_email', side_effect=RuntimeError('smtp down')):
            r = self.apply()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(JobApplication.objects.count(), 1)

    # ---------------------------------------------------------------- shortlist / reject
    def test_shortlist_with_email(self):
        app = self.make_application()
        self.login('hr')
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse('crm:career_application_action', args=[app.pk]),
                                 {'action': 'shortlist', 'send_email': '1', 'note': 'Great portfolio.'})
        self.assertEqual(r.status_code, 302)
        app.refresh_from_db()
        self.assertEqual(app.status, 'shortlisted')
        msg = django_mail.outbox[-1]
        self.assertIn('shortlisted', msg.subject)
        self.assertIn('Great portfolio.', msg.body)

    def test_shortlist_without_email_sends_nothing(self):
        app = self.make_application()
        self.login('hr')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:career_application_action', args=[app.pk]), {'action': 'shortlist'})
        app.refresh_from_db()
        self.assertEqual(app.status, 'shortlisted')
        self.assertEqual(django_mail.outbox, [])

    def test_reject_emails_and_cancels_scheduled_interviews(self):
        app = self.make_application('shortlisted')
        self.schedule(app)
        from pages.models import Interview
        self.assertEqual(Interview.objects.get().status, 'scheduled')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:career_application_action', args=[app.pk]),
                             {'action': 'reject', 'send_email': '1'})
        app.refresh_from_db()
        self.assertEqual(app.status, 'rejected')
        self.assertEqual(Interview.objects.get().status, 'cancelled')
        self.assertIn('An update on your application', django_mail.outbox[-1].subject)

    def test_invalid_action_is_refused(self):
        app = self.make_application()
        self.login('hr')
        self.client.post(reverse('crm:career_application_action', args=[app.pk]), {'action': 'delete-everything'})
        app.refresh_from_db()
        self.assertEqual(app.status, 'new')

    # ---------------------------------------------------------------- interviews
    def test_schedule_interview_emails_meet_link_and_calendar_file(self):
        from crm.models import Notification
        from pages.models import Interview
        app = self.make_application('shortlisted')
        r = self.schedule(app)
        self.assertEqual(r.status_code, 302, r.content.decode()[:600])
        iv = Interview.objects.get()
        app.refresh_from_db()
        self.assertEqual(app.status, 'interview')
        self.assertIsNotNone(iv.candidate_notified_at)
        msg = [m for m in django_mail.outbox if m.to == ['anil@x.test']][-1]
        self.assertIn('Interview invitation', msg.subject)
        self.assertIn(MEET, msg.body)
        self.assertIn('Technical round', msg.body)
        self.assertIn('Looking forward to meeting you.', msg.body)
        html = [c for c, mime in msg.alternatives if mime == 'text/html'][0]
        self.assertIn('Join on Google Meet', html)
        name, content, mime = msg.attachments[0]
        self.assertEqual(name, 'interview.ics')
        self.assertIn('METHOD:REQUEST', content if isinstance(content, str) else content.decode())

    def test_calendar_file_is_valid(self):
        from crm.modules.careers import mail
        from pages.models import Interview
        app = self.make_application('shortlisted')
        self.schedule(app)
        iv = Interview.objects.get()
        ics = mail.build_ics(iv).decode()
        for needle in ('BEGIN:VCALENDAR', 'BEGIN:VEVENT', f'UID:{iv.uid}@bthinkx', 'DTSTART:', 'DTEND:',
                       'ATTENDEE;CN=Anil Kumar', 'END:VEVENT', 'END:VCALENDAR'):
            self.assertIn(needle, ics)
        for line in ics.split('\r\n'):
            self.assertLessEqual(len(line.encode()), 75, line)
        self.assertIn('METHOD:CANCEL', mail.build_ics(iv, cancel=True).decode())

    def test_interviewer_is_notified(self):
        from crm.models import Notification
        app = self.make_application('shortlisted')
        self.login('hr')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:career_interview_create', args=[app.pk]),
                             self.interview_data(interviewers=[self.u['pm'].pk]))
        self.assertTrue(Notification.objects.filter(user=self.u['pm'], event_type='career_interview',
                                                    title__icontains='Anil').exists())

    def test_schedule_without_email_sends_nothing(self):
        app = self.make_application('shortlisted')
        data = self.interview_data()
        data.pop('notify_candidate')
        self.login('hr')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:career_interview_create', args=[app.pk]), data)
        self.assertEqual(django_mail.outbox, [])

    def test_validation(self):
        from pages.models import Interview
        app = self.make_application('shortlisted')
        self.login('hr')
        url = reverse('crm:career_interview_create', args=[app.pk])
        past = (timezone.localtime() - timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        for bad in ({'scheduled_at': past}, {'meeting_link': ''}, {'mode': 'phone', 'location': ''},
                    {'mode': 'in_person', 'location': ''}, {'duration_minutes': 5}):
            r = self.client.post(url, self.interview_data(**bad))
            self.assertEqual(r.status_code, 200, bad)
        self.assertFalse(Interview.objects.exists())
        ok = self.client.post(url, self.interview_data(mode='phone', location='+91 98470 00000', meeting_link=''))
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(Interview.objects.get().meeting_link, '')

    def test_reschedule_sends_updated_invite_and_bumps_the_sequence(self):
        from pages.models import Interview
        app = self.make_application('shortlisted')
        self.schedule(app)
        iv = Interview.objects.get()
        new_time = (timezone.localtime() + timedelta(days=5)).replace(hour=15, minute=30, second=0, microsecond=0)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse('crm:career_interview_edit', args=[iv.pk]),
                                 self.interview_data(scheduled_at=new_time.strftime('%Y-%m-%dT%H:%M')))
        self.assertEqual(r.status_code, 302)
        iv.refresh_from_db()
        self.assertEqual(iv.sequence, 1)
        self.assertTrue(django_mail.outbox[-1].subject.startswith('Updated: interview'))
        # saving again with nothing changed does not email again
        count = len(django_mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:career_interview_edit', args=[iv.pk]),
                             self.interview_data(scheduled_at=new_time.strftime('%Y-%m-%dT%H:%M')))
        self.assertEqual(len(django_mail.outbox), count)

    def test_cancel_emails_and_reverts_status(self):
        from pages.models import Interview
        app = self.make_application('shortlisted')
        self.schedule(app)
        iv = Interview.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse('crm:career_interview_outcome', args=[iv.pk]),
                                 {'action': 'cancel', 'note': 'The interviewer is unwell.', 'send_email': '1'})
        self.assertEqual(r.status_code, 302)
        iv.refresh_from_db()
        app.refresh_from_db()
        self.assertEqual((iv.status, app.status), ('cancelled', 'shortlisted'))
        last = django_mail.outbox[-1]
        self.assertIn('cancelled', last.subject)
        self.assertIn('The interviewer is unwell.', last.body)

    def test_complete_with_feedback_and_no_show(self):
        from pages.models import Interview
        app = self.make_application('shortlisted')
        self.schedule(app)
        iv = Interview.objects.get()
        self.client.post(reverse('crm:career_interview_outcome', args=[iv.pk]),
                         {'action': 'complete', 'note': 'Strong on Django.'})
        iv.refresh_from_db()
        self.assertEqual((iv.status, iv.feedback), ('completed', 'Strong on Django.'))
        # a closed interview cannot be changed again
        self.client.post(reverse('crm:career_interview_outcome', args=[iv.pk]), {'action': 'cancel'})
        iv.refresh_from_db()
        self.assertEqual(iv.status, 'completed')

    def test_cannot_schedule_for_a_rejected_candidate(self):
        from pages.models import Interview
        app = self.make_application('rejected')
        self.login('hr')
        r = self.client.post(reverse('crm:career_interview_create', args=[app.pk]), self.interview_data())
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Interview.objects.exists())

    def test_crm_pages_render(self):
        from pages.models import Interview
        app = self.make_application('shortlisted')
        self.schedule(app)
        iv = Interview.objects.get()
        for url in (reverse('crm:career_application_detail', args=[app.pk]), reverse('crm:career_applications'),
                    reverse('crm:career_interviews'), reverse('crm:career_interview_create', args=[app.pk]),
                    reverse('crm:career_interview_edit', args=[iv.pk]), reverse('crm:career_list')):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        page = self.client.get(reverse('crm:career_application_detail', args=[app.pk]))
        self.assertContains(page, 'Technical round')
        self.assertContains(page, MEET)
        self.assertContains(page, 'Emails to the candidate')
        self.assertContains(self.client.get(reverse('crm:career_interviews')), 'Anil Kumar')

    def test_sales_user_cannot_use_hiring_actions(self):
        from pages.models import Interview
        app = self.make_application('shortlisted')
        self.login('sales')
        for url, data in ((reverse('crm:career_interview_create', args=[app.pk]), self.interview_data()),
                          (reverse('crm:career_application_action', args=[app.pk]), {'action': 'reject'})):
            self.assertIn(self.client.post(url, data).status_code, (302, 403, 404))
        app.refresh_from_db()
        self.assertEqual(app.status, 'shortlisted')
        self.assertFalse(Interview.objects.exists())
        self.assertIn(self.client.get(reverse('crm:career_interviews')).status_code, (302, 403, 404))

    # ---------------------------------------------------------------- reminders
    def test_reminder_job_sends_once(self):
        from crm.modules.careers import jobs
        from pages.models import Interview
        app = self.make_application('shortlisted')
        self.schedule(app)
        iv = Interview.objects.get()
        Interview.objects.filter(pk=iv.pk).update(scheduled_at=timezone.now() + timedelta(hours=10),
                                                  candidate_notified_at=timezone.now() - timedelta(days=1))
        before = len(django_mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(jobs.interview_reminders(), 1)
        self.assertEqual(len(django_mail.outbox), before + 1)
        self.assertIn('Reminder', django_mail.outbox[-1].subject)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(jobs.interview_reminders(), 0)
        self.assertEqual(len(django_mail.outbox), before + 1)
