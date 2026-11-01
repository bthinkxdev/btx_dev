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


class SalesNavTests(CrmTestCase):
    def _sections(self, key):
        from crm.core.nav import build_nav
        return {g['label'] for g in build_nav(self.u[key], 'my_day')}

    def test_sales_executive_and_manager_have_no_money_people_or_delivery_section(self):
        for key in ('sales', 'manager'):
            got = self._sections(key)
            self.assertIn('SALES', got, key)
            for hidden in ('MONEY', 'PEOPLE', 'DELIVERY'):
                self.assertNotIn(hidden, got, f'{key}: {hidden}')

    def test_sales_roles_hold_no_money_or_delivery_views(self):
        from crm.core import permissions
        for key in ('sales', 'manager'):
            for module in ('payments', 'invoices', 'accounting', 'banking', 'payroll', 'contracts', 'projects', 'tickets'):
                self.assertFalse(permissions.has_perm(self.u[key], module, 'view'), f'{key}: {module}')

    def test_finance_hr_and_owner_still_see_their_sections(self):
        self.assertIn('MONEY', self._sections('finance'))
        self.assertIn('PEOPLE', self._sections('hr'))
        self.assertIn('DELIVERY', self._sections('pm'))
        got = self._sections('owner')
        self.assertTrue({'MONEY', 'PEOPLE'} <= got)


    def test_money_section_is_finance_team_only(self):
        for key in ('sales', 'manager', 'hr', 'pm', 'dev'):
            self.assertNotIn('MONEY', self._sections(key), key)
        for key in ('finance', 'accountant', 'auditor', 'owner'):
            self.assertIn('MONEY', self._sections(key), key)

    def test_everyone_can_still_file_their_own_expenses(self):
        from crm.core.nav import build_nav
        for key in ('sales', 'manager', 'hr', 'pm', 'dev'):
            labels = [i['label'] for g in build_nav(self.u[key], 'my_day') for i in g['items']]
            self.assertIn('My expenses', labels, key)
        finance = [i['label'] for g in build_nav(self.u['finance'], 'my_day') for i in g['items']]
        self.assertNotIn('My expenses', finance)

    def test_non_finance_roles_cannot_open_invoices_or_payments(self):
        from django.urls import reverse
        for key in ('sales', 'manager', 'hr', 'pm', 'dev'):
            self.login(key)
            for name in ('invoice_list', 'payment_list', 'receivables'):
                self.assertEqual(self.client.get(reverse(f'crm:{name}')).status_code, 403, f'{key}: {name}')
