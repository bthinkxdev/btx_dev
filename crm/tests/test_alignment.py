"""The whole business flow lined up: one credit rule, nothing orphaned when someone leaves, every login has an HR record,
no customer forgotten, and operational checks that name any gap."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.core import attribution, india, integrity
from crm.models import (AuditLog, Client, CommissionRule, Contract, ContractLine, Deliverable, DeliveryPlan, Employee, Lead, LeadSource,
                        Notification, Payment, PaymentLink, Quotation, SalesTarget, Task, UserProfile)
from crm.modules.clients import health
from crm.modules.contracts import services as cs
from crm.modules.deliverables import services as ds
from crm.modules.hr import handover, services as hr
from crm.modules.leads import services as leads
from crm.modules.payroll import commissions
from crm.modules.performance import jobs, metrics

from .base import CrmTestCase
from .test_deliverables import DeliverableCase


def lead_for(case, owner, stage='negotiation', value='40000', **kw):
    data = dict(name='Flow lead', phone='+919800000222', source=LeadSource.objects.first(), owner=owner, stage=stage, created_by=owner,
                assigned_at=timezone.now(), value=Decimal(value), next_follow_up_at=timezone.now() + timedelta(days=2))
    data.update(kw)
    return Lead.objects.create(**data)


# ====================================================================== one credit rule

class CreditAlignmentTests(CrmTestCase):
    def setUp(self):
        super().setUp()
        self.s, self.m = self.u['sales'], self.u['manager']
        UserProfile.objects.filter(user__in=[self.s, self.m]).update(receives_leads=True)
        self.today = india.today()
        self.lead = lead_for(self, self.s)
        leads.mark_won(self.lead, self.m, won_value=Decimal('50000'), note='Confirmed by phone on a call today', credit_to='me')
        self.lead.refresh_from_db()
        self.client_won = Client.objects.get(source_lead=self.lead) if Client.objects.filter(source_lead=self.lead).exists() else \
            Client.objects.create(legal_name='Won Co', display_name='Won Co', owner=self.s, source_lead=self.lead, billing_state='32')
        self.pay = Payment.objects.create(client=self.client_won, amount=Decimal('20000'), payment_date=self.today, status='verified')

    def test_the_credit_is_the_closers_choice_everywhere(self):
        self.assertEqual(attribution.payment_credit_id(self.pay), self.m.pk)
        self.assertEqual(attribution.collected_total([self.m.pk], self.today, self.today), Decimal('20000'))
        self.assertEqual(attribution.collected_total([self.s.pk], self.today, self.today), Decimal('0'))

    def test_the_targets_page_my_day_and_the_profile_agree(self):
        month = self.today.replace(day=1)
        SalesTarget.objects.create(month=month, user=self.m, won_value=Decimal('100000'), collected_value=Decimal('40000'))
        self.login('manager')
        r = self.client.get(reverse('crm:targets'), {'month': month.strftime('%Y-%m')})
        rows = {x['user'].pk: x for x in r.context['rows']}
        self.assertEqual((rows[self.m.pk]['won'], rows[self.m.pk]['collected']), (Decimal('50000'), Decimal('20000')))
        self.assertEqual((rows[self.s.pk]['won'], rows[self.s.pk]['collected']), (Decimal('0'), Decimal('0')))
        period = metrics.Period('month', 'x', month, self.today)
        prof = metrics.compute([self.m.pk], period)
        self.assertEqual((prof['won_value'], prof['collected']), (Decimal('50000'), Decimal('20000')))
        my_day = self.client.get(reverse('crm:my_day'))
        self.assertEqual(my_day.context['collected'], Decimal('20000'))

    def test_commission_goes_to_the_credited_person(self):
        rule_w = CommissionRule.objects.create(name='Won 5%', basis='won', rate_percent=Decimal('5'), effective_from=date(2025, 1, 1))
        rule_c = CommissionRule.objects.create(name='Collected 10%', basis='collected', rate_percent=Decimal('10'), effective_from=date(2025, 1, 1))
        commissions.accrue('won', lead=self.lead)
        commissions.accrue('collected', payment=self.pay)
        users = {e.rule_id: e.user for e in rule_w.entries.all() | rule_c.entries.all()}
        self.assertEqual(users.get(rule_w.pk), self.m)
        self.assertEqual(users.get(rule_c.pk), self.m)

    def test_a_hand_made_customer_is_credited_to_its_owner(self):
        c = Client.objects.create(legal_name='Hand Co', display_name='Hand Co', owner=self.s, billing_state='32')
        p = Payment.objects.create(client=c, amount=Decimal('7000'), payment_date=self.today, status='verified')
        self.assertEqual(attribution.payment_credit_id(p), self.s.pk)

    def test_refunded_in_full_or_unverified_money_is_not_credited(self):
        Payment.objects.create(client=self.client_won, amount=Decimal('9000'), payment_date=self.today, status='pending')
        Payment.objects.create(client=self.client_won, amount=Decimal('8000'), payment_date=self.today, status='refunded')
        self.assertEqual(attribution.collected_total([self.m.pk], self.today, self.today), Decimal('20000'))


# ====================================================================== leaving

class HandoverTests(DeliverableCase):
    def setUp(self):
        super().setUp()
        self.plan = self.plan_for()
        Contract.objects.filter(pk=self.plan.contract_id).update(owner=self.u['sales'])      # the salesperson owns this contract
        self.sales_emp = Employee.objects.create(user=self.u['sales'], code='E1', name='Sales', work_email='sales@e.test', joining_date=date(2026, 1, 5),
                                                 status='on_notice', last_working_day=india.today())
        self.mgr_emp = Employee.objects.create(user=self.u['manager'], code='E2', name='Manager', work_email='mgr@e.test', joining_date=date(2025, 1, 5),
                                               status='active')
        self.sales_emp.manager = self.mgr_emp
        self.sales_emp.save()

    def plan_for(self):
        return DeliveryPlan.objects.get(contract=self.contract())

    def test_everything_accountable_moves_to_the_manager(self):
        s, m = self.u['sales'], self.u['manager']
        task_project = __import__('crm.modules.projects.services', fromlist=['x']).create_project(
            self.u['pm'], client=self.client_obj, name='Store', manager=s, start_date=india.today(), due_date=india.today() + timedelta(days=30))
        Task.objects.create(project=task_project, title='Build', assignee=s, due_date=india.today() + timedelta(days=3), created_by=self.u['pm'])
        before = hr.offboarding_preview(self.sales_emp)
        self.assertGreaterEqual(before['clients'], 1)
        self.assertGreaterEqual(before['contracts'], 1)
        self.assertGreaterEqual(before['deliverables'], 1)
        summary = hr.complete_exit(self.sales_emp, self.u['owner'])
        self.client_obj.refresh_from_db()
        self.plan.refresh_from_db()
        self.assertEqual(self.client_obj.owner, m)
        self.assertEqual(Contract.objects.get(pk=self.plan.contract_id).owner, m)
        self.assertEqual(self.plan.account_owner, m)
        self.assertFalse(Deliverable.objects.filter(assignee=s, status__in=['upcoming', 'in_progress']).exists())
        self.assertTrue(Deliverable.objects.filter(assignee=m).exists())
        self.assertFalse(Task.objects.exclude(status__in=['done', 'cancelled']).filter(assignee__isnull=True).exists())
        self.assertEqual(Task.objects.get(title='Build').assignee, m)
        task_project.refresh_from_db()
        self.assertEqual(task_project.manager, m)
        self.u['sales'].refresh_from_db()
        self.assertFalse(self.u['sales'].is_active)
        self.assertGreaterEqual(summary['accounts']['clients'], 1)
        self.assertTrue(AuditLog.objects.filter(action='employee.accounts_reassigned').exists())

    def test_with_no_manager_an_administrator_takes_over_so_nothing_is_left_unassigned(self):
        Employee.objects.filter(pk=self.sales_emp.pk).update(manager=None)
        self.sales_emp.refresh_from_db()
        to = handover.target_for(self.sales_emp)
        self.assertTrue(to is not None and to.is_active)
        hr.complete_exit(self.sales_emp, self.u['owner'])
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.owner, to)
        self.assertFalse(Task.objects.exclude(status__in=['done', 'cancelled']).filter(assignee__isnull=True).exists())

    def test_quotations_links_and_plan_items_move_too(self):
        s, m = self.u['sales'], self.u['manager']
        q = self.make_quotation(accepted=False)
        self.assertEqual((q.owner, q.status), (s, 'sent'))
        item = self.plan.items.first()
        item.assignee = s
        item.save()
        hr.complete_exit(self.sales_emp, self.u['owner'])
        q.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(q.owner, m)
        self.assertIsNone(item.assignee)                                              # the plan default is the new person now
        self.assertEqual(DeliveryPlan.objects.get(pk=self.plan.pk).assignee, m)

    def test_a_leaver_with_no_login_changes_nothing_and_does_not_crash(self):
        e = Employee.objects.create(code='E9', name='No Login', work_email='nl@e.test', joining_date=date(2026, 1, 5), status='on_notice',
                                    last_working_day=india.today())
        self.assertEqual(hr.complete_exit(e, self.u['owner'])['tasks'], 0)

    def test_the_offboarding_banner_lists_customers_and_contracts(self):
        self.login('hr')
        r = self.client.get(reverse('crm:employee_detail', args=[self.sales_emp.pk]))
        self.assertContains(r, 'customers')
        self.assertContains(r, 'contracts')


# ====================================================================== every login has an HR record

class HrLinkTests(CrmTestCase):
    def test_hr_sees_logins_without_a_record_and_can_link_one(self):
        self.login('hr')
        page = self.client.get(reverse('crm:employee_list'))
        names = {u.username for u in page.context['unlinked']}
        self.assertTrue({'sales', 'sales_b', 'manager'} <= names)
        self.assertNotIn('owner', names)
        self.assertContains(page, 'without an HR record')
        sales = self.u['sales']
        form = self.client.get(reverse('crm:employee_create'), {'user': sales.pk})
        self.assertContains(form, 'linked to the existing login')
        self.assertContains(form, sales.email)
        users = get_user_model().objects.count()
        r = self.client.post(reverse('crm:employee_create'), {
            'link_user': sales.pk, 'code': 'S100', 'name': 'Sales Person', 'work_email': 'sales.person@company.test', 'employment_type': 'full_time',
            'joining_date': '2026-01-05', 'pan': '', 'uan': ''})
        self.assertEqual(r.status_code, 302)
        emp = Employee.objects.get(code='S100')
        self.assertEqual(emp.user, sales)
        self.assertEqual(get_user_model().objects.count(), users)                       # no extra login was made
        self.assertNotIn('sales', {u.username for u in self.client.get(reverse('crm:employee_list')).context['unlinked']})

    def test_a_login_cannot_get_two_records(self):
        Employee.objects.create(user=self.u['sales'], code='E1', name='Sales', work_email='s@e.test', joining_date=date(2026, 1, 5))
        with self.assertRaises(hr.HRError):
            hr.create_employee(self.u['hr'], link_user=self.u['sales'], code='E2', name='Again', work_email='again@e.test', joining_date=date(2026, 1, 6))

    def test_only_those_who_can_create_employees_see_the_panel(self):
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:employee_list')).context['unlinked'], [])


# ====================================================================== no customer goes quiet

class QuietCustomerTests(DeliverableCase):
    def setUp(self):
        super().setUp()
        self.plan = DeliveryPlan.objects.get(contract=self.contract())
        Client.objects.filter(pk=self.client_obj.pk).update(created_at=timezone.now() - timedelta(days=45))
        self.client_obj.refresh_from_db()

    def test_a_live_customer_with_no_contact_for_a_month_is_quiet(self):
        self.assertIn(self.client_obj.pk, [c.pk for c, _ in health.quiet_clients()])
        self.assertGreaterEqual(dict((c.pk, d) for c, d in health.quiet_clients())[self.client_obj.pk], 30)

    def test_real_contact_resets_it(self):
        d = Deliverable.objects.filter(plan=self.plan).first()
        ds.add_note(d.pk, self.u['sales'], 'Called the client about the poster')
        self.assertNotIn(self.client_obj.pk, [c.pk for c, _ in health.quiet_clients()])

    def test_a_payment_or_a_lead_activity_also_counts(self):
        Payment.objects.create(client=self.client_obj, amount=Decimal('1000'), payment_date=india.today(), status='verified')
        self.assertNotIn(self.client_obj.pk, [c.pk for c, _ in health.quiet_clients()])

    def test_a_customer_we_are_not_working_for_is_not_chased(self):
        Contract.objects.filter(pk=self.plan.contract_id).update(status='terminated')
        DeliveryPlan.objects.filter(pk=self.plan.pk).update(status='closed')
        Deliverable.objects.filter(plan=self.plan).update(status='cancelled')
        self.assertNotIn(self.client_obj.pk, [c.pk for c, _ in health.quiet_clients()])

    def test_the_owner_and_manager_are_told_once_a_month(self):
        self.assertGreaterEqual(health.notify_quiet(), 1)
        self.assertEqual(health.notify_quiet(), 0)
        for who in ('sales', 'manager'):
            self.assertTrue(Notification.objects.filter(user=self.u[who], event_type='client_quiet').exists(), who)
        self.assertTrue(AuditLog.objects.filter(action='client.went_quiet').exists())

    def test_the_customer_page_says_so(self):
        self.login('sales')
        self.assertContains(self.client.get(reverse('crm:client_detail', args=[self.client_obj.pk])), 'No contact for')
        d = Deliverable.objects.filter(plan=self.plan).first()
        ds.add_note(d.pk, self.u['sales'], 'Spoke today')
        self.assertNotContains(self.client.get(reverse('crm:client_detail', args=[self.client_obj.pk])), 'No contact for')

    def test_the_owners_profile_flags_it(self):
        period = metrics.Period('month', 'x', india.today().replace(day=1), india.today())
        m = metrics.compute([self.u['sales'].pk], period)
        self.assertGreaterEqual(m['quiet_clients'], 1)
        self.assertTrue(any('no contact for 30+ days' in f['text'] for f in metrics.flags(m, period)))

    def test_the_job_is_scheduled(self):
        from crm.core import jobs as registry
        self.assertIn('clients.quiet_accounts', registry.JOBS)


# ====================================================================== the checks that name every gap

class OperationalChecksTests(DeliverableCase):
    def result(self, fn):
        return fn()

    def test_a_clean_system_passes_every_operational_check(self):
        for fn in integrity.OPERATIONAL:
            if fn in (integrity.check_hr_links,):
                continue                                                               # the fixtures have logins without HR records
            r = fn()
            self.assertTrue(r['ok'], f"{r['label']}: {r['detail']} {r['items']}")

    def test_an_open_lead_without_a_follow_up_is_named_and_fixed_by_the_job(self):
        lead = lead_for(self, self.u['sales'])
        r = integrity.check_leads_next_action()
        self.assertFalse(r['ok'])
        self.assertTrue(r['warn'])
        self.assertIn('Flow lead', r['items'][0])
        jobs.missing_next_action()
        self.assertTrue(integrity.check_leads_next_action()['ok'])
        self.assertIsNotNone(lead)

    def test_work_with_a_disabled_login_is_named(self):
        lead_for(self, self.u['sales_b'])
        get_user_model().objects.filter(pk=self.u['sales_b'].pk).update(is_active=False)
        self.assertFalse(integrity.check_leads_owned()['ok'])
        Client.objects.filter(pk=self.client_obj.pk).update(owner=self.u['sales_b'])
        self.assertFalse(integrity.check_clients_owned()['ok'])

    def test_a_monthly_contract_without_a_plan_or_billing_is_named(self):
        plan = DeliveryPlan.objects.get(contract=self.contract())
        self.assertTrue(integrity.check_contract_coverage()['ok'])
        c = plan.contract
        Deliverable.objects.filter(plan=plan).delete()
        plan.revisions.all().delete()
        plan.delete()
        r = integrity.check_contract_coverage()
        self.assertFalse(r['ok'])
        self.assertTrue(any('no monthly delivery plan' in i for i in r['items']))
        from crm.models import BillingSchedule
        BillingSchedule.objects.filter(contract_line__contract=c).delete()
        self.assertTrue(any('no future billing' in i for i in integrity.check_contract_coverage()['items']))

    def test_tasks_and_deliverables_without_a_person_are_named(self):
        plan = DeliveryPlan.objects.get(contract=self.contract())
        Deliverable.objects.filter(plan=plan).update(assignee=None)
        self.assertFalse(integrity.check_work_owned()['ok'])
        proj = __import__('crm.modules.projects.services', fromlist=['x']).create_project(
            self.u['pm'], client=self.client_obj, name='P', manager=self.u['pm'], start_date=india.today(), due_date=india.today() + timedelta(days=9))
        Task.objects.create(project=proj, title='Orphan', assignee=None, created_by=self.u['pm'])
        self.assertTrue(any('Orphan' in i for i in integrity.check_work_owned()['items']))

    def test_logins_without_an_hr_record_are_named_until_linked(self):
        r = integrity.check_hr_links()
        self.assertFalse(r['ok'])
        self.assertTrue(any('Sales' in i for i in r['items']))
        for key in ('sales', 'sales_b', 'manager', 'finance', 'finance2', 'accountant', 'hr', 'pm', 'dev', 'auditor'):
            Employee.objects.create(user=self.u[key], code=f'C-{key}', name=key, work_email=f'{key}@x.test', joining_date=date(2026, 1, 5))
        self.assertTrue(integrity.check_hr_links()['ok'])

    def test_a_win_without_a_customer_credit_or_start_is_named(self):
        lead_for(self, self.u['sales'], stage='won', won_at=timezone.now() - timedelta(days=20), won_value=Decimal('1000'), next_follow_up_at=None)
        r = integrity.check_won_flow()
        self.assertFalse(r['ok'])
        self.assertTrue(any('without a customer' in i for i in r['items']))
        self.assertTrue(any('nobody credited' in i for i in r['items']))

    def test_gaps_alert_the_owners_but_never_fail_the_nightly_job(self):
        lead_for(self, self.u['sales'])                                                 # an open lead with no follow-up
        result = integrity.nightly()                                                   # would raise if a money check failed
        self.assertEqual(result['failed'], 0)
        self.assertGreaterEqual(result['gaps'], 1)
        self.assertTrue(Notification.objects.filter(user=self.u['owner'], event_type='integrity', title__contains='operational gap').exists())

    def test_the_results_carry_their_severity_for_the_jobs_page(self):
        r = integrity.run_checks()
        kinds = {c['code']: c.get('warn') for c in r['checks']}
        self.assertFalse(kinds['journals'])
        self.assertTrue(kinds['leads_next_action'])
        self.assertEqual(len(r['checks']), len(integrity.CHECKS))


# ====================================================================== several contracts, one customer

class MultiContractTests(DeliverableCase):
    def test_one_customer_with_two_monthly_contracts_sees_both_plans_and_one_summary(self):
        c1 = self.contract()
        c2 = Contract.objects.create(client=self.client_obj, owner=self.u['manager'], title='Ads retainer', start_date=self.today,
                                     end_date=india.add_months(self.today, 12) - timedelta(days=1))
        ContractLine.objects.create(contract=c2, service=self.service, description='Ads', unit_price=Decimal('8000'), billing_model='monthly',
                                    start_date=self.today, sac_code='998365')
        c2 = cs.activate(c2, self.u['manager'])
        plans = DeliveryPlan.objects.filter(client=self.client_obj)
        self.assertEqual(plans.count(), 2)
        self.login('sales')
        r = self.client.get(reverse('crm:client_deliverables', args=[self.client_obj.pk]))
        self.assertEqual(len(list(r.context['plans'])), 2)
        self.assertContains(r, 'Plan: Social retainer')
        self.assertContains(r, 'Plan: Ads retainer')
        month = ds.month_start()
        self.assertEqual(ds.month_summary(self.client_obj, month)['promised'], sum(
            d.quantity for d in Deliverable.objects.filter(client=self.client_obj, period=month).exclude(status__in=['cancelled', 'skipped'])))
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c2)
