"""Working time: login, breaks, logout, the 12-hour idle rule, midnight, devices, and who can see what."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import BreakPeriod, CompanySettings, ExportLog, UserSession, WorkPeriod
from crm.modules.worktime import services as wt

from .base import PASSWORD, CrmTestCase

DAY = date(2026, 11, 10)


def at(h, m=0, day=DAY):
    return timezone.make_aware(datetime.combine(day, time(h, m)))


def sign_in(client, username):
    return client.post(reverse('crm:login'), {'username': username, 'password': PASSWORD})


class WhoIsTrackedTests(CrmTestCase):
    def test_everyone_except_administrators_and_the_service_account(self):
        self.assertTrue(wt.tracked(self.u['sales']))
        self.assertTrue(wt.tracked(self.u['manager']))
        self.assertTrue(wt.tracked(self.u['hr']))
        self.assertFalse(wt.tracked(self.u['owner']))
        su = get_user_model().objects.create_superuser('root', 'root@example.test', PASSWORD)
        self.assertFalse(wt.tracked(su))
        self.assertFalse(wt.tracked(get_user_model().objects.get(username=wt.MCP_USERNAME)))
        self.u['sales_b'].is_active = False
        self.assertFalse(wt.tracked(self.u['sales_b']))

    def test_the_tracked_list_leaves_out_administrators(self):
        names = {u.username for u in wt.tracked_users()}
        self.assertIn('sales', names)
        self.assertNotIn('owner', names)
        self.assertNotIn(wt.MCP_USERNAME, names)


class DayMathTests(CrmTestCase):
    def test_working_time_is_login_to_logout_minus_breaks(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.start_break(u, at(13))
        wt.end_break(u, at(13, 45))
        wt.start_break(u, at(16))
        wt.end_break(u, at(16, 15))
        wt.on_logout(u, now=at(18))
        s = wt.day_summary(u, DAY, at(19))
        self.assertEqual(s['worked'], (9 * 60 - 45 - 15) * 60)           # 9 hours minus an hour of breaks
        self.assertEqual((s['break_seconds'], s['break_count'], s['longest_break']), (3600, 2, 45 * 60))
        self.assertEqual((s['first_in'], s['last_out'], s['state']), (at(9), at(18), 'off'))
        self.assertEqual(s['worked_text'], '8h 00m')

    def test_time_on_a_break_is_not_work_even_while_it_is_still_running(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.start_break(u, at(12))
        s = wt.day_summary(u, DAY, at(12, 30))
        self.assertEqual(s['state'], 'break')
        self.assertEqual((s['worked'], s['break_seconds']), (3 * 3600, 30 * 60))

    def test_logging_out_on_a_break_ends_the_break_too(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.start_break(u, at(12))
        wt.on_logout(u, now=at(13))
        brk = BreakPeriod.objects.get()
        self.assertEqual((brk.ended_at, brk.end_reason), (at(13), 'logout'))
        self.assertEqual(wt.day_summary(u, DAY, at(14))['worked'], 3 * 3600)

    def test_the_break_buttons_refuse_what_makes_no_sense(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        with self.assertRaises(wt.WorkTimeError):
            wt.end_break(u, at(10))                                        # not on a break
        wt.start_break(u, at(10))
        with self.assertRaises(wt.WorkTimeError):
            wt.start_break(u, at(10, 5))                                   # already on one
        with self.assertRaises(wt.WorkTimeError):
            wt.start_break(self.u['owner'], at(10))                        # administrators are not tracked

    def test_administrators_never_get_a_record(self):
        wt.on_login(self.u['owner'], at(9))
        wt.heartbeat(self.u['owner'], at(10))
        wt.on_logout(self.u['owner'], now=at(18))
        self.assertFalse(WorkPeriod.objects.filter(user=self.u['owner']).exists())

    def test_there_is_only_ever_one_open_day_per_person(self):
        u = self.u['sales']
        a = wt._start(u, at(9), 'login')
        b = wt._start(u, at(9, 1), 'login')                                # a second request racing the first
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(WorkPeriod.objects.filter(user=u).count(), 1)
        wt.on_login(u, at(9, 2))
        self.assertEqual(WorkPeriod.objects.filter(user=u).count(), 1)

    def test_a_second_login_the_same_day_continues_the_same_day(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.on_login(u, at(11))
        self.assertEqual(WorkPeriod.objects.filter(user=u).count(), 1)
        self.assertEqual(wt.open_period(u).started_at, at(9))

    def test_login_again_after_logout_starts_a_second_period_and_both_count(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.on_logout(u, now=at(13))
        wt.on_login(u, at(14))
        wt.on_logout(u, now=at(18))
        s = wt.day_summary(u, DAY, at(19))
        self.assertEqual((len(s['periods']), s['worked']), (2, 8 * 3600))
        self.assertEqual((s['first_in'], s['last_out']), (at(9), at(18)))


class MidnightTests(CrmTestCase):
    def test_a_day_still_open_at_midnight_is_closed_at_the_last_activity(self):
        u = self.u['sales']
        wt.on_login(u, at(21))
        wt.heartbeat(u, at(23, 40))
        wt.heartbeat(u, at(9, day=DAY + timedelta(days=1)))                # first activity the next morning
        old, new = WorkPeriod.objects.order_by('started_at')
        self.assertEqual((old.ended_at, old.end_reason, old.day), (at(23, 40), 'day_end', DAY))
        self.assertEqual((new.started_at, new.start_source, new.day), (at(9, day=DAY + timedelta(days=1)), 'day_start', DAY + timedelta(days=1)))
        self.assertEqual(wt.day_summary(u, DAY, at(9, day=DAY + timedelta(days=1)))['worked'], (2 * 60 + 40) * 60)   # not the whole night

    def test_the_job_closes_a_day_that_rolled_over_without_waiting_twelve_hours(self):
        u = self.u['sales']
        wt.on_login(u, at(21))
        wt.heartbeat(u, at(23, 40))
        n = wt.close_stale(at(0, 30, day=DAY + timedelta(days=1)))         # 50 minutes after the last activity
        self.assertEqual(n, 1)
        p = WorkPeriod.objects.get()
        self.assertEqual((p.ended_at, p.end_reason), (at(23, 40), 'day_end'))


class IdleRuleTests(CrmTestCase):
    def test_the_default_is_twelve_hours_not_thirty_minutes(self):
        self.assertEqual(CompanySettings.get().idle_timeout_minutes, 720)
        self.assertEqual(wt.idle_limit(), timedelta(hours=12))

    def test_a_person_idle_for_hours_is_not_signed_out(self):
        sign_in(self.client, 'sales')
        for hours in (0.7, 3, 11):
            s = self.client.session
            s['crm_last_activity'] = (timezone.now() - timedelta(hours=hours)).timestamp()
            s.save()
            self.assertEqual(self.client.get(reverse('crm:my_time')).status_code, 200, hours)

    def test_twelve_hours_idle_signs_them_out_and_the_day_ends_at_their_last_activity(self):
        sign_in(self.client, 'sales')
        last = timezone.now() - timedelta(hours=13)
        WorkPeriod.objects.update(started_at=timezone.now() - timedelta(hours=14), last_activity_at=last)   # as if they had worked since then
        s = self.client.session
        s['crm_last_activity'] = last.timestamp()
        s.save()
        r = self.client.get(reverse('crm:my_time'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/crm/login', r['Location'])
        p = WorkPeriod.objects.get(user=self.u['sales'])
        self.assertEqual(p.end_reason, 'idle')
        self.assertAlmostEqual(p.ended_at.timestamp(), last.timestamp(), delta=2)       # not 12 hours of sleep counted as work
        self.assertTrue(UserSession.objects.filter(user=self.u['sales'], ended_at__isnull=False).exists())

    def test_the_idle_limit_can_still_be_changed_by_an_administrator(self):
        CompanySettings.objects.update(idle_timeout_minutes=240)
        sign_in(self.client, 'sales')
        s = self.client.session
        s['crm_last_activity'] = (timezone.now() - timedelta(hours=5)).timestamp()
        s.save()
        self.assertEqual(self.client.get(reverse('crm:my_time')).status_code, 302)

    def test_the_job_signs_out_days_idle_twelve_hours_at_the_last_activity(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.start_break(u, at(11))
        n = wt.close_stale(at(23, 30))                                     # 12.5 hours after the last activity
        self.assertEqual(n, 1)
        p = WorkPeriod.objects.get()
        self.assertEqual((p.ended_at, p.end_reason), (at(11), 'idle'))
        self.assertEqual(BreakPeriod.objects.get().ended_at, at(11))
        self.assertEqual(wt.close_stale(at(23, 45)), 0)                    # nothing left to close

    def test_a_busy_person_is_left_alone_by_the_job(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.heartbeat(u, at(17))
        self.assertEqual(wt.close_stale(at(17, 15)), 0)
        self.assertTrue(wt.open_period(u))


class SignInFlowTests(CrmTestCase):
    def test_signing_in_starts_the_day_and_signing_out_ends_it(self):
        r = sign_in(self.client, 'sales')
        self.assertEqual(r.status_code, 302)
        p = WorkPeriod.objects.get(user=self.u['sales'])
        self.assertTrue(p.is_open)
        self.assertEqual(p.start_source, 'login')
        self.client.post(reverse('crm:logout'))
        p.refresh_from_db()
        self.assertEqual(p.end_reason, 'logout')

    def test_administrators_sign_in_without_being_tracked(self):
        sign_in(self.client, 'owner')
        self.assertFalse(WorkPeriod.objects.exists())

    def test_logging_out_on_one_device_keeps_the_day_open_while_another_is_in_use(self):
        u = self.u['sales']
        sign_in(self.client, 'sales')
        UserSession.objects.create(user=u, session_key='phone-session-key', last_seen_at=timezone.now() - timedelta(minutes=5))
        self.client.post(reverse('crm:logout'))
        self.assertTrue(wt.open_period(u))

    def test_a_forgotten_old_device_does_not_keep_the_day_open(self):
        u = self.u['sales']
        sign_in(self.client, 'sales')
        UserSession.objects.create(user=u, session_key='old-laptop', last_seen_at=timezone.now() - timedelta(hours=14))
        self.client.post(reverse('crm:logout'))
        self.assertIsNone(wt.open_period(u))

    def test_a_device_still_in_use_brings_the_day_back(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.on_logout(u, now=at(10))
        wt.heartbeat(u, at(10, 5))
        p = wt.open_period(u)
        self.assertEqual((p.start_source, p.started_at), ('resume', at(10, 5)))

    def test_a_tracking_problem_never_stops_anyone_working(self):
        sign_in(self.client, 'sales')
        s = self.client.session
        s['crm_last_activity'] = (timezone.now() - timedelta(minutes=5)).timestamp()
        s.save()
        with patch('crm.modules.worktime.services.heartbeat', side_effect=RuntimeError('db down')):
            self.assertEqual(self.client.get(reverse('crm:my_time')).status_code, 200)


class PageTests(CrmTestCase):
    def test_my_time_shows_the_day_and_the_break_button_locks_the_screen(self):
        sign_in(self.client, 'sales')
        r = self.client.get(reverse('crm:my_time'))
        self.assertContains(r, 'My working time')
        self.assertContains(r, 'Take a break')
        r = self.client.post(reverse('crm:break_start'), HTTP_REFERER='http://testserver' + reverse('crm:my_time'))
        self.assertRedirects(r, reverse('crm:on_break'))
        r = self.client.get(reverse('crm:on_break'))
        self.assertContains(r, 'You are on a break')
        self.assertContains(r, 'Back to work')
        r = self.client.post(reverse('crm:break_end'), follow=True)
        self.assertRedirects(r, reverse('crm:my_time'))                       # back to the page they were on
        self.assertContains(r, 'Welcome back')
        self.assertEqual(BreakPeriod.objects.count(), 1)

    def test_the_header_clock_is_for_tracked_people_only(self):
        sign_in(self.client, 'sales')
        self.assertContains(self.client.get(reverse('crm:my_time')), 'class="work-clock')
        self.client.post(reverse('crm:logout'))
        sign_in(self.client, 'owner')
        r = self.client.get(reverse('crm:my_time'))
        self.assertNotContains(r, 'class="work-clock')
        self.assertContains(r, 'not tracked')

    def test_the_break_buttons_need_a_post_and_a_token(self):
        from django.test import Client
        sign_in(self.client, 'sales')
        self.assertEqual(self.client.get(reverse('crm:break_start')).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.u['sales'])
        strict.post(reverse('crm:break_start'))
        self.assertFalse(BreakPeriod.objects.exists())

    def test_the_manager_sees_the_team_but_not_administrators(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.on_logout(u, now=at(11))
        self.login('manager')
        r = self.client.get(reverse('crm:team_time'), {'day': DAY.isoformat()})
        self.assertContains(r, 'Team working time')
        self.assertContains(r, 'Sales')                                     # Sales (the executive) is in the manager's team
        self.assertNotContains(r, 'Owner')
        row = next(x for x in r.context['rows'] if x['user'].pk == u.pk)
        self.assertEqual(row['s']['worked_text'], '2h 00m')

    def test_someone_working_right_now_shows_as_working_and_someone_on_a_break_as_on_break(self):
        wt.on_login(self.u['sales'], at(9))
        wt.on_login(self.u['sales_b'], at(9))
        wt.start_break(self.u['sales_b'], at(10))
        self.login('manager')
        r = self.client.get(reverse('crm:team_time'), {'day': DAY.isoformat()})
        states = {x['user'].pk: x['state'] for x in r.context['rows']}
        self.assertEqual(states[self.u['sales'].pk], 'working')
        self.assertEqual(states[self.u['sales_b'].pk], 'break')
        self.assertEqual(r.context['rows'][0]['user'].pk, self.u['sales_b'].pk)       # people on a break are listed first

    def test_someone_who_never_logged_in_shows_as_no_login(self):
        self.login('manager')
        r = self.client.get(reverse('crm:team_time'))
        row = next(x for x in r.context['rows'] if x['user'].pk == self.u['sales_b'].pk)
        self.assertEqual(row['state'], 'none')
        self.assertContains(r, 'No login')

    def test_a_long_break_is_flagged(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.start_break(u, at(12))
        wt.end_break(u, at(13, 30))
        wt.on_logout(u, now=at(18))
        self.login('manager')
        r = self.client.get(reverse('crm:team_time'), {'day': DAY.isoformat()})
        row = next(x for x in r.context['rows'] if x['user'].pk == u.pk)
        self.assertTrue(row['long_break'])
        self.assertContains(r, 'Long break')

    def test_who_may_open_the_team_pages(self):
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:team_time')).status_code, 403)
        self.assertEqual(self.client.get(reverse('crm:person_time', args=[self.u['sales_b'].pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('crm:person_time', args=[self.u['sales'].pk])).status_code, 200)      # their own is fine
        self.login('finance')
        self.assertEqual(self.client.get(reverse('crm:team_time')).status_code, 403)
        self.login('hr')
        r = self.client.get(reverse('crm:team_time'))
        self.assertEqual(r.status_code, 200)
        names = {x['user'].username for x in r.context['rows']}
        self.assertTrue({'sales', 'sales_b', 'finance', 'manager'} <= names)           # company scope sees everyone tracked
        self.assertNotIn('owner', names)

    def test_a_manager_cannot_open_someone_outside_the_team(self):
        self.login('manager')
        self.assertEqual(self.client.get(reverse('crm:person_time', args=[self.u['sales'].pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('crm:person_time', args=[self.u['finance'].pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('crm:person_time', args=[self.u['owner'].pk])).status_code, 403)

    def test_the_person_page_lists_days_and_totals(self):
        u = self.u['sales']
        today = wt.local_day()
        day_start = timezone.make_aware(datetime.combine(today, time(9, 0)))
        WorkPeriod.objects.create(user=u, day=today, started_at=day_start, ended_at=day_start + timedelta(hours=3),
                                  last_activity_at=day_start + timedelta(hours=3), end_reason='logout')
        self.login('manager')
        r = self.client.get(reverse('crm:person_time', args=[u.pk]))
        self.assertContains(r, 'Recent days')
        self.assertEqual(r.context['week_text'], '3h 00m')
        self.assertEqual(r.context['month_text'], '3h 00m')
        self.assertEqual(r.context['rows'][0]['day'], today)
        self.assertEqual(r.context['rows'][0]['s']['worked_text'], '3h 00m')

    def test_a_day_in_the_past_can_be_viewed(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.on_logout(u, now=at(17))
        self.login('manager')
        r = self.client.get(reverse('crm:team_time'), {'day': DAY.isoformat()})
        row = next(x for x in r.context['rows'] if x['user'].pk == u.pk)
        self.assertEqual(row['s']['worked_text'], '8h 00m')
        self.assertEqual(row['state_text'], 'Worked')

    def test_the_export_is_for_those_allowed_and_is_logged(self):
        u = self.u['sales']
        wt.on_login(u, at(9))
        wt.start_break(u, at(13))
        wt.end_break(u, at(13, 30))
        wt.on_logout(u, now=at(18))
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:team_time'), {'export': 'csv'}).status_code, 403)
        self.login('manager')
        r = self.client.get(reverse('crm:team_time'), {'export': 'csv', 'from': DAY.isoformat(), 'to': DAY.isoformat()})
        self.assertEqual(r.status_code, 200)
        text = r.content.decode('utf-8-sig')
        self.assertIn('Working time (h:mm)', text)
        self.assertIn('Sales', text)
        self.assertIn('8:30', text)                                          # 9 hours minus a 30 minute break
        self.assertTrue(ExportLog.objects.filter(user=self.u['manager'], module='attendance').exists())

    def test_the_menu_shows_my_time_to_everyone_and_team_time_to_managers(self):
        self.login('sales')
        html = self.client.get(reverse('crm:my_day')).content.decode()
        self.assertIn(reverse('crm:my_time'), html)
        self.assertNotIn(reverse('crm:team_time'), html)
        self.login('manager')
        self.assertIn(reverse('crm:team_time'), self.client.get(reverse('crm:my_day')).content.decode())

    def test_the_stale_job_is_registered(self):
        from crm.core import jobs
        self.assertIn('worktime.close_stale', jobs.JOBS)
        self.assertEqual(jobs.run_job('worktime.close_stale', force=True)['status'], 'succeeded')


class DefaultGrantTests(CrmTestCase):
    def test_sales_managers_see_their_team_and_executives_only_themselves(self):
        from crm.models import Role
        self.assertEqual(Role.objects.get(code='sales_manager').grants['attendance']['view'], 'team')
        self.assertEqual(Role.objects.get(code='sales_executive').grants['attendance']['view'], 'own')


class ReportTests(CrmTestCase):
    """The period report: days worked, hours, breaks, usual times, and days never logged in."""
    START = date(2026, 11, 2)           # a Monday

    def work(self, user, day, start_h, end_h, break_min=0):
        wt.on_login(user, at(start_h, day=day))
        if break_min:
            wt.start_break(user, at(13, day=day))
            wt.end_break(user, at(13, day=day) + timedelta(minutes=break_min))
        wt.on_logout(user, now=at(end_h, day=day))

    def test_the_report_adds_up_each_person(self):
        u = self.u['sales']
        self.work(u, self.START, 9, 18, break_min=60)                                  # 8h
        self.work(u, self.START + timedelta(days=1), 10, 16)                           # 6h
        rep = wt.report([u], self.START, self.START + timedelta(days=5), now=at(20, day=self.START + timedelta(days=6)))
        row = rep['rows'][0]
        self.assertEqual((row['days'], row['worked_text'], row['avg_worked'], row['break_text'], row['breaks']), (2, '14h 00m', '7h 00m', '1h 00m', 1))
        self.assertEqual((row['avg_in'], row['avg_out'], row['longest_break']), ('09:30', '17:00', '1h 00m'))
        self.assertEqual(rep['working_days'], 5)                                       # Monday to Friday: weekends are holidays
        self.assertEqual(row['absent'], 3)                                             # the other three working days

    def test_weekends_and_holidays_are_not_counted_as_missed_days(self):
        from crm.models import Holiday
        Holiday.objects.create(holiday_date=self.START + timedelta(days=2), name='Local holiday')
        u = self.u['sales']
        rep = wt.report([u], self.START, self.START + timedelta(days=6), now=at(20, day=self.START + timedelta(days=7)))
        self.assertEqual(rep['working_days'], 4)                                       # 5 weekdays minus the holiday
        self.assertEqual(rep['rows'][0]['absent'], 4)
        self.assertNotIn(self.START + timedelta(days=6), rep['rows'][0]['absent_days'])   # the Sunday
        self.assertNotIn(self.START + timedelta(days=5), rep['rows'][0]['absent_days'])   # and the Saturday

    def test_days_in_the_future_are_not_missed_yet(self):
        rep = wt.report([self.u['sales']], self.START, self.START + timedelta(days=5), now=at(20, day=self.START + timedelta(days=1)))
        self.assertEqual(rep['working_days'], 2)

    def test_a_person_with_no_time_shows_dashes_not_errors(self):
        rep = wt.report([self.u['sales_b']], self.START, self.START + timedelta(days=2), now=at(20, day=self.START + timedelta(days=3)))
        row = rep['rows'][0]
        self.assertEqual((row['days'], row['avg_worked'], row['avg_in'], row['avg_out']), (0, '—', '—', '—'))

    def test_the_report_page_for_a_manager(self):
        self.work(self.u['sales'], self.START, 9, 17)
        self.login('manager')
        r = self.client.get(reverse('crm:team_time_report'), {'from': self.START.isoformat(), 'to': (self.START + timedelta(days=5)).isoformat()})
        self.assertContains(r, 'Working time report')
        row = next(x for x in r.context['rep']['rows'] if x['user'].pk == self.u['sales'].pk)
        self.assertEqual(row['worked_text'], '8h 00m')
        names = {x['user'].username for x in r.context['rep']['rows']}
        self.assertNotIn('owner', names)
        self.assertNotIn('finance', names)                                             # not in the manager's team
        self.assertEqual(self.client.get(reverse('crm:team_time_report'), {'range': 'last_month'}).status_code, 200)
        self.assertEqual(self.client.get(reverse('crm:team_time_report'), {'range': 'week'}).status_code, 200)

    def test_the_report_is_only_for_those_who_see_a_team(self):
        for who in ('sales', 'finance'):
            self.login(who)
            self.assertEqual(self.client.get(reverse('crm:team_time_report')).status_code, 403, who)
        self.login('hr')
        r = self.client.get(reverse('crm:team_time_report'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('finance', {x['user'].username for x in r.context['rep']['rows']})   # company scope

    def test_the_report_csv_is_logged_and_safe(self):
        self.work(self.u['sales'], self.START, 9, 17, break_min=30)
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:team_time_report'), {'export': 'csv'}).status_code, 403)
        self.login('manager')
        r = self.client.get(reverse('crm:team_time_report'), {'from': self.START.isoformat(), 'to': (self.START + timedelta(days=5)).isoformat(),
                                                              'export': 'csv'})
        self.assertEqual(r.status_code, 200)
        text = r.content.decode('utf-8-sig')
        self.assertIn('Days not logged in', text)
        self.assertIn('7:30', text)
        self.assertTrue(ExportLog.objects.filter(user=self.u['manager'], module='attendance').exists())

    def test_a_huge_range_is_capped(self):
        self.login('manager')
        r = self.client.get(reverse('crm:team_time_report'), {'from': '2020-01-01', 'to': wt.local_day().isoformat()})
        self.assertLessEqual((r.context['end'] - r.context['start']).days, 92)


class BreakLockTests(CrmTestCase):
    """On a break, nothing in the CRM works until Back to work is pressed."""

    def on_a_break(self, who='sales'):
        sign_in(self.client, who)
        self.client.post(reverse('crm:break_start'))

    def test_every_page_sends_them_to_the_break_screen(self):
        self.on_a_break()
        for name in ('my_day', 'lead_list', 'client_list', 'my_time', 'followup_calendar', 'deliverable_mine', 'notification_list', 'home'):
            r = self.client.get(reverse(f'crm:{name}'))
            self.assertRedirects(r, reverse('crm:on_break'), fetch_redirect_response=False, msg_prefix=name)

    def test_the_break_screen_is_blank_apart_from_one_button(self):
        self.on_a_break()
        r = self.client.get(reverse('crm:on_break'))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn(reverse('crm:break_end'), html)
        self.assertNotIn('sidebar', html.lower())                              # no menu to click
        self.assertNotIn(reverse('crm:lead_list'), html)
        self.assertEqual(html.count('<button'), 1)

    def test_actions_are_refused_on_the_server_not_just_hidden(self):
        from crm.models import Lead, LeadSource
        lead = Lead.objects.create(name='Ravi', phone='+919800000009', owner=self.u['sales'], created_by=self.u['sales'], stage='contacted',
                                   source=LeadSource.objects.first(), next_follow_up_at=timezone.now() + timedelta(days=1))
        self.on_a_break()
        r = self.client.post(reverse('crm:lead_edit', args=[lead.pk]), {'name': 'Changed'})
        self.assertRedirects(r, reverse('crm:on_break'), fetch_redirect_response=False)
        lead.refresh_from_db()
        self.assertEqual(lead.name, 'Ravi')

    def test_background_requests_get_a_clear_locked_answer(self):
        self.on_a_break()
        r = self.client.get(reverse('crm:search'), {'q': 'x'}, HTTP_X_REQUESTED_WITH='fetch')
        self.assertEqual(r.status_code, 423)
        self.assertTrue(r.json()['on_break'])
        r = self.client.post(reverse('crm:lead_list'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 423)

    def test_the_way_back_and_signing_out_stay_open(self):
        self.on_a_break()
        for name in ('manifest', 'service_worker'):
            self.assertNotEqual(self.client.get(reverse(f'crm:{name}')).status_code, 302, name)
        r = self.client.post(reverse('crm:break_end'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse('crm:my_day')).status_code, 200)       # working again
        self.client.post(reverse('crm:break_start'))
        r = self.client.post(reverse('crm:logout'))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r['Location'].startswith(reverse('crm:login')))              # the login page, with the day's report to copy
        self.assertIsNone(wt.open_period(self.u['sales']))                              # signing out ended the break and the day

    def test_back_to_work_returns_to_the_page_they_were_trying_to_open(self):
        self.on_a_break()
        self.client.get(reverse('crm:client_list'))                                      # turned away, remembered
        r = self.client.post(reverse('crm:break_end'))
        self.assertRedirects(r, reverse('crm:client_list'), fetch_redirect_response=False)

    def test_an_outside_address_is_never_used_as_the_place_to_return_to(self):
        self.on_a_break()
        s = self.client.session
        s['crm_break_next'] = '//evil.example/steal'
        s.save()
        r = self.client.post(reverse('crm:break_end'))
        self.assertRedirects(r, reverse('crm:home'), fetch_redirect_response=False)

    def test_the_lock_follows_the_person_to_every_device(self):
        self.on_a_break()
        from django.test import Client
        phone = Client()
        sign_in(phone, 'sales')                                                          # signing in again does not lift it
        r = phone.get(reverse('crm:my_day'))
        self.assertRedirects(r, reverse('crm:on_break'), fetch_redirect_response=False)

    def test_people_who_are_not_on_a_break_are_never_held(self):
        sign_in(self.client, 'sales')
        self.assertEqual(self.client.get(reverse('crm:my_day')).status_code, 200)
        r = self.client.get(reverse('crm:on_break'))
        self.assertRedirects(r, reverse('crm:home'), fetch_redirect_response=False)
        from django.test import Client
        admin = Client()
        sign_in(admin, 'owner')
        self.assertEqual(admin.get(reverse('crm:my_day')).status_code, 200)

    def test_one_persons_break_does_not_lock_anyone_else(self):
        from django.test import Client
        self.on_a_break('sales')
        other = Client()
        sign_in(other, 'hr')
        self.assertEqual(other.get(reverse('crm:my_day')).status_code, 200)

    def test_a_fault_in_the_check_never_locks_people_out(self):
        sign_in(self.client, 'sales')
        with patch('crm.modules.worktime.services.current_break', side_effect=RuntimeError('db down')):
            self.assertEqual(self.client.get(reverse('crm:my_day')).status_code, 200)

    def test_twelve_hours_on_a_break_still_signs_them_out_and_ends_the_break_there(self):
        self.on_a_break()
        last = timezone.now() - timedelta(hours=13)
        WorkPeriod.objects.update(started_at=timezone.now() - timedelta(hours=15), last_activity_at=last)
        BreakPeriod.objects.update(started_at=timezone.now() - timedelta(hours=14))
        s = self.client.session
        s['crm_last_activity'] = last.timestamp()
        s.save()
        r = self.client.get(reverse('crm:my_day'))
        self.assertIn('/crm/login', r['Location'])
        self.assertEqual(BreakPeriod.objects.get().end_reason, 'idle')
