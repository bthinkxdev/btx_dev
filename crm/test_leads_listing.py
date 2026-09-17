"""
Leads-listing behavior:
- Default landing (no ?fu= at all) shows only today's follow-ups, ordered
  earliest-due-first, not the whole pipeline newest-first.
- Explicit ?fu=all still gets everyone with the original newest-first order.
- Lead age is surfaced so nothing quietly ages out of sight.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

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
        self.no_followup = Lead.objects.create(employee=self.rep, name='NoFollowupLead', phone='5')

    def test_default_landing_shows_only_today_earliest_first(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()

        self.assertIn('TodayEarly', content)
        self.assertIn('TodayLate', content)
        self.assertNotIn('OverdueLead', content)
        self.assertNotIn('FutureLead', content)
        self.assertNotIn('NoFollowupLead', content)
        # Earliest-due-first within today, not newest-created-first.
        self.assertLess(content.index('TodayEarly'), content.index('TodayLate'))

    def test_default_landing_has_today_card_active_not_all(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()
        self.assertIn('cmd-strip__card--warning cmd-strip__card--on', content)
        self.assertNotIn('cmd-strip__card--all cmd-strip__card--on', content)

    def test_default_landing_does_not_flag_as_active_filter(self):
        r = self.client.get('/crm/leads/')
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', r.content.decode())

    def test_explicit_fu_all_shows_everyone(self):
        r = self.client.get('/crm/leads/?fu=all')
        content = r.content.decode()
        for name in ('TodayEarly', 'TodayLate', 'OverdueLead', 'FutureLead', 'NoFollowupLead'):
            self.assertIn(name, content)
        self.assertIn('cmd-strip__card--all cmd-strip__card--on', content)
        self.assertIn('<span class="leads-filter-mob-btn__dot"', content)  # fu=all counts as an explicit, active filter

    def test_explicit_fu_overdue_still_works(self):
        r = self.client.get('/crm/leads/?fu=overdue')
        content = r.content.decode()
        self.assertIn('OverdueLead', content)
        self.assertIn('NoFollowupLead', content)  # no follow-up set counts as overdue
        self.assertNotIn('TodayEarly', content)
        self.assertNotIn('FutureLead', content)

    def test_clear_filters_link_points_at_bare_leads_url_which_defaults_to_today(self):
        r = self.client.get('/crm/leads/?fu=all')
        content = r.content.decode()
        self.assertIn('href="/crm/leads/"', content)  # the "Clear" link's target
        r2 = self.client.get('/crm/leads/')
        self.assertEqual(r2.status_code, 200)
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', r2.content.decode())

    def test_lead_age_indicator_present(self):
        r = self.client.get('/crm/leads/?fu=all')
        self.assertIn('old</span>', r.content.decode())
