"""Performance profiles: periods, working days, who gets credit, every metric, access, the safety-net jobs and the notice."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from crm.core import india
from crm.models import (AuditLog, Client, Employee, FollowUp, Holiday, Lead, LeadActivity, LeadSource, LeaveRequest, LeaveType, Notification,
                        Payment, SalesTarget, Shift, StaffDayStat, Task, WorkingDayException)
from crm.modules.leads import services as leads
from crm.modules.performance import jobs, metrics
from crm.modules.worktime import services as wt

from .base import PASSWORD, CrmTestCase

OCT = metrics.Period('pick_month', 'October 2026', date(2026, 10, 1), date(2026, 10, 31))


def at(h, m=0, day=date(2026, 10, 12)):
    return timezone.make_aware(datetime.combine(day, time(h, m)))


class PerfCase(CrmTestCase):
    def lead(self, owner, stage='contacted', day=date(2026, 10, 5), **kw):
        data = dict(name='Lead', phone='+919800000111', source=LeadSource.objects.first(), owner=owner, stage=stage, created_by=owner,
                    assigned_at=at(10, day=day), created_at=at(10, day=day), value=Decimal('10000'),
                    next_follow_up_at=timezone.now() + timedelta(days=2) if stage not in ('won', 'lost') else None)
        data.update(kw)
        return Lead.objects.create(**data)

    def won(self, owner, credited, value, day=date(2026, 10, 10), closed_by=None):
        return self.lead(owner, stage='won', won_at=at(12, day=day), won_value=Decimal(value), value=Decimal(value), credited_to=credited,
                         closed_by=closed_by or credited)

    def fu(self, lead, user, due, status='pending', completed=None, notes='', postponed=0):
        return FollowUp.objects.create(lead=lead, assignee=user, due_at=due, status=status, completed_at=completed, notes=notes, postpone_count=postponed)

    def login_as(self, who):
        c = HttpClient()
        c.force_login(self.u[who])
        return c


# ====================================================================== periods

class PeriodTests(CrmTestCase):
    def test_a_year_is_january_to_december(self):
        p = metrics.period_from({'year': '2025'}, today=date(2026, 11, 15))
        self.assertEqual((p.start, p.end, p.key), (date(2025, 1, 1), date(2025, 12, 31), 'pick_year'))
        p = metrics.period_from({'range': 'year'}, today=date(2026, 11, 15))
        self.assertEqual((p.start, p.end), (date(2026, 1, 1), date(2026, 11, 15)))
        p = metrics.period_from({'range': 'last_year'}, today=date(2026, 11, 15))
        self.assertEqual((p.start, p.end), (date(2025, 1, 1), date(2025, 12, 31)))

    def test_a_picked_month_and_the_current_month(self):
        p = metrics.period_from({'month': '2026-02'}, today=date(2026, 11, 15))
        self.assertEqual((p.start, p.end), (date(2026, 2, 1), date(2026, 2, 28)))
        p = metrics.period_from({'month': '2026-11'}, today=date(2026, 11, 15))
        self.assertEqual(p.end, date(2026, 11, 15))                                    # a running month ends today

    def test_presets(self):
        today = date(2026, 11, 18)                                                      # a Wednesday
        got = {k: metrics.period_from({'range': k}, today=today) for k, _ in metrics.PRESETS if k != 'all'}
        self.assertEqual((got['week'].start, got['week'].end), (date(2026, 11, 16), today))
        self.assertEqual((got['last_week'].start, got['last_week'].end), (date(2026, 11, 9), date(2026, 11, 15)))
        self.assertEqual((got['last_month'].start, got['last_month'].end), (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(got['quarter'].start, date(2026, 10, 1))
        self.assertEqual(got['today'].start, today)

    def test_all_time_starts_from_the_first_record_and_custom_dates_are_clamped(self):
        p = metrics.period_from({'range': 'all'}, today=date(2026, 11, 15), earliest=date(2024, 3, 9))
        self.assertEqual(p.start, date(2024, 3, 9))
        p = metrics.period_from({'from': '2026-11-10', 'to': '2030-01-01'}, today=date(2026, 11, 15))
        self.assertEqual(p.end, date(2026, 11, 15))
        p = metrics.period_from({'from': 'junk'}, today=date(2026, 11, 15))
        self.assertEqual(p.key, 'month')                                                # bad input falls back to the month

    def test_the_period_before(self):
        self.assertEqual((OCT.previous().start, OCT.previous().end), (date(2026, 9, 1), date(2026, 9, 30)))
        y = metrics.period_from({'year': '2025'}, today=date(2026, 11, 15)).previous()
        self.assertEqual((y.start, y.end), (date(2024, 1, 1), date(2024, 12, 31)))
        w = metrics.Period('week', 'w', date(2026, 11, 9), date(2026, 11, 15)).previous()
        self.assertEqual((w.start, w.end), (date(2026, 11, 2), date(2026, 11, 8)))
        self.assertIsNone(metrics.Period('all', 'a', date(2020, 1, 1), date(2026, 1, 1)).previous())


# ====================================================================== working days

class WorkingDayTests(CrmTestCase):
    def test_saturday_and_sunday_are_holidays_by_default(self):
        mon = date(2026, 11, 16)
        days = india.working_days_list(mon, mon + timedelta(days=6))
        self.assertEqual([d.weekday() for d in days], [0, 1, 2, 3, 4])
        self.assertFalse(india.is_working_day(date(2026, 11, 21)))                      # a Saturday
        self.assertFalse(india.is_working_day(date(2026, 11, 22)))                      # a Sunday

    def test_hr_can_make_a_weekend_day_a_working_day(self):
        sat = date(2026, 11, 21)
        WorkingDayException.objects.create(work_date=sat, reason='Festival catch-up')
        self.assertTrue(india.is_working_day(sat))
        self.assertIn(sat, india.working_days_list(date(2026, 11, 16), date(2026, 11, 22)))
        self.assertFalse(india.is_working_day(date(2026, 11, 22)))                      # the Sunday is still off

    def test_a_weekday_holiday_is_not_a_working_day_and_a_holiday_beats_a_working_weekend(self):
        Holiday.objects.create(holiday_date=date(2026, 11, 18), name='Local holiday')
        Holiday.objects.create(holiday_date=date(2026, 11, 21), name='Closed')
        WorkingDayException.objects.create(work_date=date(2026, 11, 21))
        self.assertFalse(india.is_working_day(date(2026, 11, 18)))
        self.assertFalse(india.is_working_day(date(2026, 11, 21)))

    def test_the_next_follow_up_day_skips_the_weekend(self):
        friday_evening = timezone.make_aware(datetime(2026, 11, 20, 18, 0))
        due = india.next_working_day_at(start=friday_evening)
        self.assertEqual((due.weekday(), due.hour, due.minute), (0, 10, 30))

    def test_leave_no_longer_counts_saturdays(self):
        from crm.modules.hr import services as hr
        self.assertEqual(hr.working_days(date(2026, 11, 16), date(2026, 11, 22)), 5)

    def test_hr_adds_and_removes_a_working_weekend_day_and_weekdays_are_refused(self):
        self.login('hr')
        url = reverse('crm:holiday_list')
        future_sat = india.today() + timedelta(days=(5 - india.today().weekday()) % 7 + 7)
        r = self.client.post(url, {'form': 'working_day', 'w-work_date': future_sat.isoformat(), 'w-reason': 'Launch'}, follow=True)
        self.assertContains(r, 'is now a working day')
        self.assertTrue(WorkingDayException.objects.filter(work_date=future_sat).exists())
        weekday = future_sat + timedelta(days=2)
        r = self.client.post(url, {'form': 'working_day', 'w-work_date': weekday.isoformat()})
        self.assertContains(r, 'Only a Saturday or Sunday')
        row = WorkingDayException.objects.get()
        self.client.post(url, {'form': 'delete_working_day', 'id': row.pk})
        self.assertFalse(WorkingDayException.objects.exists())
        self.login('sales')
        self.assertEqual(self.client.post(url, {'form': 'working_day', 'w-work_date': future_sat.isoformat()}).status_code, 403)


# ====================================================================== who gets the credit

class AttributionTests(PerfCase):
    def open_lead(self):
        return self.lead(self.u['sales'], stage='negotiation', value=Decimal('40000'))

    def won_by(self, actor, credit=None):
        lead = self.open_lead()
        leads.mark_won(lead, actor, won_value=Decimal('40000'), note='Confirmed by phone on a call today', credit_to=credit)
        lead.refresh_from_db()
        return lead

    def test_the_owner_closing_their_own_lead_is_credited(self):
        lead = self.won_by(self.u['sales'])
        self.assertEqual((lead.closed_by, lead.credited_to, lead.owner), (self.u['sales'], self.u['sales'], self.u['sales']))

    def test_someone_else_can_credit_the_owner_or_themselves(self):
        lead = self.won_by(self.u['manager'], 'owner')
        self.assertEqual((lead.closed_by, lead.credited_to), (self.u['manager'], self.u['sales']))
        lead = self.won_by(self.u['manager'], 'me')
        self.assertEqual((lead.closed_by, lead.credited_to, lead.owner), (self.u['manager'], self.u['manager'], self.u['sales']))

    def test_without_an_answer_the_credit_stays_with_the_owner(self):
        lead = self.won_by(self.u['manager'])
        self.assertEqual((lead.closed_by, lead.credited_to), (self.u['manager'], self.u['sales']))

    def test_a_made_up_answer_is_refused(self):
        with self.assertRaises(leads.LeadError):
            self.won_by(self.u['manager'], 'somebody-else')

    def test_the_audit_trail_names_who_closed_it_and_who_owns_it(self):
        self.won_by(self.u['manager'], 'me')
        a = AuditLog.objects.filter(action='lead.won').latest('id')
        self.assertEqual((a.changes['closed_by'], a.changes['owner']), (str(self.u['manager']), str(self.u['sales'])))

    def won_via_page(self, who, data=None):
        lead = self.open_lead()
        self.login(who)
        base = {'won-won_value': '40000', 'won-note': 'Confirmed order by phone and email today'}
        base.update(data or {})
        return lead, self.client.post(reverse('crm:lead_action', args=[lead.pk, 'won']), base, follow=True)

    def test_the_popup_asks_a_non_owner_who_gets_the_credit(self):
        lead = self.open_lead()
        self.login('manager')
        page = self.client.get(reverse('crm:lead_detail', args=[lead.pk]))
        self.assertContains(page, 'Who gets the credit for this revenue?')
        self.assertContains(page, 'Me, I closed it')
        self.login('sales')
        self.assertNotContains(self.client.get(reverse('crm:lead_detail', args=[lead.pk])), 'Who gets the credit for this revenue?')

    def test_a_non_owner_must_choose_before_the_win_is_saved(self):
        lead, r = self.won_via_page('manager')
        lead.refresh_from_db()
        self.assertEqual(lead.stage, 'negotiation')
        lead, r = self.won_via_page('manager', {'won-credit_to': 'me'})
        lead.refresh_from_db()
        self.assertEqual((lead.stage, lead.credited_to, lead.closed_by), ('won', self.u['manager'], self.u['manager']))

    def test_the_owner_is_never_asked(self):
        lead, r = self.won_via_page('sales')
        lead.refresh_from_db()
        self.assertEqual((lead.stage, lead.credited_to, lead.closed_by), ('won', self.u['sales'], self.u['sales']))

    def test_closed_by_and_owner_are_shown_on_the_lead_and_in_the_list(self):
        lead = self.won_by(self.u['manager'], 'me')
        self.login('manager')
        detail = self.client.get(reverse('crm:lead_detail', args=[lead.pk]))
        self.assertContains(detail, 'Closed by Manager')
        self.assertContains(detail, 'Owner Sales')
        self.assertContains(detail, 'Revenue credited to Manager')
        listing = self.client.get(reverse('crm:lead_list'), {'stage': 'won'})
        self.assertContains(listing, 'Closed by Manager')

    def test_the_backfill_credits_old_wins_to_their_owner(self):
        from importlib import import_module
        old = self.lead(self.u['sales'], stage='won', won_at=at(9), won_value=Decimal('5000'))
        mig = import_module('crm.migrations.0017_backfill_won_credit')
        from django.apps import apps
        mig.forwards(apps, None)
        old.refresh_from_db()
        self.assertEqual((old.credited_to, old.closed_by), (self.u['sales'], self.u['sales']))


# ====================================================================== the numbers

class MetricTests(PerfCase):
    def setUp(self):
        super().setUp()
        self.s, self.m = self.u['sales'], self.u['manager']
        SalesTarget.objects.create(month=date(2026, 10, 1), user=self.s, lead_count=10, quotation_count=4, client_count=2, call_count=20,
                                   meeting_count=5, won_value=Decimal('100000'), collected_value=Decimal('40000'))

    def test_outcomes_are_credited_to_the_right_person(self):
        self.lead(self.s)
        self.lead(self.s)
        self.lead(self.s, stage='lost', lost_at=at(15), lost_reason='price')
        mine = self.won(self.s, self.s, 50000)
        self.won(self.s, self.m, 30000, closed_by=self.m)                               # closed and credited to the manager
        old = self.lead(self.s, stage='won', won_at=at(9), won_value=Decimal('10000'))   # recorded before credit existed
        r = metrics.compute([self.s.pk], OCT)
        self.assertEqual((r['won'], r['won_value']), (2, Decimal('60000')))              # their own plus the old one, not the manager's
        self.assertEqual(r['lost'], 1)
        self.assertEqual(r['win_rate'], 66)
        self.assertEqual(r['avg_deal'], Decimal('30000'))
        self.assertEqual(metrics.compute([self.m.pk], OCT)['won_value'], Decimal('30000'))
        self.assertIsNotNone(mine)
        self.assertIsNotNone(old)

    def test_collections_follow_the_credit_on_the_lead_the_client_came_from(self):
        w = self.won(self.s, self.s, 50000)
        c = Client.objects.create(legal_name='Won Co', display_name='Won Co', owner=self.m, source_lead=w, billing_state='32')
        Payment.objects.create(client=c, amount=Decimal('20000'), payment_date=date(2026, 10, 12), status='verified')
        Payment.objects.create(client=c, amount=Decimal('5000'), payment_date=date(2026, 9, 12), status='verified')       # another month
        Payment.objects.create(client=c, amount=Decimal('7000'), payment_date=date(2026, 10, 14), status='pending')       # not counted
        self.assertEqual(metrics.compute([self.s.pk], OCT)['collected'], Decimal('20000'))
        self.assertEqual(metrics.compute([self.m.pk], OCT)['collected'], Decimal('0'))
        self.assertEqual(metrics.compute([self.s.pk], OCT)['clients'], 0)                # the client was not created in October

    def test_onboarded_clients_are_those_created_from_their_wins(self):
        w = self.won(self.s, self.s, 50000)
        c = Client.objects.create(legal_name='New Co', display_name='New Co', owner=self.m, source_lead=w, billing_state='32',
                                  created_at=at(13))
        self.assertEqual(c.source_lead, w)
        self.assertEqual(metrics.compute([self.s.pk], OCT)['clients'], 1)

    def test_follow_up_discipline(self):
        lead = self.lead(self.s)
        due = at(10, day=date(2026, 10, 6))
        self.fu(lead, self.s, due, 'done', completed=due)                                         # on time
        self.fu(lead, self.s, due, 'done', completed=due + timedelta(hours=5))                    # late
        self.fu(lead, self.s, due, 'cancelled', completed=due, notes='Client asked not to call')  # skipped by the person
        self.fu(lead, self.s, due, 'cancelled', completed=due, notes='Lead won')                  # closed with the lead: not a skip
        self.fu(lead, self.s, due, 'pending', postponed=2)                                         # long overdue: skipped
        r = metrics.compute([self.s.pk], OCT)
        self.assertEqual((r['fu_due'], r['fu_done'], r['fu_on_time'], r['fu_late'], r['fu_skipped']), (4, 2, 1, 1, 2))
        self.assertEqual(r['fu_compliance'], 25)
        self.assertEqual(r['fu_postponed'], 2)

    def test_activity_counts(self):
        lead = self.lead(self.s)
        for kind, outcome, mins in (('call', 'connected', 6), ('call', 'no_answer', 1), ('meeting', '', 30), ('whatsapp', '', None), ('note', '', None)):
            LeadActivity.objects.create(lead=lead, actor=self.s, kind=kind, outcome=outcome, duration_minutes=mins, summary='x', occurred_at=at(11))
        LeadActivity.objects.create(lead=lead, actor=self.m, kind='call', summary='not theirs', occurred_at=at(11))
        r = metrics.compute([self.s.pk], OCT)
        self.assertEqual((r['calls'], r['calls_connected'], r['call_minutes'], r['meetings'], r['messages'], r['notes']), (2, 1, 7, 1, 1, 1))

    def test_tasks(self):
        from crm.modules.projects import services as ps
        proj = ps.create_project(self.u['pm'], client=self.client_obj, name='Store', manager=self.u['pm'], start_date=date(2026, 10, 1),
                                 due_date=date(2026, 12, 1))
        Task.objects.create(project=proj, title='a', assignee=self.s, due_date=date(2026, 10, 8), status='done', completed_at=at(9, day=date(2026, 10, 8)), created_by=self.u['pm'])
        Task.objects.create(project=proj, title='b', assignee=self.s, due_date=date(2026, 10, 8), status='done', completed_at=at(9, day=date(2026, 10, 10)), created_by=self.u['pm'])
        Task.objects.create(project=proj, title='c', assignee=self.s, due_date=date(2026, 10, 9), status='todo', created_by=self.u['pm'])
        Task.objects.create(project=proj, title='d', assignee=self.s, due_date=date(2026, 10, 9), status='cancelled', created_by=self.u['pm'])
        r = metrics.compute([self.s.pk], OCT)
        self.assertEqual((r['tasks'], r['tasks_done'], r['tasks_late'], r['tasks_overdue'], r['tasks_cancelled']), (3, 2, 1, 1, 1))

    def test_time_and_presence_with_a_shift_and_leave(self):
        shift = Shift.objects.create(name='Day', start_time=time(9, 0), end_time=time(18, 0), grace_minutes=15)
        emp = Employee.objects.create(user=self.s, code='E100', name='Sales', work_email='sales@example.test', joining_date=date(2026, 10, 12),
                                      shift=shift, status='active')
        d1, d2, d3 = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
        wt.on_login(self.s, at(9, 5, d1))                                                         # on time
        wt.start_break(self.s, at(13, day=d1))
        wt.end_break(self.s, at(14, day=d1))
        wt.on_logout(self.s, now=at(18, day=d1))
        wt.on_login(self.s, at(10, 0, d2))                                                        # late
        wt.on_logout(self.s, now=at(16, day=d2))
        lt = LeaveType.objects.first()
        LeaveRequest.objects.create(employee=emp, leave_type=lt, start_date=d3, end_date=d3, days=1, reason='Family function', status='approved')
        r = metrics.compute([self.s.pk], OCT)
        self.assertEqual((r['days_worked'], r['break_seconds'], r['late_logins']), (2, 3600, 1))
        self.assertEqual(r['worked_seconds'], (8 * 3600 + 55 * 60 - 3600) + 6 * 3600)
        self.assertEqual(r['leave_days'], Decimal('1'))
        self.assertEqual(r['leave_by_type'], {lt.name: Decimal('1')})
        should = [d for d in india.working_days_list(date(2026, 10, 12), date(2026, 10, 31))]       # nothing before joining is counted
        self.assertEqual(r['absent'], len(should) - 3)                                           # two worked days and one leave day
        self.assertNotIn(d3, r['absent_days'])
        self.assertTrue(all(d >= date(2026, 10, 12) for d in r['absent_days']))
        self.assertEqual(r['usual_login'], '09:32')

    def test_a_person_with_no_record_is_absent_every_working_day(self):
        r = metrics.compute([self.u['sales_b'].pk], OCT)
        self.assertEqual(r['absent'], len(india.working_days_list(OCT.start, OCT.end)))

    def test_a_working_saturday_is_expected_and_a_weekend_is_not(self):
        sat = date(2026, 10, 10)
        before = metrics.compute([self.u['sales_b'].pk], OCT)['absent']
        WorkingDayException.objects.create(work_date=sat)
        self.assertEqual(metrics.compute([self.u['sales_b'].pk], OCT)['absent'], before + 1)

    def test_targets_and_the_gap(self):
        self.won(self.s, self.s, 50000)
        r = metrics.compute([self.s.pk], OCT)
        rows = {x['key']: x for x in r['rows']}
        self.assertEqual((rows['won']['target'], rows['won']['actual'], rows['won']['pct'], rows['won']['diff']),
                         (Decimal('100000'), Decimal('50000'), 50, Decimal('-50000')))
        self.assertEqual(rows['won']['tone'], 'bad')
        self.assertEqual(rows['leads']['target'], 10)
        self.assertEqual(rows['clients']['target'], 2)

    def test_a_target_is_shared_out_by_working_days_for_part_of_a_month(self):
        week = metrics.Period('custom', 'w', date(2026, 10, 5), date(2026, 10, 9))               # one full working week
        full = len(india.working_days_list(date(2026, 10, 1), date(2026, 10, 31)))
        t = metrics.targets_for([self.s.pk], week)
        self.assertEqual(round(t['won']), round(Decimal('100000') * 5 / full))

    def test_team_targets_use_the_team_row_when_there_is_one(self):
        from crm.models import Team
        team = Team.objects.first()
        SalesTarget.objects.create(month=date(2026, 10, 1), team=team, won_value=Decimal('500000'))
        t = metrics.targets_for([self.s.pk], OCT, team_ids=[team.pk])
        self.assertEqual(t['won'], Decimal('500000'))

    def test_funnel_and_pipeline(self):
        self.lead(self.s, stage='qualified', probability=40)
        self.lead(self.s, stage='negotiation', value=Decimal('20000'), probability=50)
        self.won(self.s, self.s, 30000)
        r = metrics.compute([self.s.pk], OCT)
        self.assertEqual(r['funnel'][0]['count'], 3)
        self.assertEqual(r['funnel'][-1]['count'], 1)
        self.assertEqual(r['open_leads'], 2)
        self.assertEqual(r['weighted'], Decimal('10000') * Decimal('0.4') + Decimal('10000'))      # 10000 at 40%, 20000 at 50%
        self.assertEqual(r['stale'], 2)                                                           # nothing touched since October

    def test_first_response(self):
        self.lead(self.s, first_response_seconds=600)
        self.lead(self.s, first_response_seconds=7200)
        r = metrics.compute([self.s.pk], OCT)
        self.assertEqual((r['first_response_min'], r['slow_first_response']), (65, 1))

    def test_flags_are_plain_and_name_their_number(self):
        due = at(10, day=date(2026, 10, 6))
        lead = self.lead(self.s)
        for _ in range(6):
            self.fu(lead, self.s, due)                                                          # six that were never done
        r = metrics.compute([self.s.pk], OCT)
        texts = {f['level']: [] for f in metrics.flags(r, OCT)}
        for f in metrics.flags(r, OCT):
            texts[f['level']].append(f['text'])
        self.assertTrue(any('6 follow-ups skipped' in t for t in texts['bad']))
        self.assertTrue(any('not logged in' in t for t in texts['bad'] + texts['warn']))
        self.assertTrue(any('target' in t and 'Collected' in t for t in texts['bad']))
        self.assertEqual(metrics.flags(r, OCT)[0]['level'], 'bad')                              # worst first

    def test_no_target_is_said_plainly(self):
        r = metrics.compute([self.u['sales_b'].pk], OCT)
        self.assertTrue(any('No target is set' in f['text'] for f in metrics.flags(r, OCT)))

    def test_graph_data_has_twelve_months_and_twelve_weeks(self):
        self.won(self.s, self.s, 50000)
        t = metrics.trend([self.s.pk], date(2026, 10, 31))
        self.assertEqual(len(t['monthly']['labels']), 12)
        self.assertEqual(t['monthly']['labels'][-1], 'Oct 26')
        self.assertEqual(t['monthly']['won'][-1], 50000.0)
        self.assertEqual(t['monthly']['target'][-1], 100000.0)
        self.assertEqual(len(t['weekly']['labels']), 12)

    def test_attendance_calendar_kinds(self):
        wt.on_login(self.s, at(9, day=date(2026, 10, 12)))
        wt.on_logout(self.s, now=at(17, day=date(2026, 10, 12)))
        days = {c['day']: c['kind'] for c in metrics.calendar_days(self.s.pk, OCT)}
        self.assertEqual(days[date(2026, 10, 12)], 'worked')
        self.assertEqual(days[date(2026, 10, 13)], 'absent')
        self.assertEqual(days[date(2026, 10, 10)], 'off')                                        # a Saturday


# ====================================================================== who may open what

class AccessTests(PerfCase):
    def test_who_can_open_a_profile(self):
        s, b, mgr = self.u['sales'], self.u['sales_b'], self.u['manager']
        url = lambda u: reverse('crm:staff_profile', args=[u.pk])
        for who, target, status in (('owner', s, 200), ('hr', s, 200), ('hr', mgr, 200), ('manager', s, 200), ('manager', b, 200),
                                    ('manager', mgr, 200), ('manager', self.u['finance'], 403), ('manager', self.u['hr'], 403),
                                    ('sales', s, 403), ('sales', b, 403), ('finance', s, 403), ('dev', s, 403), ('pm', s, 403)):
            self.login(who)
            self.assertEqual(self.client.get(url(target)).status_code, status, f'{who} -> {target.username}')

    def test_a_manager_sees_only_their_own_team(self):
        from crm.models import Team, UserProfile
        other = Team.objects.create(name='Other team', manager=self.u['finance'])
        UserProfile.objects.filter(user=self.u['sales_b']).update(team=other)
        self.login('manager')
        self.assertEqual(self.client.get(reverse('crm:staff_profile', args=[self.u['sales_b'].pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('crm:staff_profile', args=[self.u['sales'].pk])).status_code, 200)

    def test_the_board_for_a_manager_shows_the_team_and_for_hr_all_sales_staff(self):
        self.login('manager')
        r = self.client.get(reverse('crm:performance_board'))
        names = {x['user'].username for x in r.context['rows']}
        self.assertTrue({'sales', 'sales_b', 'manager'} <= names)
        self.assertNotIn('finance', names)
        self.login('hr')
        names = {x['user'].username for x in self.client.get(reverse('crm:performance_board')).context['rows']}
        self.assertTrue({'sales', 'sales_b', 'manager'} <= names)
        self.assertNotIn('dev', names)
        self.assertNotIn('owner', names)
        for who in ('sales', 'finance', 'dev'):
            self.login(who)
            self.assertEqual(self.client.get(reverse('crm:performance_board')).status_code, 403, who)

    def test_the_profile_renders_every_section_and_each_view_is_audited(self):
        self.login('hr')
        r = self.client.get(reverse('crm:staff_profile', args=[self.u['sales'].pk]), {'month': '2026-10', 'compare': '1'})
        for text in ('Targets and results', 'Follow-ups', 'Time and presence', 'Attendance calendar', 'Funnel of the leads assigned',
                     'Revenue against target', 'October 2026', 'Compare with the period before'):
            self.assertContains(r, text)
        self.assertTrue(AuditLog.objects.filter(action='performance.viewed', object_id=str(self.u['sales'].pk), actor_id=self.u['hr'].pk).exists())

    def test_every_period_control_works(self):
        self.login('hr')
        url = reverse('crm:staff_profile', args=[self.u['sales'].pk])
        for params in ({'range': k} for k, _ in metrics.PRESETS):
            self.assertEqual(self.client.get(url, params).status_code, 200, params)
        for params in ({'year': '2025'}, {'month': '2026-02'}, {'from': '2026-10-01', 'to': '2026-10-15'}, {'month': 'nonsense'}):
            self.assertEqual(self.client.get(url, params).status_code, 200, params)

    def test_a_managers_profile_has_an_individual_and_a_team_view(self):
        self.login('hr')
        url = reverse('crm:staff_profile', args=[self.u['manager'].pk])
        solo = self.client.get(url, {'month': '2026-10'})
        self.assertContains(solo, 'Individual')
        self.assertNotContains(solo, 'The team, side by side')
        team = self.client.get(url, {'month': '2026-10', 'view': 'team'})
        self.assertContains(team, 'The team, side by side')
        self.assertContains(team, 'Team average')
        names = {r['user'].username for r in team.context['team_rows']}
        self.assertEqual(names, {'sales', 'sales_b'})                                           # the manager is not counted in their own team
        self.assertNotContains(self.client.get(reverse('crm:staff_profile', args=[self.u['sales'].pk])), 'The team, side by side')

    def test_an_executive_has_no_team_view(self):
        self.login('hr')
        r = self.client.get(reverse('crm:staff_profile', args=[self.u['sales'].pk]), {'view': 'team'})
        self.assertEqual(r.context['mode'], 'individual')

    def test_the_employee_page_links_to_the_performance_profile(self):
        emp = Employee.objects.create(user=self.u['sales'], code='E200', name='Sales', work_email='s@example.test', joining_date=date(2026, 1, 5),
                                      status='active')
        self.login('hr')
        r = self.client.get(reverse('crm:employee_detail', args=[emp.pk]))
        self.assertContains(r, reverse('crm:staff_profile', args=[self.u['sales'].pk]))
        page = self.client.get(reverse('crm:staff_profile', args=[self.u['sales'].pk]))
        self.assertContains(page, reverse('crm:employee_detail', args=[emp.pk]))

    def test_a_profile_without_an_hr_record_says_so_to_hr(self):
        self.login('hr')
        self.assertContains(self.client.get(reverse('crm:staff_profile', args=[self.u['sales'].pk])), 'No HR record is linked')

    def test_the_new_targets_can_be_saved(self):
        self.login('manager')
        r = self.client.post(reverse('crm:target_save'), {'month': '2026-10', 'user': self.u['sales'].pk, 'lead_count': 10, 'quotation_count': 4,
                                                          'client_count': 3, 'call_count': 50, 'meeting_count': 8, 'won_value': '100000',
                                                          'collected_value': '60000'})
        t = SalesTarget.objects.get(user=self.u['sales'])
        self.assertEqual((t.client_count, t.call_count, t.meeting_count), (3, 50, 8))
        self.assertEqual(r.status_code, 302)


# ====================================================================== the safety nets

class MissingFollowUpTests(PerfCase):
    def test_every_open_lead_gets_a_follow_up_and_the_owner_is_told(self):
        lead = self.lead(self.u['sales'])
        self.assertFalse(lead.follow_ups.exists())
        n = jobs.missing_next_action()
        self.assertEqual(n, 1)
        fu = lead.follow_ups.get()
        self.assertEqual((fu.assignee, fu.status), (self.u['sales'], 'pending'))
        self.assertTrue(india.is_working_day(timezone.localtime(fu.due_at).date()))                # never a weekend
        lead.refresh_from_db()
        self.assertEqual(lead.next_follow_up_at, fu.due_at)
        self.assertTrue(Notification.objects.filter(user=self.u['sales'], event_type='no_next_action').exists())
        self.assertTrue(Notification.objects.filter(user=self.u['manager'], event_type='no_next_action').exists())
        self.assertEqual(StaffDayStat.objects.get(user=self.u['sales'], day=india.today()).auto_scheduled, 1)
        self.assertTrue(AuditLog.objects.filter(action='lead.followup_auto_scheduled').exists())

    def test_it_runs_twice_without_doubling(self):
        self.lead(self.u['sales'])
        jobs.missing_next_action()
        self.assertEqual(jobs.missing_next_action(), 0)
        self.assertEqual(FollowUp.objects.count(), 1)

    def test_leads_that_are_fine_or_closed_are_left_alone(self):
        ok = self.lead(self.u['sales'])
        self.fu(ok, self.u['sales'], timezone.now() + timedelta(days=1))
        self.won(self.u['sales'], self.u['sales'], 1000)
        self.lead(self.u['sales'], stage='lost', lost_at=at(9))
        self.lead(None, stage='new')
        self.assertEqual(jobs.missing_next_action(), 0)

    def test_the_nightly_job_is_registered(self):
        from crm.core import jobs as registry
        self.assertIn('leads.missing_next_action', registry.JOBS)
        self.assertIn('performance.snapshot', registry.JOBS)


class SnapshotTests(PerfCase):
    def test_the_night_snapshot_records_sales_staff_only_and_updates_in_place(self):
        lead = self.lead(self.u['sales'], stage='negotiation', value=Decimal('25000'))
        self.fu(lead, self.u['sales'], timezone.now() - timedelta(hours=3))
        n = jobs.snapshot()
        users = set(StaffDayStat.objects.values_list('user__username', flat=True))
        self.assertTrue({'sales', 'sales_b', 'manager'} <= users)
        self.assertNotIn('owner', users)
        self.assertNotIn('finance', users)
        row = StaffDayStat.objects.get(user=self.u['sales'], day=india.today())
        self.assertEqual((row.open_leads, row.open_pipeline, row.overdue_followups), (1, Decimal('25000'), 1))
        self.assertEqual(jobs.snapshot(), n)
        self.assertEqual(StaffDayStat.objects.filter(user=self.u['sales'], day=india.today()).count(), 1)

    def test_a_snapshot_never_wipes_the_auto_scheduled_count(self):
        StaffDayStat.objects.create(user=self.u['sales'], day=india.today(), auto_scheduled=3)
        jobs.snapshot()
        self.assertEqual(StaffDayStat.objects.get(user=self.u['sales'], day=india.today()).auto_scheduled, 3)


# ====================================================================== the notice

class NoticeTests(CrmTestCase):
    def sign_in(self, who):
        c = HttpClient()
        c.post(reverse('crm:login'), {'username': who, 'password': PASSWORD})
        return c

    def test_tracked_staff_are_told_once_and_the_text_is_always_on_my_time(self):
        c = self.sign_in('sales')
        page = c.get(reverse('crm:my_day'))
        self.assertContains(page, 'Before you start')
        self.assertContains(page, 'are recorded')
        r = c.post(reverse('crm:work_notice_ack'), HTTP_REFERER='http://testserver/crm/')
        self.assertEqual(r.status_code, 302)
        self.assertNotContains(c.get(reverse('crm:my_day')), 'Before you start')
        self.assertContains(c.get(reverse('crm:my_time')), 'What is recorded')
        self.assertTrue(AuditLog.objects.filter(action='worktime.notice_acknowledged', actor_id=self.u['sales'].pk).exists())

    def test_administrators_are_never_shown_it(self):
        c = self.sign_in('owner')
        self.assertNotContains(c.get(reverse('crm:my_day')), 'Before you start')

    def test_a_new_version_asks_everyone_again(self):
        c = self.sign_in('sales')
        c.post(reverse('crm:work_notice_ack'))
        from unittest.mock import patch
        with patch('crm.modules.worktime.services.NOTICE_VERSION', 2):
            self.assertContains(c.get(reverse('crm:my_day')), 'Before you start')

    def test_the_notice_cannot_be_acknowledged_by_a_get_or_without_a_token(self):
        c = self.sign_in('sales')
        self.assertEqual(c.get(reverse('crm:work_notice_ack')).status_code, 405)
        strict = HttpClient(enforce_csrf_checks=True)
        strict.force_login(self.u['finance'])
        strict.post(reverse('crm:work_notice_ack'))
        from crm.models import UserProfile
        self.assertEqual(UserProfile.objects.get(user=self.u['finance']).work_notice_version, 0)
