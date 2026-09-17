"""
Leads-listing behavior:
- Default landing (no ?fu= at all) is "Due Now" — overdue + today + leads with
  no follow-up scheduled yet — so nothing is silently missed, ordered
  earliest-due (and never-scheduled) first. It is NOT "today only": that
  would hide overdue leads and brand-new leads with no follow-up set.
- Explicit ?fu=today / ?fu=overdue narrow to exactly one bucket.
- Explicit ?fu=all still gets everyone with the original newest-first order.
- Lead age is surfaced so nothing quietly ages out of sight.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from crm.models import EmployeeProfile, Lead
from crm.views import _local_today_bounds

User = get_user_model()


class LeadsListingFollowupDefaultTests(TestCase):
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
        # Backdate so this lead is NOT "new today" — isolates the follow-up-date
        # exclusion from the separate "created today" inclusion rule.
        Lead.objects.filter(pk=self.future.pk).update(created_at=start - timedelta(days=10))
        self.no_followup = Lead.objects.create(employee=self.rep, name='NoFollowupLead', phone='5')

        self.new_lead_future_fu = Lead.objects.create(
            employee=self.rep, name='NewLeadFutureFu', phone='6', next_followup=end + timedelta(days=5),
        )

    def test_default_landing_shows_due_now_not_just_today(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()

        # Overdue, today's, and never-scheduled leads must all be visible by default —
        # none of these may be silently hidden ("don't miss any followup").
        self.assertIn('TodayEarly', content)
        self.assertIn('TodayLate', content)
        self.assertIn('OverdueLead', content)
        self.assertIn('NoFollowupLead', content)
        # A lead created today shows up regardless of its own follow-up date.
        self.assertIn('NewLeadFutureFu', content)
        # An older lead whose follow-up is genuinely in the future is excluded.
        self.assertNotIn('FutureLead', content)

    def test_new_lead_and_followup_labels_render_correctly(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()

        new_lead_pos = content.index('NewLeadFutureFu')
        # The "New Lead" label appears within this lead's card, not tied to a follow-up.
        new_lead_block = content[new_lead_pos:new_lead_pos + 3000]
        self.assertIn('New Lead', new_lead_block)

        overdue_pos = content.index('OverdueLead')
        overdue_block = content[overdue_pos:overdue_pos + 3000]
        self.assertIn('Follow-up</span>', overdue_block)

    def test_default_landing_orders_earliest_and_unscheduled_first(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()
        # Never-scheduled and overdue leads are the most urgent — they must not
        # sort behind leads merely due later today.
        self.assertLess(content.index('NoFollowupLead'), content.index('TodayLate'))
        self.assertLess(content.index('OverdueLead'), content.index('TodayLate'))
        self.assertLess(content.index('TodayEarly'), content.index('TodayLate'))

    def test_default_landing_has_due_now_card_active(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()
        self.assertIn('cmd-strip__card--due cmd-strip__card--on', content)
        self.assertNotIn('cmd-strip__card--all cmd-strip__card--on', content)
        self.assertNotIn('cmd-strip__card--danger cmd-strip__card--on', content)
        self.assertNotIn('cmd-strip__card--warning cmd-strip__card--on', content)

    def test_default_landing_does_not_flag_as_active_filter(self):
        r = self.client.get('/crm/leads/')
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', r.content.decode())

    def test_explicit_fu_today_narrows_to_today_only(self):
        r = self.client.get('/crm/leads/?fu=today')
        content = r.content.decode()
        self.assertIn('TodayEarly', content)
        self.assertIn('TodayLate', content)
        self.assertNotIn('OverdueLead', content)
        self.assertNotIn('NoFollowupLead', content)
        self.assertNotIn('FutureLead', content)
        self.assertNotIn('NewLeadFutureFu', content)  # created today, but its own follow-up isn't due today
        self.assertIn('cmd-strip__card--warning cmd-strip__card--on', content)

    def test_explicit_fu_all_shows_everyone(self):
        r = self.client.get('/crm/leads/?fu=all')
        content = r.content.decode()
        for name in ('TodayEarly', 'TodayLate', 'OverdueLead', 'FutureLead', 'NoFollowupLead', 'NewLeadFutureFu'):
            self.assertIn(name, content)
        self.assertIn('cmd-strip__card--all cmd-strip__card--on', content)
        self.assertIn('<span class="leads-filter-mob-btn__dot"', content)  # fu=all counts as an explicit, active filter

    def test_explicit_fu_overdue_still_isolated(self):
        r = self.client.get('/crm/leads/?fu=overdue')
        content = r.content.decode()
        self.assertIn('OverdueLead', content)
        self.assertIn('NoFollowupLead', content)  # no follow-up set counts as overdue
        self.assertNotIn('TodayEarly', content)
        self.assertNotIn('FutureLead', content)
        self.assertNotIn('NewLeadFutureFu', content)
        self.assertIn('cmd-strip__card--danger cmd-strip__card--on', content)

    def test_clear_filters_link_points_at_bare_leads_url_which_defaults_to_due_now(self):
        r = self.client.get('/crm/leads/?fu=all')
        content = r.content.decode()
        self.assertIn('href="/crm/leads/"', content)  # the "Clear" link's target
        r2 = self.client.get('/crm/leads/')
        self.assertEqual(r2.status_code, 200)
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', r2.content.decode())

    def test_lead_age_indicator_present(self):
        r = self.client.get('/crm/leads/?fu=all')
        self.assertIn('old</span>', r.content.decode())
