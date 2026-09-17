"""
Leads-listing behavior:
- Default landing (no ?date_scope= at all) is "Today": leads created today,
  merged with leads whose follow-up is overdue/unscheduled/due today — one
  list, ordered by time (whichever is this row's actual moment: its follow-up,
  or its creation time if it has none). Nothing is silently missed.
- Explicit ?date_scope=yesterday/this_week/this_month are historical windows:
  created OR followed-up within that exact window, no overdue-folding.
- Explicit ?date_scope=all shows everyone, newest-created-first.
- There is no separate command-strip UI any more — "From" (Today/Yesterday/
  This week/This month/All time) is the only period control.
- Lead age is surfaced so nothing quietly ages out of sight.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from crm.models import EmployeeProfile, Lead
from crm.views import _local_today_bounds

User = get_user_model()


class LeadsListingDateScopeDefaultTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('ll_rep', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.client.force_login(self.rep)

        start, end, _ = _local_today_bounds()
        self.today_late = Lead.objects.create(
            employee=self.rep, name='TodayLate', phone='1', next_followup=start + timedelta(hours=18),
        )
        self.today_early = Lead.objects.create(
            employee=self.rep, name='TodayEarly', phone='2', next_followup=start + timedelta(hours=9),
        )
        self.overdue = Lead.objects.create(
            employee=self.rep, name='OverdueLead', phone='3', next_followup=start - timedelta(days=2),
        )
        self.future = Lead.objects.create(
            employee=self.rep, name='FutureLead', phone='4', next_followup=end + timedelta(days=5),
        )
        # Backdate so this lead is NOT "created today" — isolates the follow-up-date
        # exclusion from the separate "created today" inclusion rule.
        Lead.objects.filter(pk=self.future.pk).update(created_at=start - timedelta(days=10))
        self.no_followup = Lead.objects.create(employee=self.rep, name='NoFollowupLead', phone='5')

        self.new_lead_future_fu = Lead.objects.create(
            employee=self.rep, name='NewLeadFutureFu', phone='6', next_followup=end + timedelta(days=5),
        )

        self.yesterday_lead = Lead.objects.create(employee=self.rep, name='YesterdayLead', phone='7')
        Lead.objects.filter(pk=self.yesterday_lead.pk).update(created_at=start - timedelta(hours=6))

    def test_default_landing_merges_todays_leads_and_followups(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()

        # Overdue, today's, and never-scheduled follow-ups must all be visible —
        # none of these may be silently hidden ("don't miss any followup").
        self.assertIn('TodayEarly', content)
        self.assertIn('TodayLate', content)
        self.assertIn('OverdueLead', content)
        self.assertIn('NoFollowupLead', content)
        # A lead created today shows up regardless of its own follow-up date.
        self.assertIn('NewLeadFutureFu', content)
        # An older lead whose follow-up is genuinely in the future is excluded.
        self.assertNotIn('FutureLead', content)
        # An older lead with NO follow-up at all still needs one scheduled now —
        # correctly included even though it wasn't created today.
        self.assertIn('YesterdayLead', content)

    def test_default_landing_orders_by_time(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()
        # Never-scheduled and overdue leads are the most urgent — they must not
        # sort behind leads merely due later today.
        self.assertLess(content.index('NoFollowupLead'), content.index('TodayLate'))
        self.assertLess(content.index('OverdueLead'), content.index('TodayLate'))
        self.assertLess(content.index('TodayEarly'), content.index('TodayLate'))

    def test_new_lead_and_followup_labels_render_correctly(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()

        new_lead_pos = content.index('NewLeadFutureFu')
        new_lead_block = content[new_lead_pos:new_lead_pos + 3000]
        self.assertIn('New Lead', new_lead_block)

        overdue_pos = content.index('OverdueLead')
        overdue_block = content[overdue_pos:overdue_pos + 3000]
        self.assertIn('Follow-up</span>', overdue_block)

    def test_default_landing_has_today_pill_active(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()
        self.assertIn('pill on" href="/crm/leads/?date_scope=today"', content.replace('\n', ' '))
        self.assertNotIn('pill on" href="/crm/leads/?date_scope=all"', content.replace('\n', ' '))

    def test_default_landing_does_not_flag_as_active_filter(self):
        r = self.client.get('/crm/leads/')
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', r.content.decode())

    def test_command_strip_removed(self):
        r = self.client.get('/crm/leads/')
        self.assertNotIn('cmd-strip', r.content.decode())

    def test_explicit_date_scope_yesterday_is_a_pure_historical_window(self):
        r = self.client.get('/crm/leads/?date_scope=yesterday')
        content = r.content.decode()
        # Created yesterday -> included.
        self.assertIn('YesterdayLead', content)
        # None of today's overdue/unscheduled/due-today items leak into "yesterday".
        self.assertNotIn('OverdueLead', content)
        self.assertNotIn('NoFollowupLead', content)
        self.assertNotIn('TodayEarly', content)
        self.assertNotIn('NewLeadFutureFu', content)

    def test_explicit_date_scope_all_shows_everyone(self):
        r = self.client.get('/crm/leads/?date_scope=all')
        content = r.content.decode()
        for name in (
            'TodayEarly', 'TodayLate', 'OverdueLead', 'FutureLead',
            'NoFollowupLead', 'NewLeadFutureFu', 'YesterdayLead',
        ):
            self.assertIn(name, content)
        self.assertIn('<span class="leads-filter-mob-btn__dot"', content)  # explicit filter, not the default

    def test_clear_filters_link_returns_to_default_today_view(self):
        r = self.client.get('/crm/leads/?date_scope=all')
        content = r.content.decode()
        self.assertIn('href="/crm/leads/"', content)  # the "Clear" link's target
        r2 = self.client.get('/crm/leads/')
        self.assertEqual(r2.status_code, 200)
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', r2.content.decode())

    def test_lead_age_indicator_present(self):
        r = self.client.get('/crm/leads/?date_scope=all')
        self.assertIn('old</span>', r.content.decode())
