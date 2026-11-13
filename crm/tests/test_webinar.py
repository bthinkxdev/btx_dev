"""Webinar landing page, Cashfree payment flow (always mocked), confirmation emails, CRM leads and the CRM screens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import timedelta
from unittest.mock import patch

from django.core import mail as django_mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .base import CrmTestCase

SECRET = 'test-secret'
CF = dict(CASHFREE_APP_ID='test-app', CASHFREE_SECRET_KEY=SECRET, CASHFREE_ENV='sandbox')


def signed(payload: dict):
    body = json.dumps(payload).encode()
    ts = '1700000000'
    sig = base64.b64encode(hmac.new(SECRET.encode(), (ts + body.decode()).encode(), hashlib.sha256).digest()).decode()
    return body, {'HTTP_X_WEBHOOK_TIMESTAMP': ts, 'HTTP_X_WEBHOOK_SIGNATURE': sig}


@override_settings(ALLOWED_HOSTS=['testserver'], **CF)
class WebinarTests(CrmTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        from pages.models import Webinar
        Webinar.objects.all().delete()
        self.w = Webinar.objects.create(starts_at=timezone.now() + timedelta(days=2), join_url='https://meet.google.com/aaa-bbbb-ccc')
        self.url = reverse('pages:webinar_register', args=[self.w.slug])

    # ---------------------------------------------------------------- helpers
    def data(self, **extra):
        d = {'name': 'meera joseph', 'email': 'Meera@Shop.test', 'phone': '+91 98470 12345', 'business_name': 'Meera Sarees'}
        d.update(extra)
        return d

    def register(self, **extra):
        with patch('pages.webinar_views.cashfree.create_order', return_value={'payment_session_id': 'sess_123'}) as m:
            r = self.client.post(self.url, self.data(**extra))
        self.last_create = m
        return r

    def reg(self):
        from pages.models import WebinarRegistration
        return WebinarRegistration.objects.get()

    def pay(self, reg=None, **extra):
        reg = reg or self.reg()
        body, headers = signed({'data': {'order': {'order_id': reg.cashfree_order_id},
                                         'payment': {'payment_status': 'SUCCESS', 'cf_payment_id': 998877}}})
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('pages:webinar_webhook'), body, content_type='application/json', **headers)

    # ---------------------------------------------------------------- landing page
    def test_landing_page(self):
        r = self.client.get(reverse('pages:webinar'))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for needle in ('How to Sell Online 10x Better Than Your Competitors', '&#8377;199', 'name="phone"', 'name="email"',
                       'name="business_name"', 'Pay &#8377;199 and reserve my seat', '"@type": "Event"', 'id="register"',
                       'Live on Google Meet'):
            self.assertIn(needle, html, needle)
        self.assertEqual(re.findall('[\U0001F000-\U0001FAFF☀-➿⭐✨]', html), [])
        self.assertEqual(self.client.get(reverse('pages:webinar', args=[self.w.slug])).status_code, 200)

    def test_offline_started_and_sold_out_states(self):
        self.w.is_active = False
        self.w.save()
        self.assertEqual(self.client.get(reverse('pages:webinar')).status_code, 404)
        self.w.is_active = True
        self.w.starts_at = timezone.now() - timedelta(hours=1)
        self.w.save()
        html = self.client.get(reverse('pages:webinar')).content.decode()
        self.assertIn('Registration has closed', html)
        self.assertNotIn('id="wbForm"', html)
        self.assertEqual(self.register().status_code, 400)
        self.w.starts_at = timezone.now() + timedelta(days=1)
        self.w.seat_limit = 1
        self.w.save()
        self.register()
        self.pay()
        html = self.client.get(reverse('pages:webinar')).content.decode()
        self.assertIn('All seats are taken', html)
        self.assertEqual(self.register(email='other@x.test', phone='9847000111').status_code, 400)

    def test_ad_tracking_is_kept(self):
        url = reverse('pages:webinar') + '?utm_source=facebook&utm_campaign=sunday&fbclid=abc123'
        html = self.client.get(url).content.decode()
        self.assertIn('name="utm_campaign" value="sunday"', html)
        self.register(utm_source='facebook', utm_campaign='sunday', fbclid='abc123')
        reg = self.reg()
        self.assertEqual((reg.utm_source, reg.utm_campaign, reg.ad_click_id), ('facebook', 'sunday', 'abc123'))

    # ---------------------------------------------------------------- register
    def test_register_creates_pending_row_and_a_follow_up_record_but_no_lead(self):
        from crm.models import Lead, LeadInbox, WebinarLead
        r = self.register()
        self.assertEqual(r.status_code, 200, r.content)
        out = r.json()
        self.assertEqual((out['success'], out['payment_session_id']), (True, 'sess_123'))
        reg = self.reg()
        self.assertEqual((reg.status, reg.name, reg.email, reg.phone, str(reg.amount)),
                         ('pending', 'meera joseph', 'meera@shop.test', '9847012345', '199.00'))
        kwargs = self.last_create.call_args.kwargs
        self.assertEqual((kwargs['customer_phone'], kwargs['customer_email']), ('9847012345', 'meera@shop.test'))
        self.assertTrue(kwargs['return_url'].endswith('?order_id={order_id}'))
        wl = WebinarLead.objects.get()
        self.assertEqual((wl.registration_id, wl.stage, wl.lead_id, wl.client_id), (reg.pk, 'registered', None, None))
        # registrants are not sales leads: the Leads list and the inbox stay untouched
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(LeadInbox.objects.exists())

    def test_business_name_is_optional(self):
        self.assertEqual(self.register(business_name='').status_code, 200)

    def test_validation_honeypot_and_missing_gateway(self):
        from pages.models import WebinarRegistration
        for bad in ({'phone': '12345'}, {'email': 'nope'}, {'name': 'A'}):
            self.assertEqual(self.register(**bad).status_code, 400, bad)
        self.assertEqual(self.register(website='http://spam.example').status_code, 400)
        self.assertFalse(WebinarRegistration.objects.exists())
        with override_settings(CASHFREE_APP_ID='', CASHFREE_SECRET_KEY=''):
            self.assertEqual(self.register().status_code, 503)

    def test_gateway_failure_is_reported(self):
        from pages import cashfree
        with patch('pages.webinar_views.cashfree.create_order', side_effect=cashfree.CashfreeError('gateway down')):
            r = self.client.post(self.url, self.data())
        self.assertEqual(r.status_code, 502)
        self.assertIn('gateway down', r.json()['error'])

    def test_retry_reuses_the_pending_row(self):
        from pages.models import WebinarRegistration
        self.register()
        first = self.reg().cashfree_order_id
        self.register(phone='9847012346')
        self.assertEqual(WebinarRegistration.objects.count(), 1)
        reg = self.reg()
        self.assertNotEqual(reg.cashfree_order_id, first)
        self.assertEqual(reg.phone, '9847012346')

    def test_already_paid_is_not_charged_twice(self):
        from pages.models import WebinarRegistration
        self.register()
        self.pay()
        before = len(django_mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.register()
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()['already'])
        self.assertEqual(WebinarRegistration.objects.count(), 1)
        self.assertEqual(len(django_mail.outbox), before + 1)         # the confirmation is sent again

    # ---------------------------------------------------------------- payment confirmation
    def test_webhook_confirms_seat_sends_email_and_creates_paid_lead_once(self):
        self.register()
        self.assertEqual(self.pay().status_code, 200)
        reg = self.reg()
        self.assertEqual(reg.status, 'paid')
        self.assertEqual(reg.cashfree_payment_id, '998877')
        self.assertIsNotNone(reg.confirmation_sent_at)
        mails = [m for m in django_mail.outbox if m.to == ['meera@shop.test']]
        self.assertEqual(len(mails), 1)
        msg = mails[0]
        self.assertIn('You are registered', msg.subject)
        self.assertIn('Hi Meera,', msg.body)
        self.assertIn('https://meet.google.com/aaa-bbbb-ccc', msg.body)
        self.assertIn('WEB-%05d' % reg.pk, msg.body)
        html = [c for c, mime in msg.alternatives if mime == 'text/html'][0]
        self.assertIn('Your seat is confirmed', html)
        self.assertEqual(msg.attachments[0][0], 'webinar.ics')
        # the team is told, and the CRM lead records the payment
        self.assertTrue(any(m.subject.startswith('[BThinkX] Webinar seat paid') for m in django_mail.outbox))
        from crm.models import Lead, LeadInbox, Notification, WebinarLead
        wl = WebinarLead.objects.get(registration=reg)
        self.assertTrue(wl.activities.filter(kind='paid').exists())
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(LeadInbox.objects.exists())
        self.assertTrue(Notification.objects.filter(event_type='webinar_registration', title__icontains='meera').exists())
        # a repeated webhook (Cashfree retries) changes nothing
        count = len(django_mail.outbox)
        self.pay(reg)
        self.assertEqual(len(django_mail.outbox), count)

    def test_webhook_rejects_bad_signature(self):
        self.register()
        r = self.client.post(reverse('pages:webinar_webhook'), b'{}', content_type='application/json',
                             HTTP_X_WEBHOOK_TIMESTAMP='1', HTTP_X_WEBHOOK_SIGNATURE='bad')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.reg().status, 'pending')

    def test_old_offer_webhook_url_also_confirms_webinar_orders(self):
        self.register()
        reg = self.reg()
        body, headers = signed({'data': {'order': {'order_id': reg.cashfree_order_id},
                                         'payment': {'payment_status': 'SUCCESS', 'cf_payment_id': 5}}})
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse('pages:navaratri_offer_webhook'), body, content_type='application/json', **headers)
        self.assertEqual(r.status_code, 200)
        reg.refresh_from_db()
        self.assertEqual(reg.status, 'paid')

    def test_failed_payment_webhook(self):
        self.register()
        reg = self.reg()
        body, headers = signed({'data': {'order': {'order_id': reg.cashfree_order_id},
                                         'payment': {'payment_status': 'FAILED'}}})
        self.client.post(reverse('pages:webinar_webhook'), body, content_type='application/json', **headers)
        reg.refresh_from_db()
        self.assertEqual(reg.status, 'failed')

    def test_return_page_checks_with_cashfree(self):
        self.register()
        reg = self.reg()
        url = reverse('pages:webinar_return') + f'?order_id={reg.cashfree_order_id}'
        with patch('pages.webinar.cashfree.fetch_order', return_value={'order_status': 'ACTIVE'}):
            self.assertContains(self.client.get(url), 'Confirming your payment')
        with patch('pages.webinar.cashfree.fetch_order', return_value={'order_status': 'PAID'}):
            with self.captureOnCommitCallbacks(execute=True):
                r = self.client.get(url)
        self.assertContains(r, 'You are registered')
        self.assertContains(r, 'meera@shop.test')
        self.assertContains(r, 'Add to calendar')
        self.assertEqual(self.reg().status, 'paid')
        self.assertEqual(len([m for m in django_mail.outbox if m.to == ['meera@shop.test']]), 1)

    def test_return_page_failed_unknown_and_gateway_error(self):
        from pages import cashfree
        self.register()
        reg = self.reg()
        url = reverse('pages:webinar_return') + f'?order_id={reg.cashfree_order_id}'
        with patch('pages.webinar.cashfree.fetch_order', side_effect=cashfree.CashfreeError('x')):
            self.assertContains(self.client.get(url), 'Confirming your payment')
        with patch('pages.webinar.cashfree.fetch_order', return_value={'order_status': 'EXPIRED'}):
            self.assertContains(self.client.get(url), 'Payment was not completed')
        self.assertContains(self.client.get(reverse('pages:webinar_return') + '?order_id=nope'), 'could not find')

    def test_calendar_file(self):
        self.register()
        reg = self.reg()
        url = reverse('pages:webinar_calendar') + f'?order_id={reg.cashfree_order_id}'
        self.assertEqual(self.client.get(url).status_code, 404)           # not paid yet
        self.pay(reg)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        for needle in ('BEGIN:VEVENT', 'SUMMARY:How to Sell Online', f'UID:webinar-{self.w.pk}@bthinkx', 'DTSTART:'):
            self.assertIn(needle, body)
        for line in body.split('\r\n'):
            self.assertLessEqual(len(line.encode()), 75, line)

    # ---------------------------------------------------------------- CRM
    def test_crm_registrations_page_and_actions(self):
        from pages.models import WebinarRegistration
        self.register()
        self.pay()
        WebinarRegistration.objects.create(webinar=self.w, name='Abandoned Ann', email='ann@x.test', phone='9000000001',
                                           amount=199, cashfree_order_id='WEBX1')
        self.login('owner')
        r = self.client.get(reverse('crm:webinar_registrations'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'meera joseph')
        self.assertContains(r, 'Abandoned Ann')
        self.assertContains(r, 'Paid seats')
        paid = WebinarRegistration.objects.get(status='paid')
        wl_id = paid.crm.pk
        before = len(django_mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:webinar_registration_action', args=[wl_id]), {'action': 'resend'})
        self.assertEqual(len(django_mail.outbox), before + 1)
        self.client.post(reverse('crm:webinar_registration_action', args=[wl_id]), {'action': 'attended'})
        paid.refresh_from_db()
        self.assertTrue(paid.attended)
        csv = self.client.get(reverse('crm:webinar_registrations') + '?export=csv')
        self.assertEqual(csv.status_code, 200)

    def test_crm_edit_and_send_joining_links(self):
        self.register()
        self.pay()
        self.login('owner')
        new_time = (timezone.localtime() + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)
        r = self.client.post(reverse('crm:webinar_edit', args=[self.w.pk]), {
            'title': self.w.title, 'subtitle': self.w.subtitle, 'starts_at': new_time.strftime('%Y-%m-%dT%H:%M'),
            'duration_minutes': 60, 'price': '249', 'platform': 'Zoom', 'join_url': 'https://zoom.us/j/123',
            'host_name': 'Team BThinkX', 'host_title': 'x', 'seat_limit': '', 'learn_points': 'A | b',
            'audience_points': 'Owners', 'is_active': 'on'})
        self.assertEqual(r.status_code, 302, r.content.decode()[:500])
        self.w.refresh_from_db()
        self.assertEqual((self.w.platform, str(self.w.price), self.w.duration_minutes), ('Zoom', '249.00', 60))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:webinar_send_joining', args=[self.w.pk]))
        reg = self.reg()
        self.assertIsNotNone(reg.joining_sent_at)
        self.assertIn('Your joining link', django_mail.outbox[-1].subject)
        self.assertIn('https://zoom.us/j/123', django_mail.outbox[-1].body)
        # a second click only sends to people who have not had it
        count = len(django_mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:webinar_send_joining', args=[self.w.pk]))
        self.assertEqual(len(django_mail.outbox), count)

    def test_new_webinar_gets_a_unique_slug(self):
        self.login('owner')
        when = (timezone.localtime() + timedelta(days=9)).strftime('%Y-%m-%dT%H:%M')
        payload = {'title': 'How to Sell Online 10x Better Than Your Competitors', 'subtitle': 's', 'starts_at': when,
                   'duration_minutes': 90, 'price': '199', 'platform': 'Google Meet', 'host_name': 'h', 'host_title': 't',
                   'learn_points': '', 'audience_points': '', 'is_active': 'on'}
        self.assertEqual(self.client.post(reverse('crm:webinar_create'), payload).status_code, 302)
        from pages.models import Webinar
        slugs = sorted(Webinar.objects.values_list('slug', flat=True))
        self.assertEqual(len(set(slugs)), 2)

    def test_sales_user_can_view_but_not_configure(self):
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:webinar_registrations')).status_code, 200)
        self.assertIn(self.client.get(reverse('crm:webinar_edit', args=[self.w.pk])).status_code, (302, 403, 404))
        self.assertIn(self.client.get(reverse('crm:webinar_create')).status_code, (302, 403, 404))

    # ---------------------------------------------------------------- timers
    def test_reminders_go_out_once_each(self):
        from crm.modules.webinars import jobs
        self.register()
        self.pay()
        self.w.starts_at = timezone.now() + timedelta(hours=20)
        self.w.save()
        before = len(django_mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(jobs.reminders(), 1)
            self.assertEqual(jobs.reminders(), 0)
        self.assertEqual(len(django_mail.outbox), before + 1)
        self.assertIn('Tomorrow', django_mail.outbox[-1].subject)
        self.w.starts_at = timezone.now() + timedelta(minutes=50)
        self.w.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(jobs.reminders(), 1)
        self.assertIn('Starting in 1 hour', django_mail.outbox[-1].subject)

    def test_reconcile_job_confirms_payments_the_browser_never_reported(self):
        from crm.modules.webinars import jobs
        from pages.models import WebinarRegistration
        self.register()
        WebinarRegistration.objects.update(created_at=timezone.now() - timedelta(minutes=10))
        with patch('pages.webinar.cashfree.fetch_order', return_value={'order_status': 'PAID'}):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(jobs.reconcile_pending(), 1)
        self.assertEqual(self.reg().status, 'paid')
        self.assertEqual(len([m for m in django_mail.outbox if m.to == ['meera@shop.test']]), 1)

    def test_email_failure_never_blocks_the_seat(self):
        self.register()
        with patch('pages.webinar_mail.base._render', side_effect=RuntimeError('template boom')):
            self.pay()
        self.assertEqual(self.reg().status, 'paid')


@override_settings(ALLOWED_HOSTS=['testserver'], **CF)
class WebinarFollowUpTests(CrmTestCase):
    """The Webinar section tracks registrants; Leads and Clients are created only on purpose, linked by foreign keys."""

    def setUp(self):
        super().setUp()
        cache.clear()
        from pages.models import Webinar, WebinarRegistration
        Webinar.objects.all().delete()
        self.w = Webinar.objects.create(starts_at=timezone.now() + timedelta(days=2))
        self.reg = WebinarRegistration.objects.create(
            webinar=self.w, name='Meera Joseph', email='meera@shop.test', phone='9847012345', business_name='Meera Sarees',
            amount=199, status='paid', paid_at=timezone.now(), cashfree_order_id='WEBT1', utm_campaign='sunday-ad')
        self.wl = self.reg.crm            # created automatically for every registration

    def test_every_registration_gets_a_follow_up_record_once(self):
        from crm.models import WebinarLead
        self.assertEqual(WebinarLead.objects.count(), 1)
        self.reg.name = 'Meera J'
        self.reg.save()
        self.assertEqual(WebinarLead.objects.count(), 1)

    def test_registrants_never_appear_in_the_leads_list(self):
        self.login('owner')
        self.assertNotContains(self.client.get(reverse('crm:lead_list')), 'Meera Joseph')
        self.assertContains(self.client.get(reverse('crm:webinar_registrations')), 'Meera Joseph')

    def test_stage_owner_and_notes_build_a_history(self):
        self.login('manager')
        self.client.post(reverse('crm:webinar_lead_assign', args=[self.wl.pk]), {'owner': self.u['sales'].pk})
        self.client.post(reverse('crm:webinar_lead_stage', args=[self.wl.pk]), {'stage': 'qualified', 'note': 'Has a shop'})
        fu = (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('crm:webinar_lead_note', args=[self.wl.pk]),
                         {'kind': 'call', 'text': 'Spoke for 10 minutes', 'follow_up': fu})
        self.wl.refresh_from_db()
        self.assertEqual((self.wl.stage, self.wl.owner_id), ('qualified', self.u['sales'].pk))
        self.assertIsNotNone(self.wl.qualified_at)
        self.assertIsNotNone(self.wl.next_follow_up_at)
        kinds = list(self.wl.activities.values_list('kind', flat=True))
        for k in ('assigned', 'stage', 'call'):
            self.assertIn(k, kinds)
        page = self.client.get(reverse('crm:webinar_lead_detail', args=[self.wl.pk]))
        self.assertContains(page, 'Spoke for 10 minutes')
        self.assertContains(page, 'Move to Leads pipeline')

    def test_invalid_stage_and_owner_are_refused(self):
        self.login('manager')
        self.client.post(reverse('crm:webinar_lead_stage', args=[self.wl.pk]), {'stage': 'converted'})
        self.client.post(reverse('crm:webinar_lead_stage', args=[self.wl.pk]), {'stage': 'nonsense'})
        self.client.post(reverse('crm:webinar_lead_assign', args=[self.wl.pk]), {'owner': '99999'})
        self.wl.refresh_from_db()
        self.assertEqual((self.wl.stage, self.wl.owner_id), ('registered', None))

    def test_move_to_leads_creates_one_linked_lead(self):
        from crm.models import Lead
        self.login('manager')
        r = self.client.post(reverse('crm:webinar_lead_convert', args=[self.wl.pk]), {'to': 'lead'})
        self.wl.refresh_from_db()
        lead = Lead.objects.get()
        self.assertRedirects(r, reverse('crm:lead_detail', args=[lead.pk]))
        self.assertEqual((self.wl.lead_id, self.wl.stage), (lead.pk, 'converted'))
        self.assertEqual((lead.name, lead.company, lead.phone[-10:]), ('Meera Joseph', 'Meera Sarees', '9847012345'))
        self.assertEqual(lead.source.name, 'Webinar')
        self.assertIn('Webinar:', lead.campaign.name)
        # doing it again does not create a second lead
        self.client.post(reverse('crm:webinar_lead_convert', args=[self.wl.pk]), {'to': 'lead'})
        self.assertEqual(Lead.objects.count(), 1)
        self.assertTrue(self.wl.activities.filter(kind='converted').exists())

    def test_move_to_leads_links_an_existing_lead_instead_of_duplicating(self):
        from crm.models import Lead, LeadSource
        from crm.modules.leads import services as leads
        existing = leads.create_lead(leads.LeadInput(name='Meera', phone='9847012345',
                                                     source=LeadSource.objects.create(name='Manual', channel='manual')),
                                     self.u['sales'])
        self.login('manager')
        self.client.post(reverse('crm:webinar_lead_convert', args=[self.wl.pk]), {'to': 'lead'})
        self.wl.refresh_from_db()
        self.assertEqual(self.wl.lead_id, existing.pk)
        self.assertEqual(Lead.objects.count(), 1)

    def test_make_client_creates_lead_and_client_linked(self):
        from crm.models import Client, Lead
        self.login('owner')
        before = Client.objects.count()
        r = self.client.post(reverse('crm:webinar_lead_convert', args=[self.wl.pk]), {'to': 'client'})
        self.wl.refresh_from_db()
        self.assertEqual(Client.objects.count(), before + 1)
        self.assertIsNotNone(self.wl.client_id)
        self.assertEqual(self.wl.lead.client_id, self.wl.client_id)
        self.assertEqual(Lead.objects.count(), 1)
        self.assertRedirects(r, reverse('crm:client_detail', args=[self.wl.client_id]))
        self.assertEqual(self.wl.client.source_lead_id, self.wl.lead_id)
        page = self.client.get(reverse('crm:webinar_lead_detail', args=[self.wl.pk]))
        self.assertContains(page, 'Open client')

    def test_converted_registrant_cannot_change_stage(self):
        self.login('owner')
        self.client.post(reverse('crm:webinar_lead_convert', args=[self.wl.pk]), {'to': 'lead'})
        self.client.post(reverse('crm:webinar_lead_stage', args=[self.wl.pk]), {'stage': 'contacted'})
        self.wl.refresh_from_db()
        self.assertEqual(self.wl.stage, 'converted')

    def test_permissions(self):
        self.login('finance')            # no access to leads
        self.assertIn(self.client.get(reverse('crm:webinar_lead_detail', args=[self.wl.pk])).status_code, (302, 403, 404))
        self.assertIn(self.client.post(reverse('crm:webinar_lead_stage', args=[self.wl.pk]), {'stage': 'contacted'}).status_code,
                      (302, 403, 404))
        self.wl.refresh_from_db()
        self.assertEqual(self.wl.stage, 'registered')

    def test_filters_and_export(self):
        self.login('owner')
        url = reverse('crm:webinar_registrations')
        self.assertContains(self.client.get(url + '?stage=registered'), 'Meera Joseph')
        self.assertNotContains(self.client.get(url + '?stage=qualified'), 'Meera Joseph')
        self.assertContains(self.client.get(url + '?quick=paid'), 'Meera Joseph')
        csv = self.client.get(url + '?export=csv')
        self.assertEqual(csv.status_code, 200)
        self.assertIn('Meera Joseph', b''.join(csv.streaming_content).decode('utf-8-sig'))


@override_settings(ALLOWED_HOSTS=['testserver'], **CF)
class WebinarCatalogueTests(CrmTestCase):
    """Webinars are records of their own: a list, a registration link each, editable price/time, registrations per webinar."""

    def setUp(self):
        super().setUp()
        cache.clear()
        from pages.models import Webinar, WebinarRegistration
        Webinar.objects.all().delete()
        self.a = Webinar.objects.create(title='Alpha', slug='alpha', starts_at=timezone.now() + timedelta(days=3), price=199)
        self.b = Webinar.objects.create(title='Beta', slug='beta', starts_at=timezone.now() + timedelta(days=10), price=499)
        WebinarRegistration.objects.create(webinar=self.a, name='Ann Alpha', email='ann@x.test', phone='9000000001', amount=199,
                                           status='paid', paid_at=timezone.now(), cashfree_order_id='A1')
        WebinarRegistration.objects.create(webinar=self.b, name='Ben Beta', email='ben@x.test', phone='9000000002', amount=499,
                                           cashfree_order_id='B1')

    def payload(self, **over):
        when = (timezone.localtime() + timedelta(days=20)).strftime('%Y-%m-%dT%H:%M')
        data = {'title': 'Gamma', 'subtitle': 's', 'starts_at': when, 'duration_minutes': 60, 'price': '299',
                'platform': 'Zoom', 'host_name': 'h', 'host_title': 't', 'learn_points': '', 'audience_points': '',
                'is_active': 'on'}
        data.update(over)
        return data

    def test_list_shows_each_webinar_with_its_own_link_and_numbers(self):
        self.login('owner')
        r = self.client.get(reverse('crm:webinar_list'))
        self.assertContains(r, 'Alpha')
        self.assertContains(r, 'Beta')
        self.assertContains(r, '/webinar/alpha/')
        self.assertContains(r, '/webinar/beta/')
        self.assertContains(r, 'data-copy=')
        self.assertContains(r, 'utm_source=facebook')
        self.assertContains(r, 'Rs. 499')

    def test_registrations_are_listed_per_webinar(self):
        self.login('owner')
        url = reverse('crm:webinar_registrations')
        both = self.client.get(url)
        self.assertContains(both, 'Ann Alpha')
        self.assertContains(both, 'Ben Beta')
        only_a = self.client.get(f'{url}?webinar={self.a.pk}')
        self.assertContains(only_a, 'Ann Alpha')
        self.assertNotContains(only_a, 'Ben Beta')
        self.assertContains(only_a, '/webinar/alpha/')

    def test_new_webinar_with_own_address_and_price(self):
        self.login('owner')
        r = self.client.post(reverse('crm:webinar_create'), self.payload(slug='growth-day'))
        self.assertRedirects(r, reverse('crm:webinar_list'))
        from pages.models import Webinar
        g = Webinar.objects.get(title='Gamma')
        self.assertEqual((g.slug, str(g.price), g.duration_minutes), ('growth-day', '299.00', 60))
        self.assertEqual(self.client.get('/webinar/growth-day/').status_code, 200)

    def test_address_must_be_unique_and_not_reserved(self):
        self.login('owner')
        for bad in ('alpha', 'return', 'webhook'):
            r = self.client.post(reverse('crm:webinar_create'), self.payload(slug=bad))
            self.assertEqual(r.status_code, 200, bad)
        from pages.models import Webinar
        self.assertFalse(Webinar.objects.filter(title='Gamma').exists())

    def test_blank_address_is_generated_and_edit_keeps_it(self):
        self.login('owner')
        self.client.post(reverse('crm:webinar_create'), self.payload(title='Fresh Start'))
        from pages.models import Webinar
        w = Webinar.objects.get(title='Fresh Start')
        self.assertEqual(w.slug, 'fresh-start')
        self.client.post(reverse('crm:webinar_edit', args=[w.pk]), self.payload(title='Fresh Start 2', slug=''))
        w.refresh_from_db()
        self.assertEqual((w.title, w.slug), ('Fresh Start 2', 'fresh-start'))

    def test_price_change_applies_to_new_registrations_only(self):
        from pages.models import WebinarRegistration
        self.login('owner')
        self.client.post(reverse('crm:webinar_edit', args=[self.a.pk]), self.payload(title='Alpha', slug='alpha', price='999'))
        self.a.refresh_from_db()
        self.assertEqual(str(self.a.price), '999.00')
        self.assertEqual(WebinarRegistration.objects.get(cashfree_order_id='A1').amount, 199)
        self.assertContains(self.client.get('/webinar/alpha/'), '999')

    def test_seat_limit_cannot_go_below_paid_seats(self):
        self.login('owner')
        r = self.client.post(reverse('crm:webinar_edit', args=[self.a.pk]),
                             self.payload(title='Alpha', slug='alpha', seat_limit='0'))
        self.assertEqual(r.status_code, 200)

    def test_duplicate_makes_an_offline_copy_with_a_new_address(self):
        from pages.models import Webinar
        self.login('owner')
        self.a.join_url = 'https://meet.google.com/abc-defg-hij'
        self.a.save()
        r = self.client.post(reverse('crm:webinar_duplicate', args=[self.a.pk]))
        copy = Webinar.objects.exclude(pk__in=[self.a.pk, self.b.pk]).get()
        self.assertRedirects(r, reverse('crm:webinar_edit', args=[copy.pk]))
        self.assertFalse(copy.is_active)
        self.assertEqual((copy.join_url, str(copy.price)), ('', '199.00'))
        self.assertNotEqual(copy.slug, 'alpha')
        self.assertEqual(copy.registrations.count(), 0)
        self.assertEqual(self.client.get(f'/webinar/{copy.slug}/').status_code, 404)

    def test_take_offline_and_back_keeps_registrations(self):
        self.login('owner')
        self.client.post(reverse('crm:webinar_toggle', args=[self.a.pk]))
        self.a.refresh_from_db()
        self.assertFalse(self.a.is_active)
        self.assertEqual(self.client.get('/webinar/alpha/').status_code, 404)
        self.assertEqual(self.a.registrations.count(), 1)
        self.client.post(reverse('crm:webinar_toggle', args=[self.a.pk]))
        self.a.refresh_from_db()
        self.assertTrue(self.a.is_active)

    def test_only_get_is_refused_on_actions_and_permissions(self):
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:webinar_list')).status_code, 200)
        for name in ('webinar_duplicate', 'webinar_toggle'):
            self.assertIn(self.client.post(reverse(f'crm:{name}', args=[self.a.pk])).status_code, (302, 403, 404))
        self.a.refresh_from_db()
        self.assertTrue(self.a.is_active)
        self.assertEqual(self.client.get(reverse('crm:webinar_toggle', args=[self.a.pk])).status_code, 405)


META = dict(META_PIXEL_ID='123456789012345', META_CAPI_TOKEN='tok-secret', META_CAPI_ASYNC=False, META_TEST_EVENT_CODE='')


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {'events_received': 1} if body is None else body

    def json(self):
        return self._body


@override_settings(ALLOWED_HOSTS=['testserver'], **CF)
class MetaTrackingTests(CrmTestCase):
    """Meta pixel in the browser plus Conversions API from the server, merged by event id."""

    data, register, reg, pay = WebinarTests.data, WebinarTests.register, WebinarTests.reg, WebinarTests.pay

    def setUp(self):
        super().setUp()
        cache.clear()
        from pages.models import Webinar
        Webinar.objects.all().delete()
        self.w = Webinar.objects.create(starts_at=timezone.now() + timedelta(days=2))
        self.url = reverse('pages:webinar_register', args=[self.w.slug])

    def test_pixel_off_without_id(self):
        html = self.client.get(reverse('pages:webinar')).content.decode()
        self.assertNotIn('fbevents.js', html)

    @override_settings(META_PIXEL_ID='123456789012345')
    def test_pixel_on_landing_and_result_page(self):
        html = self.client.get(reverse('pages:webinar')).content.decode()
        self.assertIn("fbq('init','123456789012345')", html)
        self.assertIn("fbq('track','PageView')", html)
        self.assertIn('data-content-id="ecommerce-10x"', html)
        self.assertIn('data-price="199.00"', html)
        js = open('new/assets/webinar.js', encoding='utf-8').read()
        for needle in ("'ViewContent'", "'Lead'", "'InitiateCheckout'", 'res.data.order_id'):
            self.assertIn(needle, js)
        self.register()
        self.pay()
        reg = self.reg()
        page = self.client.get(reverse('pages:webinar_return') + f'?order_id={reg.cashfree_order_id}').content.decode()
        self.assertIn("fbq('track', 'Purchase'", page)
        self.assertIn(f"eventID: '{reg.cashfree_order_id}'", page)
        self.assertIn('value = 199.00', page)

    def test_no_server_events_without_token(self):
        from pages.models import MetaEvent
        with override_settings(META_PIXEL_ID='123456789012345', META_CAPI_TOKEN=''):
            self.register()
            self.pay()
        self.assertFalse(MetaEvent.objects.exists())

    @override_settings(**META)
    def test_register_stores_browser_details_and_queues_lead_and_checkout(self):
        from pages.models import MetaEvent
        self.client.cookies['_fbp'] = 'fb.1.1700000000000.1234567890'
        self.client.cookies['_fbc'] = 'fb.1.1700000000000.AbCdEfGhIjKl'
        self.register(ad_click_id='ignored-because-cookie-wins')
        reg = self.reg()
        self.assertEqual((reg.fbp, reg.fbc), ('fb.1.1700000000000.1234567890', 'fb.1.1700000000000.AbCdEfGhIjKl'))
        self.assertTrue(reg.client_ip)
        events = {e.event_name: e for e in MetaEvent.objects.all()}
        self.assertEqual(set(events), {'Lead', 'InitiateCheckout'})
        p = events['Lead'].payload
        self.assertEqual(p['event_id'], reg.cashfree_order_id)
        self.assertEqual(p['action_source'], 'website')
        self.assertEqual(p['user_data']['em'], [hashlib.sha256(b'meera@shop.test').hexdigest()])
        self.assertEqual(p['user_data']['ph'], [hashlib.sha256(b'919847012345').hexdigest()])
        self.assertEqual(p['user_data']['fn'], [hashlib.sha256(b'meera').hexdigest()])
        self.assertEqual(p['user_data']['ln'], [hashlib.sha256(b'joseph').hexdigest()])
        self.assertEqual(p['user_data']['fbp'], reg.fbp)
        self.assertEqual((p['custom_data']['value'], p['custom_data']['currency']), (199.0, 'INR'))
        blob = json.dumps(p)
        for raw in ('meera@shop.test', '9847012345', 'Meera Sarees'):
            self.assertNotIn(raw, blob)          # personal data only ever leaves hashed

    @override_settings(**META)
    def test_fbc_is_built_from_the_ad_click_id_and_junk_cookies_are_dropped(self):
        self.client.cookies['_fbp'] = 'not-a-valid-cookie'
        self.register(ad_click_id='IwAR0abcDEF123456')
        reg = self.reg()
        self.assertEqual(reg.fbp, '')
        self.assertRegex(reg.fbc, r'^fb\.1\.\d{13}\.IwAR0abcDEF123456$')

    @override_settings(**META)
    def test_purchase_is_queued_once_when_payment_is_confirmed(self):
        from pages.models import MetaEvent
        self.register()
        self.pay()
        self.pay()                                # a repeated webhook must not add a second purchase
        reg = self.reg()
        purchases = MetaEvent.objects.filter(event_name='Purchase')
        self.assertEqual(purchases.count(), 1)
        p = purchases.get().payload
        self.assertEqual((p['event_id'], p['custom_data']['order_id']), (reg.cashfree_order_id,) * 2)
        self.assertEqual(p['event_time'], int(reg.paid_at.timestamp()))

    @override_settings(**META)
    def test_send_posts_to_meta_with_token_in_header_only(self):
        from pages import meta
        from pages.models import MetaEvent
        self.register()
        ev = MetaEvent.objects.get(event_name='Lead')
        with patch('pages.meta.requests.post', return_value=FakeResponse()) as post:
            self.assertTrue(meta.send(ev.pk))
        url = post.call_args.args[0]
        self.assertEqual(url, 'https://graph.facebook.com/v21.0/123456789012345/events')
        self.assertNotIn('tok-secret', url)
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer tok-secret')
        self.assertNotIn('test_event_code', post.call_args.kwargs['json'])
        ev.refresh_from_db()
        self.assertEqual((ev.status, ev.attempts), ('sent', 1))
        with patch('pages.meta.requests.post') as again:
            self.assertFalse(meta.send(ev.pk))     # a sent event is never sent twice
        again.assert_not_called()

    @override_settings(**{**META, 'META_TEST_EVENT_CODE': 'TEST123'})
    def test_test_event_code_is_included_when_set(self):
        from pages import meta
        from pages.models import MetaEvent
        self.register()
        with patch('pages.meta.requests.post', return_value=FakeResponse()) as post:
            meta.send(MetaEvent.objects.get(event_name='Lead').pk)
        self.assertEqual(post.call_args.kwargs['json']['test_event_code'], 'TEST123')

    @override_settings(**META)
    def test_failures_are_retried_with_backoff_then_given_up(self):
        import requests
        from pages import meta
        from pages.models import MetaEvent
        self.register()
        ev = MetaEvent.objects.get(event_name='Lead')
        with patch('pages.meta.requests.post', side_effect=requests.ConnectionError('boom tok-secret')):
            self.assertFalse(meta.send(ev.pk))
        ev.refresh_from_db()
        self.assertEqual((ev.status, ev.attempts), ('failed', 1))
        self.assertNotIn('tok-secret', ev.last_error)
        with patch('pages.meta.requests.post', return_value=FakeResponse()) as post:
            self.assertEqual(meta.retry_pending(), 0)          # too soon: waits before the second try
            post.assert_not_called()
            MetaEvent.objects.filter(pk=ev.pk).update(last_attempt_at=timezone.now() - timedelta(minutes=10))
            self.assertEqual(meta.retry_pending(), 1)
        ev.refresh_from_db()
        self.assertEqual((ev.status, ev.attempts), ('sent', 2))
        # permanent errors (bad token) stop retrying at once
        ic = MetaEvent.objects.get(event_name='InitiateCheckout')
        bad = FakeResponse(400, {'error': {'message': 'Invalid OAuth access token.', 'code': 190}})
        with patch('pages.meta.requests.post', return_value=bad):
            meta.send(ic.pk)
        ic.refresh_from_db()
        self.assertEqual((ic.status, ic.attempts), ('failed', meta.MAX_ATTEMPTS))
        self.assertIn('Invalid OAuth', ic.last_error)

    @override_settings(**META)
    def test_old_events_are_not_sent(self):
        from pages import meta
        from pages.models import MetaEvent
        self.register()
        ev = MetaEvent.objects.get(event_name='Lead')
        MetaEvent.objects.filter(pk=ev.pk).update(event_time=timezone.now() - timedelta(days=8))
        with patch('pages.meta.requests.post') as post:
            self.assertFalse(meta.send(ev.pk))
        post.assert_not_called()

    @override_settings(**META)
    def test_tracking_failure_never_breaks_registration_or_payment(self):
        with patch('pages.meta.build_event', side_effect=RuntimeError('boom')):
            r = self.register()
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.pay().status_code, 200)
        self.assertEqual(self.reg().status, 'paid')

    @override_settings(**META)
    def test_events_show_in_the_crm_registration_page(self):
        self.register()
        self.pay()
        self.login('owner')
        page = self.client.get(reverse('crm:webinar_lead_detail', args=[self.reg().crm.pk]))
        self.assertContains(page, 'Meta ad events')
        self.assertContains(page, 'Purchase')


TOKEN = 'EAAB' + 'x' * 60


@override_settings(ALLOWED_HOSTS=['testserver'], META_PIXEL_ID='', META_CAPI_TOKEN='', META_TEST_EVENT_CODE='', META_CAPI_ASYNC=False, **CF)
class MetaSettingsInCrmTests(CrmTestCase):
    """Pixel id, token and test code are edited by an administrator in the CRM, not by redeploying."""

    data, register, reg, pay = WebinarTests.data, WebinarTests.register, WebinarTests.reg, WebinarTests.pay

    def setUp(self):
        super().setUp()
        cache.clear()
        from pages.models import Webinar
        Webinar.objects.all().delete()
        self.w = Webinar.objects.create(starts_at=timezone.now() + timedelta(days=2))
        self.url = reverse('pages:webinar_register', args=[self.w.slug])
        self.page = reverse('crm:webinar_tracking')

    def save(self, **over):
        data = {'pixel_enabled': 'on', 'pixel_id': '123456789012345', 'capi_enabled': 'on', 'capi_token': TOKEN,
                'test_event_code': '', 'graph_version': 'v21.0'}
        data.update(over)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post(self.page, data)

    def test_only_the_owner_can_open_or_change_it(self):
        for who in ('manager', 'sales', 'finance'):
            self.login(who)
            self.assertEqual(self.client.get(self.page).status_code, 403, who)
            self.assertEqual(self.save().status_code, 403, who)
            self.assertEqual(self.client.post(reverse('crm:webinar_tracking_test')).status_code, 403, who)
        from pages.models import MetaTracking
        self.assertFalse(MetaTracking.objects.exclude(pixel_id='').exists())

    def test_owner_saves_and_the_public_page_uses_it_at_once(self):
        self.login('owner')
        self.assertContains(self.client.get(self.page), 'Tracking is off')
        self.assertNotIn('fbevents.js', self.client.get(reverse('pages:webinar')).content.decode())
        self.assertEqual(self.save().status_code, 302)
        html = self.client.get(reverse('pages:webinar')).content.decode()
        self.assertIn("fbq('init','123456789012345')", html)
        self.assertContains(self.client.get(self.page), 'Browser and server tracking are on')

    def test_token_is_encrypted_and_never_shown_again(self):
        from pages.models import MetaTracking
        self.login('owner')
        self.save()
        row = MetaTracking.current()
        self.assertTrue(row.capi_token_enc)
        self.assertNotIn(TOKEN, row.capi_token_enc)
        page = self.client.get(self.page)
        self.assertNotContains(page, TOKEN)
        self.assertContains(page, 'A token is stored')
        from crm.models import AuditLog
        self.assertFalse(any(TOKEN in str(a.changes) for a in AuditLog.objects.filter(action='webinar.meta_settings')))
        # blank keeps it; the remove box clears it
        self.save(capi_token='', pixel_id='999999999999')
        row.refresh_from_db()
        self.assertTrue(row.capi_token_enc)
        self.assertEqual(row.pixel_id, '999999999999')
        self.save(capi_token='', clear_token='on')
        row.refresh_from_db()
        self.assertEqual(row.capi_token_enc, '')

    def test_saved_values_beat_the_environment_and_empty_ones_fall_back(self):
        from pages import meta
        self.login('owner')
        with override_settings(META_PIXEL_ID='111111111', META_CAPI_TOKEN='envtoken' * 6, META_TEST_EVENT_CODE='ENVTEST'):
            cfg = meta.config()
            self.assertEqual((cfg.pixel_id, cfg.source, cfg.test_code, cfg.capi_on), ('111111111', 'environment', 'ENVTEST', True))
            self.save(pixel_id='222222222', capi_token='', test_event_code='')
            cfg = meta.config()
            self.assertEqual((cfg.pixel_id, cfg.source), ('222222222', 'crm'))
            self.assertEqual((cfg.token, cfg.test_code), ('envtoken' * 6, 'ENVTEST'))

    def test_switches_turn_each_half_off_without_losing_values(self):
        from pages import meta
        self.login('owner')
        self.save()
        self.save(pixel_enabled=None, capi_token='')
        cfg = meta.config()
        self.assertEqual((cfg.pixel_on, cfg.capi_on, cfg.pixel_id), (False, True, '123456789012345'))
        self.assertNotIn('fbevents.js', self.client.get(reverse('pages:webinar')).content.decode())
        self.save(capi_enabled=None, capi_token='')
        cfg = meta.config()
        self.assertEqual((cfg.pixel_on, cfg.capi_on), (True, False))

    def test_server_events_use_the_saved_token_and_test_code(self):
        from pages import meta
        from pages.models import MetaEvent
        self.login('owner')
        self.save(test_event_code='TEST777', graph_version='v22.0')
        self.client.logout()
        self.register()
        ev = MetaEvent.objects.get(event_name='Lead')
        with patch('pages.meta.requests.post', return_value=type('R', (), {'status_code': 200, 'json': lambda s: {'events_received': 1}})()) as post:
            self.assertTrue(meta.send(ev.pk))
        self.assertEqual(post.call_args.args[0], 'https://graph.facebook.com/v22.0/123456789012345/events')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], f'Bearer {TOKEN}')
        self.assertEqual(post.call_args.kwargs['json']['test_event_code'], 'TEST777')

    def test_bad_values_are_refused(self):
        from pages.models import MetaTracking
        self.login('owner')
        for over in ({'pixel_id': 'abc'}, {'pixel_id': '123'}, {'capi_token': 'short'}, {'test_event_code': 'has space'},
                     {'graph_version': '21'}):
            self.assertEqual(self.save(**over).status_code, 200, over)
        self.assertFalse(MetaTracking.objects.exclude(pixel_id='').exists())

    def test_test_event_needs_the_code_and_reports_meta_answers(self):
        self.login('owner')
        self.save()
        with patch('pages.meta.requests.post') as post:
            r = self.client.post(reverse('crm:webinar_tracking_test'), follow=True)
        post.assert_not_called()
        self.assertContains(r, 'Enter the test event code')
        self.save(test_event_code='TEST777', capi_token='')
        ok = type('R', (), {'status_code': 200, 'json': lambda s: {'events_received': 1}})()
        with patch('pages.meta.requests.post', return_value=ok) as post:
            r = self.client.post(reverse('crm:webinar_tracking_test'), follow=True)
        self.assertContains(r, 'Meta accepted the test event')
        self.assertEqual(post.call_args.kwargs['json']['test_event_code'], 'TEST777')
        bad = type('R', (), {'status_code': 400, 'json': lambda s: {'error': {'message': 'Invalid OAuth access token.'}}})()
        with patch('pages.meta.requests.post', return_value=bad):
            r = self.client.post(reverse('crm:webinar_tracking_test'), follow=True)
        self.assertContains(r, 'Invalid OAuth access token')

    def test_button_on_the_webinar_list_is_for_the_owner_only(self):
        self.login('owner')
        self.assertContains(self.client.get(reverse('crm:webinar_list')), 'Meta tracking')
        self.login('manager')
        self.assertNotContains(self.client.get(reverse('crm:webinar_list')), 'Meta tracking')
