"""CRM Hiring inbox (job applications + notifications) and Our Works (website portfolio)."""
from __future__ import annotations

import base64

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from .base import CrmTestCase

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')


def _png(name='cover.png'):
    return SimpleUploadedFile(name, PNG, content_type='image/png')


@override_settings(ALLOWED_HOSTS=['testserver'])
class HiringInboxTests(CrmTestCase):
    def _apply(self):
        from pages.models import JobApplication, JobPosting
        job = JobPosting.objects.create(title='QA Engineer', slug='qa-engineer', summary='s', description='d')
        r = self.client.post(reverse('pages:career_apply'), {
            'job': job.pk, 'full_name': 'Anil K', 'email': 'anil@x.test', 'phone': '9847000000',
            'resume': SimpleUploadedFile('cv.pdf', b'%PDF-1.4 test', content_type='application/pdf')})
        self.assertEqual(r.status_code, 200, r.content)
        return JobApplication.objects.get()

    def test_submission_notifies_hr_with_link_to_the_application(self):
        from crm.models import Notification
        app = self._apply()
        note = Notification.objects.get(user=self.u['hr'], event_type='career_application')
        self.assertIn('Anil K', note.title)
        self.assertIn('QA Engineer', note.body)
        self.assertEqual(note.url, reverse('crm:career_application_detail', args=[app.pk]))
        self.assertFalse(Notification.objects.filter(user=self.u['sales'], event_type='career_application').exists())

    def test_detail_page_notes_status_and_sidebar_badge(self):
        app = self._apply()
        self.login('hr')
        detail = reverse('crm:career_application_detail', args=[app.pk])
        r = self.client.get(detail)
        self.assertContains(r, 'Anil K')
        self.assertContains(r, 'aria-label="1 new"')
        r = self.client.post(reverse('crm:career_application_update', args=[app.pk]),
                             {'internal_notes': 'Strong profile'}, HTTP_REFERER='http://testserver' + detail)
        self.assertRedirects(r, detail)
        app.refresh_from_db()
        self.assertEqual((app.internal_notes, app.status), ('Strong profile', 'new'))
        self.client.post(reverse('crm:career_application_update', args=[app.pk]), {'status': 'interview'})
        app.refresh_from_db()
        self.assertEqual((app.internal_notes, app.status), ('Strong profile', 'interview'))
        self.assertNotContains(self.client.get(reverse('crm:career_applications')), 'aria-label="1 new"')

    def test_update_ignores_foreign_referer(self):
        app = self._apply()
        self.login('hr')
        r = self.client.post(reverse('crm:career_application_update', args=[app.pk]), {'status': 'reviewing'},
                             HTTP_REFERER='https://evil.example/x')
        self.assertRedirects(r, reverse('crm:career_applications'))

    def test_pdf_resume_opens_inline_and_others_download(self):
        app = self._apply()
        self.login('hr')
        url = reverse('crm:career_application_resume', args=[app.pk])
        self.assertIn('inline', self.client.get(url + '?inline=1')['Content-Disposition'])
        self.assertIn('attachment', self.client.get(url)['Content-Disposition'])

    def test_non_hr_cannot_open_application(self):
        app = self._apply()
        self.login('sales')
        self.assertIn(self.client.get(reverse('crm:career_application_detail', args=[app.pk])).status_code,
                      (302, 403, 404))


@override_settings(ALLOWED_HOSTS=['testserver'])
class OurWorksTests(CrmTestCase):
    def _payload(self, **extra):
        data = {'title': 'Kerala Saree Store', 'category': 'ecommerce', 'year': '2025',
                'short_description': 'Online store with UPI checkout.', 'sort_order': 0,
                'is_published': 'on', 'show_on_homepage': 'on', 'new_tags': 'Django, Razorpay, django',
                'image': _png()}
        data.update(extra)
        return data

    def test_owner_adds_work_and_it_goes_live(self):
        from pages.models import Project
        self.login('owner')
        for name in ('crm:work_list', 'crm:work_create'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        r = self.client.post(reverse('crm:work_create'), self._payload())
        self.assertRedirects(r, reverse('crm:work_list'))
        work = Project.objects.get()
        self.assertEqual(work.slug, 'kerala-saree-store')
        self.assertEqual(sorted(work.stack.values_list('name', flat=True)), ['Django', 'Razorpay'])
        self.assertContains(self.client.get(reverse('crm:work_list')), 'Kerala Saree Store')
        self.assertEqual(self.client.get(reverse('crm:work_edit', args=[work.pk])).status_code, 200)
        self.client.logout()
        self.assertContains(self.client.get(reverse('pages:portfolio')), 'Kerala Saree Store')
        self.assertContains(self.client.get(reverse('pages:index')), 'Kerala Saree Store')

    def test_hide_removes_it_from_the_website(self):
        from pages.models import Project
        self.login('owner')
        self.client.post(reverse('crm:work_create'), self._payload())
        work = Project.objects.get()
        self.client.post(reverse('crm:work_toggle', args=[work.pk]))
        self.client.logout()
        self.assertNotContains(self.client.get(reverse('pages:portfolio')), 'Kerala Saree Store')
        self.assertNotContains(self.client.get(reverse('pages:index')), 'Kerala Saree Store')

    def test_image_required_on_create_but_kept_on_edit(self):
        from pages.models import Project
        self.login('owner')
        r = self.client.post(reverse('crm:work_create'), self._payload(image=''))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Upload a cover image')
        self.client.post(reverse('crm:work_create'), self._payload())
        work = Project.objects.get()
        old = work.image.name
        data = self._payload()
        data.pop('image')
        data['title'] = 'Renamed'
        self.assertEqual(self.client.post(reverse('crm:work_edit', args=[work.pk]), data).status_code, 302)
        work.refresh_from_db()
        self.assertEqual((work.title, work.image.name), ('Renamed', old))

    def test_rejects_non_image_upload(self):
        from pages.models import Project
        self.login('owner')
        bad = SimpleUploadedFile('x.png', b'MZ not an image', content_type='image/png')
        r = self.client.post(reverse('crm:work_create'), self._payload(image=bad))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Project.objects.exists())

    def test_only_one_featured_work(self):
        from pages.models import Project
        self.login('owner')
        self.client.post(reverse('crm:work_create'), self._payload(is_featured='on'))
        self.client.post(reverse('crm:work_create'),
                         self._payload(title='Second', is_featured='on', image=_png('b.png')))
        self.assertEqual(Project.objects.filter(is_featured=True).count(), 1)
        self.assertTrue(Project.objects.get(title='Second').is_featured)

    def test_delete_and_permissions(self):
        from pages.models import Project
        self.login('owner')
        self.client.post(reverse('crm:work_create'), self._payload())
        work = Project.objects.get()
        self.client.logout()
        self.login('sales')
        self.assertIn(self.client.get(reverse('crm:work_list')).status_code, (302, 403, 404))
        self.assertIn(self.client.post(reverse('crm:work_delete', args=[work.pk])).status_code, (302, 403, 404))
        self.assertTrue(Project.objects.filter(pk=work.pk).exists())
        self.client.logout()
        self.login('owner')
        self.client.post(reverse('crm:work_delete', args=[work.pk]))
        self.assertFalse(Project.objects.filter(pk=work.pk).exists())
