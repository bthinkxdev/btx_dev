"""Website -> CRM lead capture + notifications, and the CRM-managed Careers page."""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from .base import CrmTestCase


@override_settings(ALLOWED_HOSTS=['testserver'])
class WebsiteLeadTests(CrmTestCase):
    def test_contact_form_creates_lead_and_notifies(self):
        from crm.models import Lead, Notification
        r = self.client.post(reverse('pages:contact_submit'), {
            'name': 'Meera Joseph', 'email': 'meera@shop.test', 'company': 'Meera Sarees', 'project_type': 'E-commerce',
            'message': 'Need an online store.', 'budget': '', 'timeline': ''})
        self.assertEqual(r.status_code, 200, r.content)
        lead = Lead.objects.get(email='meera@shop.test')
        self.assertEqual(lead.source.channel, 'website')
        self.assertIn('Need an online store', lead.notes)
        # the owner (or, with no owner, the triage users) were told
        self.assertTrue(Notification.objects.filter(event_type__in=['website_lead', 'lead_assigned', 'info'],
                                                    title__icontains='Meera').exists())

    def test_quote_request_creates_lead(self):
        from crm.models import Lead
        r = self.client.post(reverse('pages:quote_request'), {
            'name': 'Ravi', 'phone': '9847012345', 'business_name': 'Ravi Mart', 'business_type': 'grocery-fmcg',
            'package': 'online-store'})
        self.assertEqual(r.status_code, 200, r.content)
        lead = Lead.objects.get(name='Ravi')
        self.assertEqual(lead.company, 'Ravi Mart')

    def test_repeat_submission_attaches_instead_of_duplicating(self):
        from crm.models import Lead
        data = {'name': 'Meera Joseph', 'email': 'meera@shop.test', 'project_type': 'E-commerce', 'message': 'Hi'}
        self.client.post(reverse('pages:contact_submit'), data)
        self.client.post(reverse('pages:contact_submit'), data)
        self.assertEqual(Lead.objects.filter(email='meera@shop.test').count(), 1)

    def test_website_failure_never_breaks_form(self):
        from unittest import mock
        with mock.patch('crm.modules.leads.website.capture', side_effect=RuntimeError('boom')):
            r = self.client.post(reverse('pages:contact_submit'), {
                'name': 'X Y', 'email': 'x@y.test', 'project_type': 'Web', 'message': 'm'})
        self.assertEqual(r.status_code, 200)


@override_settings(ALLOWED_HOSTS=['testserver'])
class CareersTests(CrmTestCase):
    def test_hr_posts_job_and_it_appears_on_website(self):
        from pages.models import JobPosting
        self.login('hr')
        r = self.client.post(reverse('crm:career_create'), {
            'title': 'Django Developer', 'department': 'Engineering', 'location': 'Remote', 'employment_type': 'full_time',
            'summary': 'Build things', 'description': 'Details', 'sort_order': 0, 'is_published': 'on'})
        self.assertEqual(r.status_code, 302, r.content.decode()[:500])
        job = JobPosting.objects.get(title='Django Developer')
        self.assertEqual(job.slug, 'django-developer')
        self.client.logout()
        self.assertContains(self.client.get(reverse('pages:careers')), 'Django Developer')
        self.login('hr')
        self.client.post(reverse('crm:career_toggle', args=[job.pk]))
        self.client.logout()
        self.assertNotContains(self.client.get(reverse('pages:careers')), 'Django Developer')

    def test_application_flow_and_pages_render(self):
        from pages.models import JobApplication, JobPosting
        job = JobPosting.objects.create(title='QA', slug='qa', summary='s', description='d', is_published=True)
        r = self.client.post(reverse('pages:career_apply'), {
            'job': job.pk, 'full_name': 'Anil K', 'email': 'anil@x.test', 'phone': '9847000000',
            'resume': SimpleUploadedFile('cv.pdf', b'%PDF-1.4 test', content_type='application/pdf')})
        self.assertEqual(r.status_code, 200, r.content)
        app = JobApplication.objects.get()
        self.login('hr')
        for name in ('crm:career_list', 'crm:career_applications', 'crm:career_create'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertContains(self.client.get(reverse('crm:career_applications')), 'Anil K')
        self.client.post(reverse('crm:career_application_update', args=[app.pk]), {'status': 'shortlisted'})
        app.refresh_from_db()
        self.assertEqual(app.status, 'shortlisted')
        self.assertEqual(self.client.get(reverse('crm:career_application_resume', args=[app.pk])).status_code, 200)

    def test_sales_user_cannot_open_careers(self):
        self.login('sales')
        self.assertIn(self.client.get(reverse('crm:career_list')).status_code, (302, 403, 404))
