"""AI business staff: numbers, rules, suggested actions, analyst, plan upload, jobs and access."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from crm.core import india, llm
from crm.core.nav import build_nav
from crm.models import (BusinessGoal, BusinessPlan, DailySnapshot, DeskAction, DeskBriefing, FollowUp, Lead, LeadActivity,
                        LeadSource, Notification, Payment)
from crm.modules.desk import assistant, jobs, metrics, plan as plan_svc, rules

from .base import CrmTestCase


class DeskBase(CrmTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def lead(self, owner, name='Lead', *, created_by=None, stage='contacted', **kw):
        src = LeadSource.objects.first() or LeadSource.objects.create(name='Website', channel='website')
        n = Lead.objects.count()
        return Lead.objects.create(name=f'{name} {n}', phone=f'+9198770{n:05d}', source=src, owner=owner, stage=stage,
                                   value=kw.pop('value', Decimal('50000')), probability=40, created_by=created_by,
                                   next_follow_up_at=kw.pop('next_follow_up_at', timezone.now() + timedelta(days=1)), **kw)

    def pay(self, amount, when=None, ref=''):
        return Payment.objects.create(client=self.client_obj, amount=Decimal(amount), payment_date=when or india.today(),
                                      status='verified', method='bank_transfer', reference_number=ref)

    ai_url = property(lambda self: reverse('crm:dashboard_owner') + '?tab=ai')

    def overdue(self, inv, days):
        from crm.models import Invoice
        Invoice.objects.filter(pk=inv.pk).update(invoice_date=india.today() - timedelta(days=days + 10),
                                                 due_date=india.today() - timedelta(days=days))

    def ask_http(self, question, **extra):
        return self.client.post(reverse('crm:desk_ask'), json.dumps({'question': question, **extra}), content_type='application/json')


class AccessTests(DeskBase):
    def test_only_the_owner_can_open_it(self):
        self.login('owner')
        self.assertEqual(self.client.get(self.ai_url).status_code, 200)
        for who in ('manager', 'sales', 'finance', 'accountant', 'auditor', 'pm'):
            self.login(who)
            self.assertEqual(self.client.get(self.ai_url).status_code, 403, who)
            self.assertEqual(self.ask_http('hello').status_code, 403, who)
            self.assertEqual(self.client.get(reverse('crm:desk_goals')).status_code, 403, who)
            self.assertEqual(self.client.get(reverse('crm:desk_plan')).status_code, 403, who)

    def test_anonymous_is_sent_to_login(self):
        r = self.client.get(self.ai_url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r['Location'])

    def test_there_is_no_separate_menu_entry(self):
        def urls(user):
            return [c['href'] for g in build_nav(user, '') for it in g['items'] for c in it['children']]
        self.assertNotIn(self.ai_url, urls(self.u['owner']))
        self.assertIn(reverse('crm:dashboard_owner'), urls(self.u['owner']))

    def test_old_address_goes_to_the_owner_dashboard_tab(self):
        self.login('owner')
        self.assertRedirects(self.client.get('/crm/business-staff/'), self.ai_url, fetch_redirect_response=False)

    def test_every_page_renders_for_the_owner(self):
        self.login('owner')
        self.lead(self.u['sales'], created_by=self.u['sales'])
        self.pay('5000')
        for url in (self.ai_url, reverse('crm:desk_goals'), reverse('crm:desk_plan')):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get(self.ai_url)
        self.assertContains(r, 'Ask your business staff')
        self.assertContains(r, 'Sales team today')
        self.assertContains(r, 'aria-current="page">Business analytics AI')

    def test_owner_dashboard_has_exactly_two_tabs(self):
        self.login('owner')
        r = self.client.get(reverse('crm:dashboard_owner'))
        self.assertContains(r, 'aria-current="page">Overview')
        self.assertContains(r, '?tab=ai')
        self.assertNotContains(r, 'Business staff')


class MetricsTests(DeskBase):
    def test_today_numbers(self):
        today = india.today()
        l1 = self.lead(self.u['sales'], created_by=self.u['sales'])
        self.lead(self.u['sales_b'], created_by=self.u['sales_b'])
        won = self.lead(self.u['sales'], stage='won', won_value=Decimal('20000'), won_at=timezone.now())
        Lead.objects.filter(pk=won.pk).update(next_follow_up_at=None)
        self.pay('7500')
        self.pay('2500', ref='B2')
        self.pay('9999', when=today - timedelta(days=1), ref='Y1')
        LeadActivity.objects.create(lead=l1, kind='call', actor=self.u['sales'], outcome='connected', summary='x')
        n = metrics.day_numbers(today)
        self.assertEqual(n['leads_created'], 3)
        self.assertEqual(n['leads_won'], 1)
        self.assertEqual(n['won_value'], 20000.0)
        self.assertEqual(n['collected'], 10000.0)
        self.assertEqual(n['calls'], 1)
        self.assertEqual(metrics.day_numbers(today - timedelta(days=1))['collected'], 9999.0)

    def test_rejected_payment_is_not_counted(self):
        p = self.pay('5000')
        Payment.objects.filter(pk=p.pk).update(status='rejected')
        self.assertEqual(metrics.day_numbers(india.today())['collected'], 0)

    def test_compare_gives_change_against_yesterday(self):
        self.pay('2000', when=india.today() - timedelta(days=1), ref='Y')
        self.pay('3000')
        c = metrics.compare(india.today())['collected']
        self.assertEqual((c['today'], c['yesterday']), (3000.0, 2000.0))
        self.assertEqual(c['vs_yesterday'], 50.0)

    def test_change_handles_zero_baseline(self):
        self.assertIsNone(metrics._change(0, 0))
        self.assertEqual(metrics._change(5, 0), 100.0)

    def test_leads_created_by_person(self):
        self.lead(self.u['sales'], created_by=self.u['sales'])
        self.lead(self.u['sales'], created_by=self.u['sales'])
        self.lead(self.u['sales_b'], created_by=self.u['sales_b'])
        self.lead(self.u['sales'])   # website or automatic
        by = metrics.leads(india.today())['created_today_by_person']
        self.assertEqual(by['Sales'], 2)
        self.assertEqual(by['Sales_b'], 1)
        self.assertEqual(by['Website and automatic'], 1)

    def test_team_board_rates_people(self):
        for _ in range(5):
            fu_lead = self.lead(self.u['sales'])
            FollowUp.objects.create(lead=fu_lead, assignee=self.u['sales'], due_at=timezone.now() - timedelta(hours=5))
        rows = {r['name']: r for r in metrics.team(india.today())}
        self.assertEqual(rows['Sales']['overdue_followups'], 5)
        self.assertEqual(rows['Sales']['status'], 'red')
        self.assertIn('5 follow-ups overdue', rows['Sales']['reasons'])
        self.assertEqual(rows['Sales_b']['status'], 'green')

    def test_tomorrow_counts_followups(self):
        tomorrow = timezone.localtime() + timedelta(days=1)
        lead = self.lead(self.u['sales'])
        FollowUp.objects.create(lead=lead, assignee=self.u['sales'], due_at=tomorrow.replace(hour=11, minute=0))
        t = metrics.tomorrow(india.today())
        self.assertEqual(t['followups_due'], 1)
        self.assertEqual(t['followups_by_person'], {'Sales': 1})

    def test_new_client_and_lost_contract(self):
        self.assertEqual(metrics.customers(india.today())['onboarded_today'], ['Acme'])
        self.assertEqual(metrics.day_numbers(india.today())['clients_onboarded'], 1)

    def test_funnel_and_context_never_fail_on_an_empty_database(self):
        ctx = metrics.full_context(fresh=True)
        for key in ('numbers', 'compare', 'delivery', 'tomorrow', 'team', 'leads', 'revenue', 'customers', 'goals', 'funnel',
                    'stock', 'flags', 'weak_areas'):
            self.assertIn(key, ctx)
        json.dumps(ctx, default=str)


class GoalTests(DeskBase):
    def goal(self, metric, target):
        return BusinessGoal.objects.create(metric=metric, month=india.month_start(india.today()), target=Decimal(target))

    def test_progress_and_status(self):
        self.goal('revenue_collected', '100000')
        self.pay('100000')
        g = metrics.goal_progress(india.today())[0]
        self.assertEqual((g['actual'], g['pct'], g['status']), (100000.0, 100, 'ahead'))

    def test_behind_when_pace_is_too_slow(self):
        self.goal('new_leads', '1000')
        self.lead(self.u['sales'])
        g = metrics.goal_progress(india.today())[0]
        self.assertEqual(g['status'], 'behind')
        self.assertEqual(g['remaining'], 999)

    def test_win_rate_goal(self):
        self.goal('win_rate', '50')
        won = self.lead(self.u['sales'], stage='won', won_value=Decimal('1'), won_at=timezone.now())
        lost = self.lead(self.u['sales'], stage='lost', lost_at=timezone.now())
        Lead.objects.filter(pk__in=[won.pk, lost.pk]).update(next_follow_up_at=None)
        g = metrics.goal_progress(india.today())[0]
        self.assertEqual(g['actual'], 50.0)
        self.assertEqual(g['status'], 'ahead')

    def test_only_this_months_goals_count(self):
        BusinessGoal.objects.create(metric='new_leads', month=india.add_months(india.month_start(india.today()), 1), target=Decimal('5'))
        self.assertEqual(metrics.goal_progress(india.today()), [])

    def test_add_and_replace_a_goal_from_the_page(self):
        self.login('owner')
        url = reverse('crm:desk_goals')
        self.client.post(url, {'metric': 'new_leads', 'target': '40', 'note': 'first'})
        self.client.post(url, {'metric': 'new_leads', 'target': '60'})
        goals = BusinessGoal.objects.all()
        self.assertEqual(goals.count(), 1)
        self.assertEqual(goals[0].target, Decimal('60'))
        self.assertEqual(goals[0].created_by, self.u['owner'])

    def test_bad_goal_input_is_rejected(self):
        self.login('owner')
        url = reverse('crm:desk_goals')
        for body in ({'metric': 'nope', 'target': '5'}, {'metric': 'new_leads', 'target': '-3'},
                     {'metric': 'new_leads', 'target': 'abc'}, {'metric': 'win_rate', 'target': '150'}):
            self.client.post(url, body)
        self.assertEqual(BusinessGoal.objects.count(), 0)

    def test_delete_goal(self):
        self.login('owner')
        g = self.goal('new_leads', '10')
        self.client.post(reverse('crm:desk_goal_delete', args=[g.pk]))
        self.assertFalse(BusinessGoal.objects.exists())

    def test_flags_mention_a_goal_that_is_behind(self):
        self.goal('new_leads', '1000')
        ctx = metrics.full_context(fresh=True)
        self.assertTrue(any('New leads' in f['text'] or 'new leads' in f['text'].lower() for f in ctx['flags']))
        self.assertTrue(any(w['area'] == 'Lead volume' for w in ctx['weak_areas']))


class ActionTests(DeskBase):
    def stale_leads(self, n=4):
        old = timezone.now() - timedelta(days=12)
        out = []
        for _ in range(n):
            lead = self.lead(self.u['sales'])
            Lead.objects.filter(pk=lead.pk).update(last_activity_at=old)
            out.append(lead)
        return out

    def test_stale_leads_become_follow_up_suggestions(self):
        self.stale_leads(4)
        made = rules.suggest_actions(metrics.full_context(fresh=True))
        self.assertEqual(made, 4)
        a = DeskAction.objects.filter(kind='follow_up').first()
        self.assertEqual(a.status, 'suggested')
        self.assertEqual(a.assignee, self.u['sales'])
        self.assertFalse(FollowUp.objects.exists(), 'nothing may be created before approval')

    def test_suggesting_twice_in_a_day_adds_nothing_new(self):
        self.stale_leads(4)
        ctx = metrics.full_context(fresh=True)
        rules.suggest_actions(ctx)
        self.assertEqual(rules.suggest_actions(ctx), 0)
        self.assertEqual(DeskAction.objects.count(), 4)

    def test_approve_books_the_follow_up(self):
        self.stale_leads(3)
        rules.suggest_actions(metrics.full_context(fresh=True))
        a = DeskAction.objects.filter(kind='follow_up').first()
        self.login('owner')
        r = self.client.post(reverse('crm:desk_action', args=[a.pk, 'approve']))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()['ok'])
        a.refresh_from_db()
        self.assertEqual(a.status, 'approved')
        self.assertEqual(a.decided_by, self.u['owner'])
        fu = FollowUp.objects.get(lead=a.lead, status='pending', purpose__startswith='Suggested')
        self.assertEqual(fu.assignee, self.u['sales'])

    def test_dismiss_creates_nothing_and_cannot_be_repeated(self):
        self.stale_leads(3)
        rules.suggest_actions(metrics.full_context(fresh=True))
        a = DeskAction.objects.first()
        self.login('owner')
        self.assertEqual(self.client.post(reverse('crm:desk_action', args=[a.pk, 'dismiss'])).json()['status'], 'dismissed')
        self.assertEqual(self.client.post(reverse('crm:desk_action', args=[a.pk, 'approve'])).status_code, 409)
        self.assertFalse(FollowUp.objects.filter(purpose__startswith='Suggested').exists())

    def test_nudge_notifies_the_person(self):
        for _ in range(5):
            FollowUp.objects.create(lead=self.lead(self.u['sales']), assignee=self.u['sales'], due_at=timezone.now() - timedelta(hours=3))
        rules.suggest_actions(metrics.full_context(fresh=True))
        a = DeskAction.objects.get(kind='nudge', source_key__startswith='overdue-fu')
        self.login('owner')
        self.client.post(reverse('crm:desk_action', args=[a.pk, 'approve']))
        n = Notification.objects.get(user=self.u['sales'], event_type='desk_action')
        self.assertIn('overdue follow-ups', n.title)

    def test_approving_a_closed_lead_fails_cleanly(self):
        lead = self.stale_leads(1)[0]
        DeskAction.objects.create(kind='follow_up', title='t', lead=lead, assignee=self.u['sales'], for_date=india.today(), source_key='x1')
        Lead.objects.filter(pk=lead.pk).update(stage='lost', next_follow_up_at=None)
        self.login('owner')
        r = self.client.post(reverse('crm:desk_action', args=[DeskAction.objects.get().pk, 'approve']))
        self.assertFalse(r.json()['ok'])
        self.assertEqual(DeskAction.objects.get().status, 'failed')

    def test_only_those_who_may_approve_can(self):
        a = DeskAction.objects.create(kind='nudge', title='t', assignee=self.u['sales'], for_date=india.today(), source_key='x2')
        self.login('sales')
        self.assertEqual(self.client.post(reverse('crm:desk_action', args=[a.pk, 'approve'])).status_code, 403)
        a.refresh_from_db()
        self.assertEqual(a.status, 'suggested')

    def test_get_is_not_allowed_on_actions(self):
        a = DeskAction.objects.create(kind='nudge', title='t', assignee=self.u['sales'], for_date=india.today(), source_key='x3')
        self.login('owner')
        self.assertEqual(self.client.get(reverse('crm:desk_action', args=[a.pk, 'approve'])).status_code, 405)

    def test_unassigned_leads_go_to_the_sales_manager(self):
        src = LeadSource.objects.first()
        Lead.objects.create(name='Nobody', phone='+919877700001', source=src, stage='new', value=Decimal('1'),
                            next_follow_up_at=timezone.now() + timedelta(hours=2))
        rules.suggest_actions(metrics.full_context(fresh=True))
        self.assertEqual(DeskAction.objects.get(source_key__startswith='unassigned').assignee, self.u['manager'])

    def test_dashboard_fills_suggestions_on_first_visit(self):
        self.stale_leads(3)
        self.login('owner')
        r = self.client.get(self.ai_url)
        self.assertEqual(len(r.context['actions']), 3)
        self.assertContains(r, 'Approve')


class RouteTests(DeskBase):
    def test_routing(self):
        cases = {
            'Who is delayed and who is going well?': 'team',
            'What should I do today?': 'todo',
            'what is planned for tomorrow': 'tomorrow',
            'Which customers might we lose?': 'customers',
            'How much revenue did we collect': 'revenue',
            'Compare today with yesterday': 'compare',
            'Plan for next month': 'roadmap',
            'what are our weak areas against the plan': 'plan_gap',
            'how many leads came from each source': 'leads',
            'how many items are pending': 'delivery',
        }
        for q, intent in cases.items():
            self.assertIn(intent, assistant.route(q), q)
        self.assertEqual(assistant.route('hello there'), ['overview'])

    def test_aliases_round_trip(self):
        al = assistant.Aliases()
        self.assertEqual(al.tag('Acme Kerala'), 'C1')
        self.assertEqual(al.tag('Acme Kerala'), 'C1')
        self.assertEqual(al.tag('Zen'), 'C2')
        self.assertEqual(al.restore('C1 owes money, C2 is fine, ABC1 stays'), 'Acme Kerala owes money, Zen is fine, ABC1 stays')


class AnalystTests(DeskBase):
    def setUp(self):
        super().setUp()
        self.login('owner')

    def test_answers_without_ai(self):
        self.pay('4000')
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            d = self.ask_http('How are we doing today?').json()
        self.assertTrue(d['ok'])
        self.assertEqual(d['engine'], 'rules')
        self.assertIn('Rs 4,000', d['answer'])
        self.assertTrue(d['evidence'])

    def test_team_answer_names_the_delayed_person(self):
        for _ in range(5):
            FollowUp.objects.create(lead=self.lead(self.u['sales']), assignee=self.u['sales'], due_at=timezone.now() - timedelta(hours=3))
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            d = self.ask_http('who is delayed?').json()
        self.assertIn('Sales', d['answer'])
        self.assertIn('Delayed', d['answer'])

    def test_model_gets_aliases_and_never_client_names(self):
        sent = {}

        def fake(messages, **kw):
            sent['text'] = json.dumps(messages)
            return 'C1 is at risk because its invoice is unpaid.', 'test-model'
        inv = self.make_invoice()
        self.overdue(inv, 45)
        with mock.patch('crm.core.llm.complete', side_effect=fake):
            d = self.ask_http('which customers might we lose?').json()
        self.assertNotIn('Acme', sent['text'])
        self.assertNotIn('acme@example.test', sent['text'])
        self.assertIn('C1', sent['text'])
        self.assertTrue(d['answer'].startswith('Acme is at risk'), d['answer'])
        self.assertEqual(d['engine'], 'test-model')

    def test_suggestions_come_back_with_actionable_questions(self):
        old = timezone.now() - timedelta(days=12)
        for _ in range(3):
            Lead.objects.filter(pk=self.lead(self.u['sales']).pk).update(last_activity_at=old)
        with mock.patch('crm.core.llm.complete', return_value=('Do these.', 'test-model')):
            d = self.ask_http('what should I do today?').json()
        self.assertEqual(len(d['suggestions']), 3)
        self.assertEqual(d['suggestions'][0]['kind'], 'follow_up')

    def test_blank_question_and_bad_body_are_handled(self):
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            self.assertTrue(self.ask_http('').json()['ok'])
        r = self.client.post(reverse('crm:desk_ask'), 'not json', content_type='application/json')
        self.assertEqual(r.status_code, 200)

    def test_history_is_passed_and_capped(self):
        captured = {}

        def fake(messages, **kw):
            captured['n'] = len(messages)
            return 'ok', 'm'
        history = [{'role': 'user' if i % 2 == 0 else 'bot', 'text': 'x' * 3000} for i in range(30)]
        with mock.patch('crm.core.llm.complete', side_effect=fake):
            self.ask_http('and yesterday?', history=history)
        self.assertEqual(captured['n'], 1 + 6 + 1)

    def test_roadmap_answer_without_ai(self):
        self.pay('90000')
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            d = self.ask_http('plan for next month').json()
        self.assertIn('Plan for', d['answer'])
        self.assertIn('Targets', d['answer'])

    def test_a_failure_inside_gives_a_clean_error(self):
        with mock.patch('crm.modules.desk.assistant.ask', side_effect=RuntimeError('boom')):
            r = self.ask_http('anything')
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.json()['ok'])

    def test_plan_context_reaches_the_model(self):
        BusinessPlan.objects.create(title='Plan', summary='We run a digital agency.', context_notes='Grow retainers', is_active=True)
        seen = {}

        def fake(messages, **kw):
            seen['system'] = messages[0]['content']
            return 'ok', 'm'
        with mock.patch('crm.core.llm.complete', side_effect=fake):
            self.ask_http('what are our weak areas?')
        self.assertIn('Grow retainers', seen['system'])


class PlanTests(DeskBase):
    TEXT = ('Our plan for the year.\nTarget revenue of Rs 12,00,000 per year.\nWe want 60 leads every month.\n'
            'Win rate target 25%.\nOnboard 4 new clients each month.')

    def upload(self, name='plan.txt', data=None, title='FY plan'):
        data = self.TEXT.encode() if data is None else data
        return self.client.post(reverse('crm:desk_plan'), {'title': title, 'file': SimpleUploadedFile(name, data)})

    def test_text_is_read_and_goals_found_by_rules(self):
        self.login('owner')
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            r = self.upload()
        plan = BusinessPlan.objects.get()
        self.assertRedirects(r, reverse('crm:desk_plan_review', args=[plan.pk]))
        found = {g['metric']: g['target'] for g in plan.proposed}
        self.assertEqual(found['revenue_collected'], 100000.0)     # yearly 12 lakh -> monthly
        self.assertEqual(found['new_leads'], 60.0)
        self.assertEqual(found['win_rate'], 25.0)
        self.assertEqual(plan.proposed_by, 'rules')
        self.assertEqual(plan.status, 'proposed')
        self.assertFalse(BusinessGoal.objects.exists(), 'a proposal must not become a goal before review')

    def test_ai_proposal_is_parsed_and_cleaned(self):
        self.login('owner')
        reply = ('Sure! {"summary": "A digital agency.", "goals": [{"metric": "new_leads", "target": "80", "note": "page 2"}, '
                 '{"metric": "made_up", "target": 5}, {"metric": "won_value", "target": -1}, {"metric": "new_leads", "target": 99}], '
                 '"priorities": "Grow retainers"}')
        with mock.patch('crm.core.llm.complete', return_value=(reply, 'test-model')):
            self.upload()
        plan = BusinessPlan.objects.get()
        self.assertEqual(plan.proposed, [{'metric': 'new_leads', 'target': 80.0, 'note': 'page 2'}])
        self.assertEqual(plan.summary, 'A digital agency.')
        self.assertEqual(plan.context_notes, 'Grow retainers')
        self.assertEqual(plan.proposed_by, 'test-model')

    def test_garbage_ai_reply_falls_back_to_rules(self):
        self.login('owner')
        with mock.patch('crm.core.llm.complete', return_value=('I cannot help with that', 'm')):
            self.upload()
        self.assertTrue(BusinessPlan.objects.get().proposed)

    def test_review_saves_exactly_what_the_owner_edited(self):
        self.login('owner')
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            self.upload()
        plan = BusinessPlan.objects.get()
        month = india.month_start(india.today())
        body = {'context_notes': 'Focus on retainers'}
        for i, g in enumerate(plan.proposed):
            body.update({f'metric_{i}': g['metric'], f'month_{i}': f'{month:%Y-%m}', f'note_{i}': g['note'],
                         f'target_{i}': '77' if g['metric'] == 'new_leads' else str(g['target'])})
            if g['metric'] != 'win_rate':
                body[f'keep_{i}'] = '1'
        r = self.client.post(reverse('crm:desk_plan_review', args=[plan.pk]), body)
        self.assertRedirects(r, self.ai_url)
        goals = {g.metric: g for g in BusinessGoal.objects.all()}
        self.assertEqual(goals['new_leads'].target, Decimal('77'))
        self.assertNotIn('win_rate', goals, 'an unticked goal must not be saved')
        self.assertEqual(goals['new_leads'].source, 'plan')
        plan.refresh_from_db()
        self.assertTrue(plan.is_active)
        self.assertEqual(plan.status, 'confirmed')
        self.assertEqual(plan_svc.context_text().count('Focus on retainers'), 1)

    def test_only_one_plan_is_active(self):
        self.login('owner')
        for title in ('One', 'Two'):
            with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
                self.upload(title=title)
        for plan in BusinessPlan.objects.order_by('id'):
            self.client.post(reverse('crm:desk_plan_review', args=[plan.pk]), {'context_notes': plan.title})
        self.assertEqual(BusinessPlan.objects.filter(is_active=True).count(), 1)
        self.assertEqual(BusinessPlan.objects.get(is_active=True).title, 'Two')

    def test_bad_target_is_refused_without_saving(self):
        self.login('owner')
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            self.upload()
        plan = BusinessPlan.objects.get()
        r = self.client.post(reverse('crm:desk_plan_review', args=[plan.pk]),
                             {'metric_0': 'new_leads', 'keep_0': '1', 'target_0': 'lots', 'month_0': '2026-11'})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(BusinessGoal.objects.exists())

    def test_upload_validation(self):
        self.login('owner')
        self.upload(name='virus.exe', data=b'MZ' + b'x' * 100)
        self.upload(name='empty.txt', data=b'hi')
        self.upload(name='big.txt', data=b'a' * (plan_svc.MAX_BYTES + 10))
        r = self.client.post(reverse('crm:desk_plan'), {'title': 'none'})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(BusinessPlan.objects.exists())

    def test_docx_and_xlsx_are_readable(self):
        xml = '<w:document><w:body><w:p><w:r><w:t>We want 90 leads every month.</w:t></w:r></w:p></w:body></w:document>'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('word/document.xml', xml)
        self.assertIn('90 leads', plan_svc.extract_text('plan.docx', buf.getvalue()))
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.append(['Metric', 'Target'])
        wb.active.append(['New leads per month', 120])
        out = io.BytesIO()
        wb.save(out)
        self.assertIn('New leads per month | 120', plan_svc.extract_text('t.xlsx', out.getvalue()))

    def test_corrupt_files_give_a_clear_error(self):
        for name in ('x.docx', 'x.xlsx', 'x.pdf'):
            with self.assertRaises(plan_svc.PlanError):
                plan_svc.extract_text(name, b'this is not a real file at all, just text')

    def test_amount_units(self):
        found = {g['metric']: g['target'] for g in plan_svc.rule_goals('Monthly revenue target 5 lakh\nNew leads 50k per month')}
        self.assertEqual(found['revenue_collected'], 500000.0)

    def test_deleting_a_plan_keeps_its_goals(self):
        self.login('owner')
        plan = BusinessPlan.objects.create(title='P', extracted_text='x' * 30)
        BusinessGoal.objects.create(metric='new_leads', month=india.month_start(india.today()), target=Decimal('10'), plan=plan, source='plan')
        self.client.post(reverse('crm:desk_plan_delete', args=[plan.pk]))
        self.assertFalse(BusinessPlan.objects.exists())
        self.assertEqual(BusinessGoal.objects.get().plan, None)

    def test_a_manager_cannot_upload(self):
        self.login('manager')
        self.assertEqual(self.upload().status_code, 403)


class RoadmapAndWeakAreaTests(DeskBase):
    def test_roadmap_has_targets_and_per_person_split(self):
        self.pay('90000')
        ctx = metrics.full_context(fresh=True)
        r = rules.roadmap(ctx, india.today())
        self.assertEqual(len(r['targets']), 3)
        self.assertTrue(r['per_person'])
        self.assertTrue(r['first_week'])

    def test_collections_flagged_when_most_is_overdue(self):
        inv = self.make_invoice()
        self.overdue(inv, 40)
        ctx = metrics.full_context(fresh=True)
        self.assertTrue(any(w['area'] == 'Collections' for w in ctx['weak_areas']))
        self.assertTrue(any(f['area'] == 'money' and f['level'] == 'red' for f in ctx['flags']))
        self.assertTrue(ctx['customers']['at_risk'])

    def test_weak_areas_are_ranked_by_severity(self):
        BusinessGoal.objects.create(metric='new_leads', month=india.month_start(india.today()), target=Decimal('1000'))
        for _ in range(12):
            FollowUp.objects.create(lead=self.lead(self.u['sales']), assignee=self.u['sales'], due_at=timezone.now() - timedelta(days=2))
        ctx = metrics.full_context(fresh=True)
        sev = [w['severity'] for w in ctx['weak_areas']]
        self.assertEqual(sev, sorted(sev, reverse=True))
        self.assertGreaterEqual(len(sev), 2)


class JobTests(DeskBase):
    def test_snapshot_job_stores_and_refreshes_the_day(self):
        self.pay('1000')
        self.assertEqual(jobs.snapshot(), 2)       # today and the missing yesterday
        snap = DailySnapshot.objects.get(date=india.today())
        self.assertEqual(snap.data['numbers']['collected'], 1000.0)
        self.assertIn('stock', snap.data)
        self.pay('500', ref='R2')
        cache.clear()
        self.assertEqual(jobs.snapshot(), 1)
        self.assertEqual(DailySnapshot.objects.count(), 2)
        self.assertEqual(DailySnapshot.objects.get(date=india.today()).data['numbers']['collected'], 1500.0)

    def at_hour(self, hour):
        real = timezone.localtime()
        return mock.patch('crm.modules.desk.jobs.timezone.localtime', return_value=real.replace(hour=hour))

    def test_morning_briefing_once_a_day_and_not_before_eight(self):
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            with self.at_hour(6):
                self.assertEqual(jobs.morning(), 0)
            with self.at_hour(9):
                self.assertGreaterEqual(jobs.morning(), 1)
                self.assertEqual(jobs.morning(), 0)
        self.assertEqual(DeskBriefing.objects.filter(kind='morning').count(), 1)
        self.assertTrue(Notification.objects.filter(user=self.u['owner'], event_type='desk_briefing').exists())
        self.assertFalse(Notification.objects.filter(user=self.u['sales'], event_type='desk_briefing').exists())

    def test_evening_report_is_emailed(self):
        from django.core import mail
        with mock.patch('crm.core.llm.complete', return_value=('**Today** went well.', 'test-model')):
            with self.at_hour(7):
                self.assertEqual(jobs.evening(), 0)
            with self.captureOnCommitCallbacks(execute=True):
                with self.at_hour(20):
                    jobs.evening()
        b = DeskBriefing.objects.get(kind='evening')
        self.assertEqual(b.engine, 'test-model')
        self.assertTrue(any('evening business report' in m.subject.lower() for m in mail.outbox))

    def test_jobs_are_registered_and_run_through_the_runner(self):
        from crm.core.jobs import JOBS, run_job
        for name in ('desk.snapshot', 'desk.morning', 'desk.evening', 'desk.suggestions'):
            self.assertIn(name, JOBS)
        self.assertEqual(run_job('desk.snapshot')['status'], 'succeeded')
        self.assertEqual(run_job('desk.suggestions')['status'], 'succeeded')

    def test_manual_briefing_button_and_double_click_guard(self):
        self.login('owner')
        with mock.patch('crm.core.llm.complete', side_effect=llm.LLMUnavailable('off')):
            self.client.post(reverse('crm:desk_briefing_generate'))
            self.client.post(reverse('crm:desk_briefing_generate'))
        self.assertEqual(DeskBriefing.objects.filter(kind='manual').count(), 1)
        self.assertContains(self.client.get(self.ai_url), 'On request briefing')

    def test_briefing_uses_ai_text_with_names_restored(self):
        inv = self.make_invoice()
        self.overdue(inv, 45)
        with mock.patch('crm.core.llm.complete', return_value=('Watch C1 closely.', 'm')):
            b = assistant.generate_briefing('manual')
        self.assertEqual(b.text, 'Watch Acme closely.')
