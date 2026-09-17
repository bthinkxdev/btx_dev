"""
Leads-listing behavior:
- Every period (Today / Yesterday / This week / This month) uses the exact
  same rule: leads created within the window, OR leads with a follow-up
  scheduled exactly within the window. No overdue-folding, no "no follow-up
  set" catch-all — a lead that's simply never had a follow-up scheduled does
  NOT get swept into every period forever just because it has nothing set.
  Overdue tracking is the dedicated Follow-ups page's job, not this list's.
- Default landing (no ?date_scope= at all) is "Today".
- Explicit ?date_scope=all shows everyone, newest-created-first.
- There is no separate command-strip UI — "From" (Today/Yesterday/This
  week/This month/All time) is the only period control.
- Ordering within a period is by time (follow-up time, or creation time for
  rows with none). Lead age is surfaced so nothing quietly ages out of sight.
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
        # Backdate both so only their follow-up date (not creation) puts them in scope.
        Lead.objects.filter(pk__in=[self.today_late.pk, self.today_early.pk]).update(
            created_at=start - timedelta(days=30),
        )

        self.overdue = Lead.objects.create(
            employee=self.rep, name='OverdueLead', phone='3', next_followup=start - timedelta(days=2),
        )
        Lead.objects.filter(pk=self.overdue.pk).update(created_at=start - timedelta(days=30))

        self.future = Lead.objects.create(
            employee=self.rep, name='FutureLead', phone='4', next_followup=end + timedelta(days=5),
        )
        Lead.objects.filter(pk=self.future.pk).update(created_at=start - timedelta(days=10))

        # The exact reported bug: a lead created months ago that has never had a
        # follow-up scheduled must NOT appear in "today" (or any other period)
        # forever just because next_followup is null.
        self.old_no_followup = Lead.objects.create(
            employee=self.rep, name='OldNeverFollowedUp', phone='5',
        )
        Lead.objects.filter(pk=self.old_no_followup.pk).update(created_at=start - timedelta(days=120))

        # Created today with no follow-up yet -> included via creation date, not
        # because it's unscheduled.
        self.new_no_followup = Lead.objects.create(
            employee=self.rep, name='NewLeadNoFollowup', phone='6',
        )

        self.new_lead_future_fu = Lead.objects.create(
            employee=self.rep, name='NewLeadFutureFu', phone='7', next_followup=end + timedelta(days=5),
        )

        self.yesterday_lead = Lead.objects.create(employee=self.rep, name='YesterdayLead', phone='8')
        Lead.objects.filter(pk=self.yesterday_lead.pk).update(created_at=start - timedelta(hours=6))

    def test_default_landing_shows_only_todays_leads_and_followups(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()

        # Follow-ups scheduled exactly today -> included.
        self.assertIn('TodayEarly', content)
        self.assertIn('TodayLate', content)
        # Leads created today (regardless of their own follow-up date) -> included.
        self.assertIn('NewLeadNoFollowup', content)
        self.assertIn('NewLeadFutureFu', content)

        # An overdue follow-up from an old lead must NOT leak into "today" —
        # that's the Follow-ups page's job, not this list's.
        self.assertNotIn('OverdueLead', content)
        # A lead that's never had a follow-up scheduled, and wasn't created
        # today, must not be swept in just because next_followup is null —
        # this is the exact reported bug.
        self.assertNotIn('OldNeverFollowedUp', content)
        # A lead created yesterday with no follow-up at all -> not today's business.
        self.assertNotIn('YesterdayLead', content)
        # An old lead whose follow-up is genuinely in the future -> not due yet.
        self.assertNotIn('FutureLead', content)

    def test_default_landing_orders_by_time(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()
        self.assertLess(content.index('TodayEarly'), content.index('TodayLate'))

    def test_new_lead_and_followup_labels_render_correctly(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()

        new_lead_pos = content.index('NewLeadNoFollowup')
        new_lead_block = content[new_lead_pos:new_lead_pos + 3000]
        self.assertIn('New Lead', new_lead_block)
        self.assertNotIn('Follow-up</span>', new_lead_block)  # no follow-up scheduled -> no follow-up badge

        fu_pos = content.index('TodayEarly')
        fu_block = content[fu_pos:fu_pos + 3000]
        self.assertIn('Follow-up</span>', fu_block)
        self.assertNotIn('New Lead', fu_block)  # created 30 days ago -> not a "new lead"

    def test_default_landing_has_today_pill_active(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode().replace('\n', ' ')
        self.assertIn('pill on" href="/crm/leads/?date_scope=today"', content)
        self.assertNotIn('pill on" href="/crm/leads/?date_scope=all"', content)

    def test_default_landing_does_not_flag_as_active_filter(self):
        r = self.client.get('/crm/leads/')
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', r.content.decode())

    def test_command_strip_removed(self):
        r = self.client.get('/crm/leads/')
        self.assertNotIn('cmd-strip', r.content.decode())

    def test_explicit_date_scope_yesterday_is_a_pure_historical_window(self):
        r = self.client.get('/crm/leads/?date_scope=yesterday')
        content = r.content.decode()
        self.assertIn('YesterdayLead', content)
        # None of today's items, and no null-follow-up leads, leak into "yesterday".
        self.assertNotIn('OverdueLead', content)
        self.assertNotIn('OldNeverFollowedUp', content)
        self.assertNotIn('TodayEarly', content)
        self.assertNotIn('NewLeadNoFollowup', content)
        self.assertNotIn('NewLeadFutureFu', content)

    def test_explicit_date_scope_all_shows_everyone(self):
        r = self.client.get('/crm/leads/?date_scope=all')
        content = r.content.decode()
        for name in (
            'TodayEarly', 'TodayLate', 'OverdueLead', 'FutureLead', 'OldNeverFollowedUp',
            'NewLeadNoFollowup', 'NewLeadFutureFu', 'YesterdayLead',
        ):
            self.assertIn(name, content)
        self.assertIn('<span class="leads-filter-mob-btn__dot"', content)  # explicit filter, not the default

        # Badges only mean something inside a specific period — hidden under "All time".
        old_pos = content.index('OldNeverFollowedUp')
        self.assertNotIn('New Lead', content[old_pos:old_pos + 3000])

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
