"""Phone bottom bar and mobile table layout."""
from __future__ import annotations

from django.urls import reverse

from crm.core.nav import mobile_tabs

from .base import CrmTestCase


class MobileTests(CrmTestCase):
    def test_tabs_follow_permissions(self):
        sales = [t['label'] for t in mobile_tabs(self.u['sales'], '')]
        self.assertEqual(sales[:3], ['My Day', 'Leads', 'Follow-ups'])
        dev = [t['label'] for t in mobile_tabs(self.u['dev'], '')]
        self.assertNotIn('Leads', dev)
        self.assertIn('My Day', dev)

    def test_active_tab(self):
        tabs = mobile_tabs(self.u['sales'], 'lead_detail')
        self.assertTrue(next(t for t in tabs if t['label'] == 'Leads')['active'])

    def test_bottom_bar_and_line_tables_render(self):
        self.login('sales')
        html = self.client.get(reverse('crm:my_day')).content.decode()
        self.assertIn('aria-label="Quick navigation"', html)
        self.assertIn('Open full menu', html)
        self.login('owner')
        self.assertIn('line-table', self.client.get(reverse('crm:journal_create')).content.decode())
