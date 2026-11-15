"""The daily report: content, the code, the sign-out pop-up, and how HR and admins read and check it."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from crm.core import india
from crm.models import (AuditLog, Client, DailyReport, FollowUp, Lead, LeadActivity, Payment, Task)
from crm.modules.performance import daily
from crm.modules.worktime import services as wt

from .base import PASSWORD
from .test_deliverables import DeliverableCase
from .test_performance import PerfCase

DAY = date(2026, 10, 12)
CODE = re.compile(r'^DR\d{6}-[A-Z2-9]{4}-[A-Z0-9]{2}$')


def at(h, m=0, day=DAY):
    return timezone.make_aware(datetime.combine(day, time(h, m)))


class ReportCase(PerfCase):
    def busy_day(self):
        s, m = self.u['sales'], self.u['manager']
        a = self.lead(s, day=DAY, created_by=s, value=Decimal('10000'))
        self.lead(s, day=DAY, created_by=s, value=Decimal('10000'))
        self.lead(s, day=DAY, created_by=m, value=Decimal('10000'))                                  # assigned to them, made by the manager
        for kind, outcome in (('call', 'connected'), ('call', 'connected'), ('call', 'no_answer'), ('meeting', ''), ('whatsapp', ''), ('email', '')):
            LeadActivity.objects.create(lead=a, actor=s, kind=kind, outcome=outcome, summary='x', occurred_at=at(11))
        due = at(10)
        self.fu(a, s, due, 'done', completed=due)
        self.fu(a, s, due, 'done', completed=due + timedelta(hours=5))
        self.fu(a, s, due, 'cancelled', completed=at(12), notes='Client travelling')
        self.fu(a, s, due, 'cancelled', completed=at(12), notes='Lead won')
        self.fu(a, s, timezone.now() + timedelta(days=3))
        self.fu(a, s, timezone.now() + timedelta(days=4))
        won = self.won(s, s, 50000, day=DAY)
        self.won(s, m, 30000, day=DAY, closed_by=m)                                                  # credited to the manager, not them
        self.lead(s, stage='lost', lost_at=at(15), lost_reason='price')
        c = Client.objects.create(legal_name='Won Co', display_name='Won Co', owner=m, source_lead=won, billing_state='32', created_at=at(13))
        Payment.objects.create(client=c, amount=Decimal('20000'), payment_date=DAY, status='verified')
        from crm.modules.projects import services as ps
        proj = ps.create_project(self.u['pm'], client=self.client_obj, name='Store build', manager=self.u['pm'], start_date=DAY, due_date=DAY + timedelta(days=30))
        for title in ('Build cart', 'Fix menu'):
            Task.objects.create(project=proj, title=title, assignee=s, due_date=DAY, status='done', completed_at=at(16), created_by=self.u['pm'])
        Task.objects.create(project=proj, title='Late one', assignee=s, due_date=DAY - timedelta(days=2), created_by=self.u['pm'])
        wt.on_login(s, at(9, 5))
        wt.start_break(s, at(13))
        wt.end_break(s, at(14))
        wt.on_logout(s, now=at(18, 10))                      # signing out freezes the report
        return daily.generate(s, DAY, now=at(19))


class ContentTests(ReportCase):
    def test_every_number_comes_from_the_days_records(self):
        rep = self.busy_day()
        d = rep.data
        self.assertEqual(d['time'], {**d['time'], 'in': '09:05', 'out': '18:10', 'worked': '8h 05m', 'break': '1h 00m', 'breaks': 1})
        s = d['sales_data']
        self.assertEqual((s['leads_new'], Decimal(s['leads_new_value'])), (2, Decimal('20000')))
        self.assertEqual((s['leads_assigned'], Decimal(s['leads_assigned_value'])), (3, Decimal('30000')))    # two they made and one the manager made
        self.assertEqual((s['calls'], s['connected'], s['meetings'], s['messages']), (3, 2, 1, 2))
        self.assertEqual((s['fu_done'], s['fu_on_time'], s['fu_skipped'], s['fu_pending']), (2, 1, 1, 2))
        self.assertEqual((s['won'], Decimal(s['won_value']), s['lost']), (1, Decimal('50000'), 1))                   # the manager's credited win is not theirs
        self.assertEqual(Decimal(s['collected']), Decimal('20000'))
        self.assertEqual(s['clients'], 1)
        self.assertEqual(d['tasks']['done'], 2)
        self.assertEqual(d['tasks']['projects'], ['Store build'])
        self.assertEqual(d['tasks']['overdue'], 1)

    def test_the_text_is_short_complete_and_ready_to_paste(self):
        rep = self.busy_day()
        t = rep.text
        for line in ('DAILY REPORT | Sales', 'Mon 12 Oct 2026', 'In 09:05 | Out 18:10 | Worked 8h 05m | Break 1h 00m', 'SALES',
                     'Leads: 2 new', 'Calls: 3 (2 connected) | Meetings 1 | Messages 2', 'Follow-ups: 2 done (1 on time), 1 skipped | 2 pending',
                     'Won: 1', 'Collected: ₹20,000 (Won Co 20,000)', 'Clients onboarded: 1 (Won Co)', 'Pipeline:', 'PROJECTS AND TASKS',
                     'Tasks done: 2 (Build cart, Fix menu) | Projects: Store build | Overdue 1', f'Code: {rep.code}'):
            self.assertIn(line, t)
        self.assertLess(len(t.splitlines()), 22)                                                         # short
        self.assertLess(len(t), 1200)
        self.assertNotIn('<', t)

    def test_a_quiet_day_still_shows_the_core_sales_lines_as_zero(self):
        s = self.u['sales']
        wt.on_login(s, at(9))
        wt.on_logout(s, now=at(17))
        rep = daily.generate(s, DAY, now=at(18))
        for line in ('Leads: 0 new', 'Calls: 0 (0 connected)', 'Won: 0', 'Collected: ₹0'):
            self.assertIn(line, rep.text)
        self.assertNotIn('PROJECTS AND TASKS', rep.text)                                                  # nothing to say there
        self.assertNotIn('CLIENT DELIVERABLES', rep.text)

    def test_people_who_do_not_sell_get_time_and_work_but_no_sales_block(self):
        f = self.u['finance']
        wt.on_login(f, at(9))
        wt.on_logout(f, now=at(17))
        rep = daily.generate(f, DAY, now=at(18))
        self.assertIn('Worked 8h 00m', rep.text)
        self.assertNotIn('SALES', rep.text)

    def test_a_person_who_never_logged_in_does_not_read_as_still_working(self):
        d = daily.build(self.u['sales_b'], DAY)
        self.assertIn('No login recorded', daily.render(d))
        self.assertNotIn('still working', daily.render(d))

    def test_administrators_get_no_report(self):
        self.assertIsNone(daily.generate(self.u['owner'], DAY))
        self.assertFalse(DailyReport.objects.filter(user=self.u['owner']).exists())

    def test_names_are_clipped_so_it_stays_short(self):
        self.assertEqual(daily._names(['a', 'b', 'c', 'd', 'e']), 'a, b, c +2 more')


class DeliverableLineTests(DeliverableCase):
    def test_delivered_work_and_files_are_counted_by_kind(self):
        from crm.modules.deliverables import services as ds
        plan = self.plan()
        poster = self.live(plan, 'Poster').order_by('due_date').first()
        reels = self.live(plan, 'Reels').order_by('due_date').first()
        ds.start(poster.pk, self.u['sales'])
        Deliverable_update = type(poster).objects.filter(pk=poster.pk)
        Deliverable_update.update(review_state='approved')
        ds.deliver(poster.pk, self.u['sales'], note='poster.png')
        ds.deliver(reels.pk, self.u['sales'], note='reels', done=1, shortfall_reason='one only')
        ds.add_asset(poster.pk, self.u['sales'], name='Poster file', kind='link', url='https://x.example/p')
        wt.on_login(self.u['sales'], timezone.now() - timedelta(minutes=5))
        d = daily.build(self.u['sales'], india.today())
        self.assertEqual(d['deliverables']['delivered'], 2)
        self.assertEqual(d['deliverables']['by_kind'], {'Poster': 1, 'Reel or video': 1})
        self.assertEqual(d['deliverables']['files'], 1)
        text = daily.render(d)
        self.assertIn('CLIENT DELIVERABLES', text)
        self.assertIn('Delivered: 2 - 1 Poster, 1 Reel or video (Acme)', text)
        self.assertIn('Files added 1', text)


class CodeTests(ReportCase):
    def test_the_code_has_a_fixed_shape_and_is_unique(self):
        rep = self.busy_day()
        self.assertRegex(rep.code, CODE)
        self.assertTrue(rep.code.startswith('DR261012-'))
        self.assertEqual(DailyReport.objects.filter(code=rep.code).count(), 1)

    def test_a_genuine_report_checks_out_and_an_altered_one_does_not(self):
        rep = self.busy_day()
        self.assertTrue(daily.verify(rep))
        DailyReport.objects.filter(pk=rep.pk).update(text=rep.text.replace('Won: 1', 'Won: 9'))
        rep.refresh_from_db()
        self.assertFalse(daily.verify(rep))

    def test_a_code_cannot_be_moved_to_another_report(self):
        rep = self.busy_day()
        other = DailyReport.objects.create(user=self.u['manager'], day=DAY, seq=1, code='DR261012-ZZZZ-00', text='x', data={}, digest='0' * 64)
        DailyReport.objects.filter(pk=other.pk).update(code=rep.code[:-2] + 'XX')
        other.refresh_from_db()
        self.assertFalse(daily.verify(other))

    def test_the_same_day_with_nothing_new_is_the_same_report(self):
        rep = self.busy_day()
        again = daily.generate(self.u['sales'], DAY, now=at(19))
        self.assertEqual(again.pk, rep.pk)
        self.assertEqual(DailyReport.objects.filter(user=self.u['sales'], day=DAY).count(), 1)

    def test_a_change_after_the_report_makes_a_new_version_and_keeps_the_old(self):
        rep = self.busy_day()
        LeadActivity.objects.create(lead=Lead.objects.filter(owner=self.u['sales']).first(), actor=self.u['sales'], kind='call', outcome='connected',
                                    summary='late call', occurred_at=at(17))
        new = daily.generate(self.u['sales'], DAY, now=at(20))
        self.assertEqual((new.seq, new.pk != rep.pk), (2, True))
        self.assertNotEqual(new.code, rep.code)
        rep.refresh_from_db()
        self.assertTrue(daily.verify(rep) and daily.verify(new))                                         # the first one is untouched
        self.assertIn('Calls: 3 (2 connected)', rep.text)
        self.assertIn('Calls: 4 (3 connected)', new.text)

    def test_comparing_a_pasted_copy(self):
        rep = self.busy_day()
        self.assertTrue(daily.same_text(rep, rep.text))
        self.assertTrue(daily.same_text(rep, '\r\n'.join(line + '  ' for line in rep.text.split('\n'))))   # whitespace does not matter
        self.assertFalse(daily.same_text(rep, rep.text.replace('Won: 1', 'Won: 2')))


class SignOutFlowTests(ReportCase):
    def sign_in(self, who='sales'):
        c = HttpClient()
        c.post(reverse('crm:login'), {'username': who, 'password': PASSWORD})
        return c

    def test_signing_out_shows_the_report_on_the_login_page(self):
        c = self.sign_in()
        r = c.post(reverse('crm:logout'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/crm/login/?report=', r['Location'])
        page = c.get(r['Location'])
        self.assertContains(page, 'Your daily report')
        self.assertContains(page, 'DAILY REPORT | Sales')
        rep = DailyReport.objects.get(user=self.u['sales'])
        self.assertContains(page, rep.code)
        self.assertContains(page, 'readonly')                                                            # it cannot be edited
        self.assertContains(page, 'Closing in')                                                          # and shuts itself after copying
        self.assertContains(page, 'Copy report')
        rep.refresh_from_db()
        self.assertIsNotNone(rep.shown_at)

    def test_the_popup_closes_five_seconds_after_copying(self):
        c = self.sign_in()
        page = c.get(c.post(reverse('crm:logout'))['Location'])
        html = page.content.decode()
        self.assertIn('var left = 5;', html)
        self.assertIn("setInterval(function () { left -= 1;", html)

    def test_a_forged_or_old_link_shows_nothing(self):
        c = self.sign_in()
        c.post(reverse('crm:logout'))
        self.assertNotContains(c.get(reverse('crm:login'), {'report': 'forged'}), 'Your daily report')
        rep = DailyReport.objects.get(user=self.u['sales'])
        token = daily.token_for(rep)
        from unittest.mock import patch
        with patch('crm.modules.performance.daily.TOKEN_MAX_AGE', -1):
            self.assertNotContains(c.get(reverse('crm:login'), {'report': token}), 'Your daily report')

    def test_the_page_does_not_let_a_name_break_out_of_the_popup(self):
        get_user = type(self.u['sales']).objects
        get_user.filter(pk=self.u['sales'].pk).update(first_name='<script>alert(1)</script>')
        c = self.sign_in()
        page = c.get(c.post(reverse('crm:logout'))['Location'])
        self.assertNotContains(page, '<script>alert(1)</script>')

    def test_copying_and_closing_are_recorded(self):
        c = self.sign_in()
        c.get(c.post(reverse('crm:logout'))['Location'])
        rep = DailyReport.objects.get(user=self.u['sales'])
        token = daily.token_for(rep)
        self.assertEqual(c.post(reverse('crm:report_event'), {'token': token, 'event': 'copied'}).status_code, 200)
        rep.refresh_from_db()
        self.assertEqual(rep.copy_count, 1)
        self.assertIsNotNone(rep.copied_at)
        self.assertEqual(c.post(reverse('crm:report_event'), {'token': 'nonsense', 'event': 'copied'}).status_code, 400)
        self.assertEqual(c.get(reverse('crm:report_event')).status_code, 405)

    def test_administrators_see_no_report_when_they_sign_out(self):
        c = self.sign_in('owner')
        r = c.post(reverse('crm:logout'))
        self.assertNotIn('report=', r['Location'])

    def test_a_day_closed_for_them_is_made_into_a_report_anyway(self):
        s = self.u['sales']
        wt.on_login(s, at(9))
        wt.close_stale(at(23, 30))                                                                       # signed out after 12 idle hours
        rep = DailyReport.objects.get(user=s, day=DAY)
        self.assertIn('Out 09:00', rep.text)
        self.assertIsNone(rep.shown_at)

    def test_it_is_shown_after_the_next_sign_in_if_it_was_never_seen(self):
        s = self.u['sales']
        wt.on_login(s, at(9))
        wt.close_stale(at(23, 30))
        c = self.sign_in()
        c.post(reverse('crm:work_notice_ack'))                                                           # the one-time notice first
        page = c.get(reverse('crm:my_day'))
        self.assertContains(page, 'Your daily report')
        self.assertContains(page, 'DAILY REPORT | Sales')
        rep = DailyReport.objects.get(user=s, day=DAY)
        c.post(reverse('crm:report_event'), {'token': daily.token_for(rep), 'event': 'closed'})
        self.assertNotContains(c.get(reverse('crm:my_day')), 'Your daily report')

    def test_the_two_popups_never_stack(self):
        s = self.u['sales']
        wt.on_login(s, at(9))
        wt.close_stale(at(23, 30))
        c = self.sign_in()
        page = c.get(reverse('crm:my_day'))                                                              # the staff notice has not been answered
        self.assertContains(page, 'Before you start')
        self.assertNotContains(page, 'Your daily report')

    def test_todays_report_is_not_shown_again_on_the_same_day(self):
        c = self.sign_in()
        c.post(reverse('crm:work_notice_ack'))
        rep = daily.generate(self.u['sales'])
        self.assertIsNotNone(rep)
        self.assertNotContains(c.get(reverse('crm:my_day')), 'Your daily report')


class HrViewTests(ReportCase):
    def setUp(self):
        super().setUp()
        self.rep = self.busy_day()

    def test_who_may_read_a_report(self):
        url = reverse('crm:daily_report', args=[self.rep.pk])
        for who, status in (('owner', 200), ('hr', 200), ('manager', 200), ('sales', 403), ('sales_b', 403), ('finance', 403), ('dev', 403)):
            self.login(who)
            self.assertEqual(self.client.get(url).status_code, status, who)

    def test_a_manager_cannot_read_a_report_from_outside_their_team(self):
        from crm.models import Team, UserProfile
        other = Team.objects.create(name='Elsewhere', manager=self.u['finance'])
        UserProfile.objects.filter(user=self.u['sales']).update(team=other)
        self.login('manager')
        self.assertEqual(self.client.get(reverse('crm:daily_report', args=[self.rep.pk])).status_code, 403)

    def test_the_report_page_shows_the_text_the_code_and_that_it_is_genuine(self):
        self.login('hr')
        r = self.client.get(reverse('crm:daily_report', args=[self.rep.pk]))
        self.assertContains(r, 'DAILY REPORT | Sales')
        self.assertContains(r, self.rep.code)
        self.assertContains(r, 'Genuine, code matches')
        DailyReport.objects.filter(pk=self.rep.pk).update(text=self.rep.text.replace('Won: 1', 'Won: 9'))
        self.assertContains(self.client.get(reverse('crm:daily_report', args=[self.rep.pk])), 'Does not match its code')

    def test_a_pasted_copy_is_compared_word_for_word(self):
        self.login('hr')
        url = reverse('crm:daily_report', args=[self.rep.pk])
        self.assertContains(self.client.post(url, {'pasted': self.rep.text}), 'It matches this report exactly')
        self.assertContains(self.client.post(url, {'pasted': self.rep.text.replace('Won: 1', 'Won: 5')}), 'Something was changed')

    def test_a_report_is_found_by_its_code_and_a_bad_code_is_said_plainly(self):
        self.login('hr')
        r = self.client.get(reverse('crm:daily_find'), {'code': self.rep.code.lower() + ' '})
        self.assertRedirects(r, reverse('crm:daily_report', args=[self.rep.pk]), fetch_redirect_response=False)
        r = self.client.get(reverse('crm:daily_find'), {'code': 'DR000000-AAAA-00'}, follow=True)
        self.assertContains(r, 'No report has that code')
        self.login('manager')
        self.assertEqual(self.client.get(reverse('crm:daily_find'), {'code': self.rep.code}).status_code, 302)
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:daily_find'), {'code': self.rep.code}).status_code, 302)       # redirect, then refused
        self.assertEqual(self.client.get(reverse('crm:daily_report', args=[self.rep.pk])).status_code, 403)

    def test_the_board_lists_who_has_a_report_for_a_day(self):
        self.login('hr')
        r = self.client.get(reverse('crm:daily_board'), {'day': DAY.isoformat()})
        rows = {x['user'].username: x['report'] for x in r.context['rows']}
        self.assertEqual(rows['sales'].pk, self.rep.pk)
        self.assertIsNone(rows['sales_b'])
        self.assertContains(r, 'Not yet')
        self.assertContains(r, self.rep.code)
        self.assertContains(r, 'Check a report by its code')
        self.login('manager')
        self.assertEqual(self.client.get(reverse('crm:daily_board')).status_code, 200)
        for who in ('sales', 'finance', 'dev'):
            self.login(who)
            self.assertEqual(self.client.get(reverse('crm:daily_board')).status_code, 403, who)

    def test_each_persons_reports_are_listed_day_by_day_on_their_profile(self):
        self.login('hr')
        r = self.client.get(reverse('crm:staff_profile', args=[self.u['sales'].pk]), {'month': '2026-10'})
        self.assertContains(r, 'Daily reports')
        self.assertContains(r, self.rep.code)
        self.assertContains(r, reverse('crm:daily_report', args=[self.rep.pk]))

    def test_reading_reports_is_audited(self):
        self.login('hr')
        self.client.get(reverse('crm:daily_report', args=[self.rep.pk]))
        self.assertTrue(AuditLog.objects.filter(action='dailyreport.viewed', object_repr=self.rep.code, actor_id=self.u['hr'].pk).exists())

    def test_the_menu_has_daily_reports_for_those_who_can_see_a_team(self):
        self.login('manager')
        self.assertIn(reverse('crm:daily_board'), self.client.get(reverse('crm:my_day')).content.decode())
        self.login('sales')
        self.assertNotIn(reverse('crm:daily_board'), self.client.get(reverse('crm:my_day')).content.decode())
