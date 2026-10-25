"""Dashboards and reports (Part 13.1, 13.2, 13.7)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.urls import reverse
from django.utils import timezone

from crm.core import india
from crm.modules.reports import catalogue

from .base import CrmTestCase

DASHBOARDS = ['crm:dashboard_owner', 'crm:dashboard_sales', 'crm:dashboard_finance', 'crm:dashboard_delivery', 'crm:hr_dashboard']


class InsightsTest(CrmTestCase):
    def _lead(self, owner, name, **kw):
        from crm.models import Lead, LeadSource
        src = LeadSource.objects.first() or LeadSource.objects.create(name='Website', kind='website')
        return Lead.objects.create(name=name, phone=f'+9198765{Lead.objects.count():05d}', source=src, owner=owner,
                                   stage=kw.pop('stage', 'contacted'), value=kw.pop('value', Decimal('50000')), probability=40,
                                   next_follow_up_at=kw.pop('next_follow_up_at', timezone.now() + timedelta(days=1)), **kw)

    def setUp(self):
        super().setUp()
        self._lead(self.u['sales'], 'Own lead')
        self._lead(self.u['sales_b'], 'Colleague lead')
        self._lead(self.u['sales'], 'Overdue lead', next_follow_up_at=timezone.now() - timedelta(hours=3))

    def test_every_dashboard_renders_or_is_forbidden_for_every_role(self):
        self.make_invoice()
        for key in self.u:
            self.login(key)
            for name in DASHBOARDS:
                r = self.client.get(reverse(name))
                self.assertIn(r.status_code, (200, 403), f'{key} {name} -> {r.status_code}')
            for preset in ('today', 'week', 'quarter', 'fy', 'last_month'):
                self.assertIn(self.client.get(reverse('crm:dashboard_owner') + f'?preset={preset}').status_code, (200, 403))
        self.login('owner')
        r = self.client.get(reverse('crm:dashboard_owner') + '?from=2026-01-01&to=bad')
        self.assertEqual(r.status_code, 200)

    def test_card_count_equals_list_total(self):
        self.make_invoice()
        for who in ('owner', 'manager', 'sales'):
            self.login(who)
            for name in ('crm:dashboard_owner', 'crm:dashboard_sales', 'crm:dashboard_finance'):
                r = self.client.get(reverse(name))
                if r.status_code != 200:
                    continue
                for card in r.context['cards']:
                    if card.kind != 'count' or not card.href.startswith('/') or '?' not in card.href:
                        continue
                    lr = self.client.get(card.href)
                    if lr.status_code == 200 and 'total' in lr.context:
                        self.assertEqual(lr.context['total'], card.value, f'{who} {name} {card.key}')

    def test_money_card_matches_list_sum(self):
        inv = self.make_invoice()
        self.login('owner')
        r = self.client.get(reverse('crm:dashboard_finance'))
        card = next(c for c in r.context['cards'] if c.key == 'outstanding')
        self.assertEqual(card.value, inv.balance)
        self.assertEqual(r.context['ageing']['total'], inv.balance)

    def test_sales_dashboard_scope_is_own(self):
        self.login('sales')
        r = self.client.get(reverse('crm:dashboard_sales'))
        overdue = next(c for c in r.context['cards'] if c.key == 'overdue')
        self.assertEqual(overdue.value, 1)
        leads_in = next(c for c in r.context['cards'] if c.key == 'leads_in')
        self.assertEqual(leads_in.value, 2)  # not the colleague's lead

    def test_configure_hides_and_orders_cards(self):
        self.login('owner')
        r = self.client.get(reverse('crm:dashboard_owner'))
        keys = [c.key for c in r.context['all_cards']]
        order = list(reversed(keys))
        self.client.post(reverse('crm:dashboard_configure', args=['owner']), {'order': order, 'show': order[1:]})
        r = self.client.get(reverse('crm:dashboard_owner'))
        shown = [c.key for c in r.context['cards']]
        self.assertNotIn(order[0], shown)
        self.assertEqual(shown, order[1:])
        self.client.post(reverse('crm:dashboard_configure', args=['owner']), {'reset': '1'})
        r = self.client.get(reverse('crm:dashboard_owner'))
        self.assertEqual([c.key for c in r.context['cards']], keys)

    def test_dashboard_export_is_logged(self):
        from crm.models import ExportLog
        self.login('owner')
        r = self.client.get(reverse('crm:dashboard_owner') + '?export=csv')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(ExportLog.objects.filter(module='dashboard:owner').exists())
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:dashboard_sales') + '?export=csv').status_code, 403)

    def test_every_report_renders_and_exports(self):
        from crm.models import ExportLog
        self.make_invoice()
        self.login('owner')
        self.assertEqual(self.client.get(reverse('crm:report_index')).status_code, 200)
        for r in catalogue.REPORTS:
            url = reverse('crm:report_view', args=[r.slug])
            resp = self.client.get(url + '?from=2020-01-01')
            self.assertIn(resp.status_code, (200, 302), r.slug)
            if r.func is not None:
                resp = self.client.get(url + '?from=2020-01-01&export=csv')
                self.assertEqual(resp.status_code, 200, r.slug)
                self.assertTrue(ExportLog.objects.filter(module=f'report:{r.slug}').exists())
        for by in ('client', 'project', 'service'):
            self.assertEqual(self.client.get(reverse('crm:report_view', args=['profitability']) + f'?by={by}').status_code, 200)

    def test_reports_visible_by_role(self):
        for key in self.u:
            self.login(key)
            r = self.client.get(reverse('crm:report_index'))
            self.assertIn(r.status_code, (200, 403))
            for rep in catalogue.visible(self.u[key]):
                if rep.func is None:
                    continue
                resp = self.client.get(reverse('crm:report_view', args=[rep.slug]))
                self.assertEqual(resp.status_code, 200, f'{key} {rep.slug}')
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:report_view', args=['payroll_cost'])).status_code, 404)

    def test_report_scope_for_executive(self):
        p = catalogue.params_from({'from': (india.today() - timedelta(days=5)).isoformat()})
        t = catalogue.funnel(self.u['sales'], p)
        self.assertEqual(t.rows[0]['cells'][1], 2)
        t = catalogue.funnel(self.u['owner'], p)
        self.assertEqual(t.rows[0]['cells'][1], 3)

    def test_scheduled_report_stays_in_managers_scope(self):
        from crm.models import Lead, ReportSchedule, Team
        outsider_team = Team.objects.create(name='Other team')
        other = self._lead(self.u['owner'], 'Outside the team')
        self.u['owner'].crm_profile.team = outsider_team
        self.u['owner'].crm_profile.save()
        ReportSchedule.objects.create(user=self.u['manager'], report='response', querystring='', frequency='daily', hour=0)
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            sent = catalogue.run_schedules()
        self.assertEqual(sent, 1)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].attachments[0][1]
        body = body.decode('utf-8') if isinstance(body, bytes) else body
        self.assertIn('Sales', body)
        self.assertNotIn('Owner', body.split('\n', 2)[2])
        self.assertTrue(Lead.objects.filter(pk=other.pk).exists())
        # Same day: not sent twice.
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(catalogue.run_schedules(), 0)

    def test_schedule_create_and_delete(self):
        from crm.models import ReportSchedule
        self.login('manager')
        r = self.client.post(reverse('crm:report_schedule', args=['funnel']), {'frequency': 'weekly', 'hour': '9', 'querystring': 'from=2026-01-01'})
        self.assertEqual(r.status_code, 302)
        s = ReportSchedule.objects.get(user=self.u['manager'])
        self.login('sales')
        self.assertEqual(self.client.post(reverse('crm:report_schedule_delete', args=[s.pk])).status_code, 404)
        self.login('manager')
        self.client.post(reverse('crm:report_schedule_delete', args=[s.pk]))
        self.assertFalse(ReportSchedule.objects.exists())

    def test_csv_blocks_formula_injection(self):
        self.assertEqual(catalogue._safe('=HYPERLINK("x")'), '\'=HYPERLINK("x")')
        self.assertEqual(catalogue._safe('-12.50'), '-12.50')
        self.assertEqual(catalogue._safe('-cmd'), "'-cmd")

    def test_overdue_approvals_filter(self):
        from crm.core import approvals
        lead = self._lead(self.u['sales'], 'Big lost lead', value=Decimal('500000'))
        req = approvals.submit('lead_lost', lead, title='Mark lost', module='leads', requested_by=self.u['sales'], level='manager')
        self.login('manager')
        r = self.client.get(reverse('crm:approval_list') + '?overdue=1')
        self.assertEqual(len(r.context['page_obj'].object_list), 0)
        type(req).objects.filter(pk=req.pk).update(requested_at=timezone.now() - timedelta(hours=30))
        r = self.client.get(reverse('crm:approval_list') + '?overdue=1')
        self.assertEqual(len(r.context['page_obj'].object_list), 1)
