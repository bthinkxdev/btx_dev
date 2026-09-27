"""Activity-based lead engine: outcome dispatch, state transitions, migration command."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from crm.models import Activity, EmployeeProfile, FollowUp, Lead
from crm.services.pipeline import PipelineError, complete_activity, move_to_nurture

User = get_user_model()


class FreshLeadProtectionTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('pipe_rep', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')

    def test_new_lead_gets_an_immediate_pending_activity(self):
        lead = Lead.objects.create(employee=self.rep, name='Fresh Co', phone='1')
        lead.refresh_from_db()
        self.assertIsNotNone(lead.next_activity)
        self.assertEqual(lead.next_activity.status, Activity.Status.PENDING)
        self.assertEqual(lead.lifecycle_state, Lead.LifecycleState.ACTIVE)


class OutcomeDispatchTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('pipe_rep2', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.lead = Lead.objects.create(employee=self.rep, name='Acme', phone='2', deal_value=Decimal('5000'))
        self.lead.refresh_from_db()
        self.activity = self.lead.next_activity

    def test_no_answer_creates_automatic_retry(self):
        next_act, lead = complete_activity(self.activity, outcome=Activity.Outcome.NO_ANSWER, user=self.rep)
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, Activity.Status.COMPLETED)
        self.assertIsNotNone(next_act)
        self.assertEqual(next_act.source, Activity.Source.AUTOMATIC)
        lead.refresh_from_db()
        self.assertEqual(lead.next_activity_id, next_act.id)
        self.assertEqual(lead.lifecycle_state, Lead.LifecycleState.ACTIVE)

    def test_interested_requires_explicit_next_action(self):
        with self.assertRaises(PipelineError):
            complete_activity(self.activity, outcome=Activity.Outcome.INTERESTED, user=self.rep)

    def test_interested_with_next_action_creates_it(self):
        when = timezone.now() + timezone.timedelta(days=2)
        next_act, lead = complete_activity(
            self.activity,
            outcome=Activity.Outcome.INTERESTED,
            user=self.rep,
            next_type=Activity.Type.DEMO,
            next_scheduled_at=when,
        )
        self.assertEqual(next_act.activity_type, Activity.Type.DEMO)
        self.assertEqual(next_act.source, Activity.Source.MANUAL)
        lead.refresh_from_db()
        self.assertEqual(lead.next_activity_id, next_act.id)

    def test_won_requires_deal_value(self):
        self.lead.deal_value = Decimal('0')
        self.lead.save(update_fields=['deal_value'])
        with self.assertRaises(PipelineError):
            complete_activity(self.activity, outcome=Activity.Outcome.WON, user=self.rep)

    def test_won_closes_lead_and_cancels_pending(self):
        next_act, lead = complete_activity(self.activity, outcome=Activity.Outcome.WON, user=self.rep)
        self.assertIsNone(next_act)
        lead.refresh_from_db()
        self.assertEqual(lead.lifecycle_state, Lead.LifecycleState.WON)
        self.assertIsNone(lead.next_activity)

    def test_lost_requires_reason(self):
        with self.assertRaises(PipelineError):
            complete_activity(self.activity, outcome=Activity.Outcome.LOST, user=self.rep)

    def test_lost_with_reason_closes_lead(self):
        next_act, lead = complete_activity(
            self.activity, outcome=Activity.Outcome.LOST, user=self.rep,
            lost_reason='Competitor selected', lost_note='Chose a cheaper agency.',
        )
        self.assertIsNone(next_act)
        lead.refresh_from_db()
        self.assertEqual(lead.lifecycle_state, Lead.LifecycleState.LOST)
        self.assertEqual(lead.lost_reason, 'Competitor selected')
        self.assertIsNotNone(lead.lost_at)

    def test_completed_activity_never_leaves_lead_without_next_action_or_terminal_state(self):
        """The spec's central invariant."""
        for outcome, kwargs in [
            (Activity.Outcome.NO_ANSWER, {}),
            (Activity.Outcome.FOLLOW_UP_REQUIRED, {
                'next_type': Activity.Type.CALL,
                'next_scheduled_at': timezone.now() + timezone.timedelta(days=1),
            }),
            (Activity.Outcome.WON, {}),
        ]:
            with self.subTest(outcome=outcome):
                lead = Lead.objects.create(
                    employee=self.rep, name=f'Invariant {outcome}', phone='3', deal_value=Decimal('1000'),
                )
                lead.refresh_from_db()
                complete_activity(lead.next_activity, outcome=outcome, user=self.rep, **kwargs)
                lead.refresh_from_db()
                is_terminal = lead.lifecycle_state != Lead.LifecycleState.ACTIVE
                self.assertTrue(lead.next_activity_id is not None or is_terminal)


class NurtureTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('pipe_rep3', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.lead = Lead.objects.create(employee=self.rep, name='Nurture Co', phone='4')

    def test_nurture_requires_reason_and_date(self):
        with self.assertRaises(PipelineError):
            move_to_nurture(self.lead, reason='', next_review=None)

    def test_nurture_sets_state_and_clears_next_activity(self):
        move_to_nurture(
            self.lead, reason='Budget in December', next_review=timezone.localdate() + timezone.timedelta(days=60),
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.lifecycle_state, Lead.LifecycleState.NURTURE)
        self.assertIsNone(self.lead.next_activity)


class MigrationCommandTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('pipe_rep4', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')

    def test_migrates_pending_followup_and_sets_next_activity(self):
        lead = Lead.objects.create(employee=self.rep, name='Legacy Co', phone='5')
        Activity.objects.filter(lead=lead).delete()
        lead.next_activity = None
        lead.save(update_fields=['next_activity'])
        when = timezone.now() + timezone.timedelta(days=3)
        FollowUp.objects.create(lead=lead, employee=self.rep, datetime=when, note='call back')

        out = StringIO()
        call_command('migrate_to_activities', stdout=out)

        lead.refresh_from_db()
        self.assertIsNotNone(lead.next_activity)
        self.assertEqual(lead.next_activity.scheduled_at, when)
        self.assertEqual(lead.next_activity.source, Activity.Source.MIGRATED)

    def test_active_lead_with_no_followup_is_surfaced_not_faked(self):
        lead = Lead.objects.create(employee=self.rep, name='No Action Co', phone='6')
        Activity.objects.filter(lead=lead).delete()
        lead.next_activity = None
        lead.save(update_fields=['next_activity'])

        call_command('migrate_to_activities', stdout=StringIO())

        lead.refresh_from_db()
        self.assertIsNone(lead.next_activity)
        self.assertEqual(lead.lifecycle_state, Lead.LifecycleState.ACTIVE)

    def test_idempotent_second_run_skips_already_migrated_leads(self):
        lead = Lead.objects.create(employee=self.rep, name='Idempotent Co', phone='7')
        FollowUp.objects.create(lead=lead, employee=self.rep, datetime=timezone.now() + timezone.timedelta(days=1))

        call_command('migrate_to_activities', stdout=StringIO())
        count_after_first = Activity.objects.filter(lead=lead, source=Activity.Source.MIGRATED).count()
        call_command('migrate_to_activities', stdout=StringIO())
        count_after_second = Activity.objects.filter(lead=lead, source=Activity.Source.MIGRATED).count()

        self.assertEqual(count_after_first, count_after_second)

    def test_dry_run_writes_nothing(self):
        lead = Lead.objects.create(employee=self.rep, name='DryRun Co', phone='8')
        before = Activity.objects.filter(lead=lead).count()
        call_command('migrate_to_activities', '--dry-run', stdout=StringIO())
        after = Activity.objects.filter(lead=lead).count()
        self.assertEqual(before, after)


class WorkbenchViewTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('pipe_rep5', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.client.force_login(self.rep)
        self.lead = Lead.objects.create(employee=self.rep, name='Workbench Co', phone='9', deal_value=Decimal('2000'))
        self.lead.refresh_from_db()

    def test_workbench_page_loads_and_shows_counts(self):
        r = self.client.get('/crm/workbench/?tab=all')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Workbench Co', r.content.decode())

    def test_workbench_tabs(self):
        for tab in ('overdue', 'today', 'new', 'upcoming', 'no_action', 'all'):
            r = self.client.get(f'/crm/workbench/?tab={tab}')
            self.assertEqual(r.status_code, 200, tab)

    def test_lead_panel_shows_current_activity(self):
        r = self.client.get(f'/crm/workbench/leads/{self.lead.pk}/panel/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('outcome', r.content.decode())

    def test_complete_activity_via_post_advances_queue(self):
        r = self.client.post(
            f'/crm/workbench/leads/{self.lead.pk}/complete/',
            {'outcome': 'won'},
        )
        self.assertEqual(r.status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.lifecycle_state, Lead.LifecycleState.WON)

    def test_complete_activity_invalid_outcome_returns_error(self):
        r = self.client.post(
            f'/crm/workbench/leads/{self.lead.pk}/complete/',
            {'outcome': 'lost'},  # missing required lost_reason
        )
        self.assertEqual(r.status_code, 400)

    def test_team_view_requires_manager(self):
        r = self.client.get('/crm/workbench/team/')
        self.assertEqual(r.status_code, 403)

    def test_team_view_allows_manager(self):
        EmployeeProfile.objects.filter(user=self.rep).update(is_sales_manager=True)
        r = self.client.get('/crm/workbench/team/')
        self.assertEqual(r.status_code, 200)
