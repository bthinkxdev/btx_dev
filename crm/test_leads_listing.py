"""
Leads-listing behavior:
- Default landing (no ?date_scope= at all) is "All time" — every lead in
  scope, newest-created-first. Nothing is hidden by default.
- Every explicit period (Today / Yesterday / This week / This month) uses
  the exact same rule: leads created within the window, OR leads with a
  follow-up scheduled exactly within the window. No overdue-folding, no
  "no follow-up set" catch-all — a lead that's simply never had a
  follow-up scheduled does NOT get swept into every period forever just
  because it has nothing set. Overdue tracking is the dedicated
  Follow-ups page's job, not this list's.
- There is no separate command-strip UI — "From" (Today/Yesterday/This
  week/This month/All time) is the only period control.
- Within an explicit period, ordering is by time (follow-up time, or
  creation time for rows with none). Lead age is surfaced so nothing
  quietly ages out of sight.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from crm.models import Activity, EmployeeProfile, Lead
from crm.views import _local_today_bounds

User = get_user_model()


def _schedule_next_activity(lead, when):
    """Test helper: replace a lead's auto-created 'fresh lead' Activity
    (crm.signals always makes one at scheduled_at=now()) with one at an
    explicit time — this is what Lead.next_activity is the single source of
    truth for everywhere now, so tests drive it the same way production does."""
    Activity.objects.filter(lead=lead).update(status=Activity.Status.CANCELLED)
    act = Activity.objects.create(
        lead=lead, owner=lead.employee, activity_type=Activity.Type.CALL,
        scheduled_at=when, status=Activity.Status.PENDING,
    )
    Lead.objects.filter(pk=lead.pk).update(next_activity=act)
    lead.refresh_from_db()
    return act


def _clear_next_activity(lead):
    """Test helper: simulate a lead with genuinely no next action set (the
    Workbench's 'No Next Action' case) — cancels the auto-created activity
    instead of leaving it pending at 'now', which would otherwise always
    count as 'due today'."""
    Activity.objects.filter(lead=lead).update(status=Activity.Status.CANCELLED)
    Lead.objects.filter(pk=lead.pk).update(next_activity=None)
    lead.refresh_from_db()


class LeadsListingDateScopeDefaultTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('ll_rep', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.client.force_login(self.rep)

        start, end, _ = _local_today_bounds()

        self.today_late = Lead.objects.create(employee=self.rep, name='TodayLate', phone='1')
        _schedule_next_activity(self.today_late, start + timedelta(hours=18))
        self.today_early = Lead.objects.create(employee=self.rep, name='TodayEarly', phone='2')
        _schedule_next_activity(self.today_early, start + timedelta(hours=9))
        # Backdate both so only their follow-up date (not creation) puts them in scope.
        Lead.objects.filter(pk__in=[self.today_late.pk, self.today_early.pk]).update(
            created_at=start - timedelta(days=30),
        )

        self.overdue = Lead.objects.create(employee=self.rep, name='OverdueLead', phone='3')
        _schedule_next_activity(self.overdue, start - timedelta(days=2))
        Lead.objects.filter(pk=self.overdue.pk).update(created_at=start - timedelta(days=30))

        self.future = Lead.objects.create(employee=self.rep, name='FutureLead', phone='4')
        _schedule_next_activity(self.future, end + timedelta(days=5))
        Lead.objects.filter(pk=self.future.pk).update(created_at=start - timedelta(days=10))

        # The exact reported bug: a lead created months ago that has never had a
        # follow-up scheduled must NOT appear in "today" (or any other period)
        # forever just because next_activity is null.
        self.old_no_followup = Lead.objects.create(
            employee=self.rep, name='OldNeverFollowedUp', phone='5',
        )
        _clear_next_activity(self.old_no_followup)
        Lead.objects.filter(pk=self.old_no_followup.pk).update(created_at=start - timedelta(days=120))

        # Created today with no follow-up yet -> included via creation date, not
        # because it's unscheduled.
        self.new_no_followup = Lead.objects.create(
            employee=self.rep, name='NewLeadNoFollowup', phone='6',
        )
        _clear_next_activity(self.new_no_followup)

        self.new_lead_future_fu = Lead.objects.create(employee=self.rep, name='NewLeadFutureFu', phone='7')
        _schedule_next_activity(self.new_lead_future_fu, end + timedelta(days=5))

        self.yesterday_lead = Lead.objects.create(employee=self.rep, name='YesterdayLead', phone='8')
        _clear_next_activity(self.yesterday_lead)
        Lead.objects.filter(pk=self.yesterday_lead.pk).update(created_at=start - timedelta(hours=6))

    def test_default_landing_shows_everyone(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()
        for name in (
            'TodayEarly', 'TodayLate', 'OverdueLead', 'FutureLead', 'OldNeverFollowedUp',
            'NewLeadNoFollowup', 'NewLeadFutureFu', 'YesterdayLead',
        ):
            self.assertIn(name, content)

    def test_default_landing_orders_newest_created_first(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode()
        # YesterdayLead and NewLeadFutureFu were both created "now" (last in
        # setUp, never backdated) — newest-created-first puts the later one
        # earlier in the page than a lead backdated 30+ days ago.
        self.assertLess(content.index('NewLeadFutureFu'), content.index('OldNeverFollowedUp'))

    def test_default_landing_has_all_pill_active(self):
        r = self.client.get('/crm/leads/')
        content = r.content.decode().replace('\n', ' ')
        self.assertIn('pill on" href="/crm/leads/?date_scope=all"', content)
        self.assertNotIn('pill on" href="/crm/leads/?date_scope=today"', content)

    def test_default_landing_does_not_flag_as_active_filter(self):
        r = self.client.get('/crm/leads/')
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', r.content.decode())

    def test_command_strip_removed(self):
        r = self.client.get('/crm/leads/')
        self.assertNotIn('cmd-strip', r.content.decode())

    def test_explicit_date_scope_today_shows_only_todays_leads_and_followups(self):
        r = self.client.get('/crm/leads/?date_scope=today')
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

        # Explicit filter now, unlike the default landing -> flagged as active.
        self.assertIn('<span class="leads-filter-mob-btn__dot"', content)

    def test_explicit_date_scope_today_orders_by_time(self):
        r = self.client.get('/crm/leads/?date_scope=today')
        content = r.content.decode()
        self.assertLess(content.index('TodayEarly'), content.index('TodayLate'))

    def test_new_lead_and_followup_labels_render_correctly(self):
        r = self.client.get('/crm/leads/?date_scope=today')
        content = r.content.decode()

        new_lead_pos = content.index('NewLeadNoFollowup')
        new_lead_block = content[new_lead_pos:new_lead_pos + 3000]
        self.assertIn('New Lead', new_lead_block)
        self.assertNotIn('Follow-up</span>', new_lead_block)  # no follow-up scheduled -> no follow-up badge

        fu_pos = content.index('TodayEarly')
        fu_block = content[fu_pos:fu_pos + 3000]
        self.assertIn('Follow-up</span>', fu_block)
        self.assertNotIn('New Lead', fu_block)  # created 30 days ago -> not a "new lead"

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

    def test_explicit_date_scope_all_is_same_as_default(self):
        r = self.client.get('/crm/leads/?date_scope=all')
        content = r.content.decode()
        for name in (
            'TodayEarly', 'TodayLate', 'OverdueLead', 'FutureLead', 'OldNeverFollowedUp',
            'NewLeadNoFollowup', 'NewLeadFutureFu', 'YesterdayLead',
        ):
            self.assertIn(name, content)
        # 'all' is treated the same whether implicit (no param) or explicit —
        # not flagged as an active filter either way.
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', content)

        # Badges only mean something inside a specific period — hidden under "All time".
        old_pos = content.index('OldNeverFollowedUp')
        self.assertNotIn('New Lead', content[old_pos:old_pos + 3000])

    def test_clear_filters_link_returns_to_default_all_view(self):
        r = self.client.get('/crm/leads/?date_scope=today')
        content = r.content.decode()
        self.assertIn('href="/crm/leads/"', content)  # the "Clear" link's target
        r2 = self.client.get('/crm/leads/')
        self.assertEqual(r2.status_code, 200)
        self.assertNotIn('<span class="leads-filter-mob-btn__dot"', r2.content.decode())

    def test_lead_age_indicator_present(self):
        r = self.client.get('/crm/leads/')
        self.assertIn('old</span>', r.content.decode())
