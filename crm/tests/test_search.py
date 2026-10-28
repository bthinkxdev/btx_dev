"""List search: multi-word and phone-digit matching."""
from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from crm.models import Lead, LeadSource

from .base import CrmTestCase


class LeadSearchTests(CrmTestCase):
    def setUp(self):
        super().setUp()
        self.login('owner')
        src = LeadSource.objects.first() or LeadSource.objects.create(name='Web', kind='website')
        Lead.objects.create(name='John Mathew', company='Acme Travels', phone='9876543210', source=src, next_follow_up_at=timezone.now() + timedelta(days=1))
        Lead.objects.create(name='Asha Rao', company='Zen Foods', phone='9123456780', source=src, next_follow_up_at=timezone.now() + timedelta(days=1))

    def names(self, q):
        html = self.client.get(reverse('crm:lead_list'), {'q': q}).content.decode()
        return ('John Mathew' in html, 'Asha Rao' in html)

    def test_words_match_across_fields(self):
        self.assertEqual(self.names('john acme'), (True, False))

    def test_phone_with_separators(self):
        self.assertEqual(self.names('+91 98765-43210'), (True, False))

    def test_partial_name(self):
        self.assertEqual(self.names('ash'), (False, True))
