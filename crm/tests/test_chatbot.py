"""Website chatbot (public API, lead capture) and CRM chat history / AI analyst access."""
from __future__ import annotations

import json
from unittest import mock

from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from crm.core import llm
from crm.core.nav import build_nav
from crm.models import ChatSession, Lead

from .base import CrmTestCase


class ChatbotTests(CrmTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.visitor = Client(enforce_csrf_checks=True)   # no CSRF token: the public API must not need one

    def post(self, name, body):
        return self.visitor.post(reverse(f'chat_public:{name}'), json.dumps(body), content_type='application/json')

    def start(self):
        d = self.post('start', {'page': '/'}).json()
        self.assertTrue(d['ok'])
        return d['token']

    def test_start_greets_and_resumes(self):
        d = self.post('start', {}).json()
        self.assertIn('Bee', d['messages'][0]['text'])
        self.assertTrue(d['quick_replies'])
        again = self.post('start', {'token': d['token']}).json()
        self.assertEqual(again['token'], d['token'])
        self.assertEqual(ChatSession.objects.count(), 1)

    def test_answers_without_ai_key(self):
        token = self.start()
        with mock.patch.object(llm.settings, 'CHATBOT_API_KEY', ''):
            d = self.post('message', {'token': token, 'text': 'What are your packages and price?'}).json()
        self.assertTrue(d['ok'])
        self.assertIn('34,999', d['reply'])

    def test_uses_ai_reply_when_available(self):
        token = self.start()
        with mock.patch('crm.core.llm.complete', return_value=('Sure, happy to help!', 'test-model')):
            d = self.post('message', {'token': token, 'text': 'tell me about marketing'}).json()
        self.assertEqual(d['reply'], 'Sure, happy to help!')
        self.assertEqual(ChatSession.objects.get().messages.filter(role='bot').last().engine, 'test-model')

    def test_phone_in_message_creates_one_lead(self):
        token = self.start()
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            d = self.post('message', {'token': token, 'text': 'Call me on 98765 43211 please'}).json()
            self.assertTrue(d['captured'])
            self.post('message', {'token': token, 'text': 'my number is 9876543211'})
        lead = Lead.objects.get(phone__endswith='9876543211')
        self.assertEqual(lead.source.name, 'Website Chatbot')
        self.assertEqual(Lead.objects.filter(source=lead.source).count(), 1)
        session = ChatSession.objects.get()
        self.assertEqual(session.lead_id, lead.pk)

    def test_lead_form_validates_and_creates_lead(self):
        token = self.start()
        bad = self.post('lead', {'token': token, 'name': 'A', 'phone': '123'})
        self.assertEqual(bad.status_code, 400)
        ok = self.post('lead', {'token': token, 'name': 'Meera Nair', 'phone': '+91 98765 43299', 'business': 'Meera Sarees'}).json()
        self.assertTrue(ok['ok'])
        lead = Lead.objects.get(name='Meera Nair')
        self.assertEqual(lead.company, 'Meera Sarees')
        self.assertIn('Full chat', lead.notes)

    def test_honeypot_creates_nothing(self):
        token = self.start()
        self.post('lead', {'token': token, 'name': 'Bot', 'phone': '9876543288', 'website': 'spam'})
        self.assertFalse(Lead.objects.filter(name='Bot').exists())

    def test_foreign_origin_and_rate_limit(self):
        r = self.visitor.post(reverse('chat_public:start'), '{}', content_type='application/json', HTTP_ORIGIN='https://evil.example')
        self.assertEqual(r.status_code, 403)
        token = self.start()
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            codes = [self.post('message', {'token': token, 'text': 'hi'}).status_code for _ in range(45)]
        self.assertIn(429, codes)

    def test_unknown_token_is_rejected(self):
        r = self.post('message', {'token': '00000000-0000-0000-0000-000000000000', 'text': 'hi'})
        self.assertEqual(r.status_code, 404)


class ChatCrmAccessTests(CrmTestCase):
    def setUp(self):
        super().setUp()
        s = ChatSession.objects.create(visitor_name='Ravi', message_count=3)
        s.messages.create(role='visitor', content='Need an online store')
        self.session = s

    def test_only_manager_and_owner_see_chats(self):
        url = reverse('crm:chat_list')
        for key, code in [('owner', 200), ('manager', 200), ('sales', 403), ('finance', 403), ('hr', 403), ('auditor', 403)]:
            self.login(key)
            self.assertEqual(self.client.get(url).status_code, code, key)
            self.assertEqual(self.client.get(reverse('crm:chat_detail', args=[self.session.pk])).status_code, code, key)

    def test_detail_shows_transcript(self):
        self.login('manager')
        self.assertContains(self.client.get(reverse('crm:chat_detail', args=[self.session.pk])), 'Need an online store')

    def test_sidebar_entries_follow_permission(self):
        sales_urls = [c['href'] for g in build_nav(self.u['sales'], '') for i in g['items'] for c in i.get('children', [])]
        mgr_urls = [c['href'] for g in build_nav(self.u['manager'], '') for i in g['items'] for c in i.get('children', [])]
        self.assertNotIn(reverse('crm:chat_list'), sales_urls)
        self.assertIn(reverse('crm:chat_list'), mgr_urls)
        self.assertIn(reverse('crm:ai_analyst'), mgr_urls)
        self.assertNotIn(reverse('crm:ai_analyst'), sales_urls)

    def test_analyst_access_and_fallback_answer(self):
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:ai_analyst')).status_code, 403)
        self.assertEqual(self.client.post(reverse('crm:ai_analyst_ask'), '{}', content_type='application/json').status_code, 403)
        self.login('manager')
        self.assertEqual(self.client.get(reverse('crm:ai_analyst')).status_code, 200)
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            d = self.client.post(reverse('crm:ai_analyst_ask'), json.dumps({'question': 'who is best?', 'days': '30'}),
                                 content_type='application/json').json()
        self.assertTrue(d['ok'])
        self.assertEqual(d['engine'], 'rules')
        self.assertIn('Sales performance', d['answer'])

    def test_analyst_sends_no_customer_data_to_ai(self):
        self.login('owner')
        captured = {}

        def fake(msgs, **kw):
            captured['p'] = json.dumps(msgs)
            return 'ok', 'm'
        with mock.patch('crm.core.llm.complete', fake):
            self.client.post(reverse('crm:ai_analyst_ask'), json.dumps({'question': 'summary', 'days': '7'}),
                             content_type='application/json')
        self.assertNotIn('acme@example.test', captured['p'])
        self.assertNotIn('9876543210', captured['p'])
