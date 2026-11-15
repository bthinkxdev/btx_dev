"""Monthly deliverables: schedule maths, generation, plan edits, the work flow, reminders, carry-forward, access."""
from __future__ import annotations

import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core import mail as django_mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from crm.core import india
from crm.models import (AuditLog, Contract, ContractLine, Deliverable, DeliveryPlan, Notification, PlanItem,
                        ServiceDeliverable, Task)
from crm.modules.contracts import services as cs
from crm.modules.deliverables import jobs, services as ds
from crm.models.deliverables import CalendarFeedToken

from .base import CrmTestCase


class DeliverableCase(CrmTestCase):
    """A client of the sales executive, a monthly contract, and a standard plan on the service."""

    def setUp(self):
        super().setUp()
        self.today = india.today()
        self.tpl_poster = ServiceDeliverable.objects.create(
            service=self.service, title='Poster', category='Poster', frequency='weekly', weekdays='6', quantity=1, handled_by='Design team',
            requires_review=True, preferences='Use the red logo', sort=1)
        self.tpl_report = ServiceDeliverable.objects.create(
            service=self.service, title='Monthly report', category='Report', frequency='monthly', month_days='28', quantity=1,
            handled_by='Marketing team', sort=2)
        self.tpl_reels = ServiceDeliverable.objects.create(
            service=self.service, title='Reels', category='Reel or video', frequency='weekly', weekdays='4', quantity=2, handled_by='Media team', sort=3)

    # ---------------------------------------------------------------- builders
    def contract(self, months=12, start=None):
        start = start or self.today
        c = Contract.objects.create(client=self.client_obj, owner=self.u['manager'], title='Social retainer', start_date=start,
                                    end_date=india.add_months(start, months) - timedelta(days=1))
        ContractLine.objects.create(contract=c, service=self.service, description='Social media', unit_price=Decimal('12000'),
                                    billing_model='monthly', start_date=start, sac_code='998365')
        return cs.activate(c, self.u['manager'])

    def plan(self, **kw):
        c = self.contract(**kw)
        return DeliveryPlan.objects.get(contract=c)

    def instances(self, plan, title=None, **filters):
        qs = Deliverable.objects.filter(plan=plan, **filters)
        return qs.filter(title=title) if title else qs

    def live(self, plan, title=None):
        return self.instances(plan, title).exclude(status='cancelled')

    def overdue_item(self, plan, days_late, **kw):
        defaults = dict(plan=plan, client=plan.client, title='Late poster', period=(self.today - timedelta(days=days_late)).replace(day=1),
                        original_due_date=self.today - timedelta(days=days_late), due_date=self.today - timedelta(days=days_late),
                        due_time=time(18, 0), assignee=self.u['sales'], status='in_progress')
        defaults.update(kw)
        return Deliverable.objects.create(**defaults)


# ====================================================================== dates

class ScheduleMathTests(DeliverableCase):
    def item(self, **kw):
        base = dict(frequency='monthly', weekdays='', month_days='', start_date=date(2027, 1, 1), title='x')
        base.update(kw)
        return PlanItem(**base)

    def test_weekly_falls_on_the_chosen_weekdays_only(self):
        got = ds.due_dates(self.item(frequency='weekly', weekdays='6,2'), date(2026, 11, 1), date(2026, 11, 30))
        expected = [date(2026, 11, d) for d in range(1, 31) if date(2026, 11, d).weekday() in (6, 2)]
        self.assertEqual(got, expected)
        self.assertTrue(all(d.weekday() in (2, 6) for d in got))

    def test_the_31st_clamps_to_the_real_end_of_each_month(self):
        got = ds.due_dates(self.item(month_days='31'), date(2027, 1, 1), date(2027, 4, 30))
        self.assertEqual(got, [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)])

    def test_leap_year_february_has_a_29th(self):
        got = ds.due_dates(self.item(month_days='30', start_date=date(2028, 1, 1)), date(2028, 2, 1), date(2028, 2, 29))
        self.assertEqual(got, [date(2028, 2, 29)])

    def test_several_days_in_one_month(self):
        got = ds.due_dates(self.item(month_days='5,20'), date(2027, 3, 1), date(2027, 3, 31))
        self.assertEqual(got, [date(2027, 3, 5), date(2027, 3, 20)])

    def test_quarterly_steps_three_months_from_the_start_month(self):
        got = ds.due_dates(self.item(frequency='quarterly', month_days='15', start_date=date(2027, 1, 15)), date(2027, 1, 1), date(2027, 12, 31))
        self.assertEqual(got, [date(2027, 1, 15), date(2027, 4, 15), date(2027, 7, 15), date(2027, 10, 15)])

    def test_one_time_falls_once_inside_the_window(self):
        item = self.item(frequency='once', start_date=date(2027, 5, 10))
        self.assertEqual(ds.due_dates(item, date(2027, 5, 1), date(2027, 5, 31)), [date(2027, 5, 10)])
        self.assertEqual(ds.due_dates(item, date(2027, 6, 1), date(2027, 6, 30)), [])

    def test_a_year_boundary_is_handled(self):
        got = ds.due_dates(self.item(month_days='10'), date(2026, 12, 1), date(2027, 2, 28))
        self.assertEqual(got, [date(2026, 12, 10), date(2027, 1, 10), date(2027, 2, 10)])


# ====================================================================== plan + generation

class PlanGenerationTests(DeliverableCase):
    def test_activating_a_contract_builds_the_clients_plan_from_the_service(self):
        plan = self.plan()
        self.assertEqual(sorted(plan.items.values_list('title', flat=True)), ['Monthly report', 'Poster', 'Reels'])
        self.assertEqual(plan.account_owner, self.u['sales'])          # the client's owner, who came from the lead
        self.assertEqual(plan.assignee, self.u['sales'])
        self.assertEqual(plan.revisions.count(), 1)
        poster = plan.items.get(title='Poster')
        self.assertTrue(poster.requires_review)
        self.assertEqual(poster.source, self.tpl_poster)

    def test_instances_are_created_thirty_days_ahead_and_never_in_the_past(self):
        plan = self.plan()
        dates = list(self.live(plan).values_list('due_date', flat=True))
        self.assertTrue(dates)
        self.assertTrue(all(self.today <= d <= self.today + timedelta(days=30) for d in dates))
        sundays = self.live(plan, 'Poster').count()
        expected = sum(1 for i in range(31) if (self.today + timedelta(days=i)).weekday() == 6)
        self.assertEqual(sundays, expected)

    def test_each_instance_carries_assignee_reviewer_and_preferences(self):
        plan = self.plan()
        plan.client_preferences = 'Friendly tone'
        plan.save()
        ds.set_preferences(plan, self.u['sales'], 'Friendly tone')
        d = self.live(plan, 'Poster').first()
        self.assertEqual(d.assignee, self.u['sales'])
        self.assertEqual(d.reviewer, self.u['manager'])               # the assignee's manager reviews
        self.assertIn('Friendly tone', d.preferences)
        self.assertIn('Use the red logo', d.preferences)
        self.assertEqual(d.client_state, 'none')

    def test_running_generation_again_creates_nothing_new(self):
        plan = self.plan()
        n = Deliverable.objects.count()
        self.assertEqual(ds.generate(plan), 0)
        self.assertEqual(ds.generate_all(), 0)
        self.assertEqual(Deliverable.objects.count(), n)

    def test_the_database_itself_refuses_a_duplicate_date(self):
        plan = self.plan()
        d = self.live(plan, 'Poster').first()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Deliverable.objects.create(plan=plan, plan_item=d.plan_item, client=plan.client, title='Poster', period=d.period,
                                       original_due_date=d.original_due_date, due_date=d.due_date)

    def test_a_missed_nightly_run_is_caught_up(self):
        plan = self.plan()
        victim = self.live(plan, 'Reels').order_by('due_date').first()
        Deliverable.objects.filter(pk=victim.pk).delete()             # as if the job had not run that day
        self.assertGreaterEqual(ds.generate(plan), 0)
        # nothing is created before the plan started, so a deleted *future* row is recreated and a past one is not
        if victim.original_due_date >= plan.effective_from:
            self.assertTrue(Deliverable.objects.filter(plan_item=victim.plan_item, original_due_date=victim.original_due_date).exists())

    def test_nothing_is_created_for_dates_before_the_plan_started(self):
        plan = self.plan(start=self.today - timedelta(days=20))        # the contract is old; the plan starts now
        self.assertEqual(plan.effective_from, self.today)
        self.assertFalse(Deliverable.objects.filter(plan=plan, due_date__lt=self.today).exists())

    def test_free_text_service_promises_start_switched_off_and_ask_for_a_schedule(self):
        ServiceDeliverable.objects.all().delete()
        self.service.deliverables = '- 8 Instagram posts\n- Monthly ad report'
        self.service.save()
        plan = self.plan()
        items = list(plan.items.order_by('id'))
        self.assertEqual([i.title for i in items], ['8 Instagram posts', 'Monthly ad report'])
        self.assertTrue(all(i.is_paused and i.needs_schedule for i in items))
        self.assertFalse(Deliverable.objects.filter(plan=plan).exists())      # nothing guessed, nothing silently missed
        with self.assertRaises(ValidationError):
            ds.set_paused(items[0], self.u['sales'], False)               # cannot be switched on without days

    def test_a_one_time_contract_gets_no_plan(self):
        ServiceDeliverable.objects.all().delete()
        c = Contract.objects.create(client=self.client_obj, owner=self.u['manager'], title='Store build', start_date=self.today)
        ContractLine.objects.create(contract=c, service=self.service, description='Build', unit_price=Decimal('50000'),
                                    billing_model='one_time', start_date=self.today, sac_code='998314')
        c = cs.activate(c, self.u['manager'])
        self.assertFalse(DeliveryPlan.objects.filter(contract=c).exists())

    def test_a_template_is_applied_to_a_one_time_service_too(self):
        c = Contract.objects.create(client=self.client_obj, owner=self.u['manager'], title='Site build', start_date=self.today)
        ContractLine.objects.create(contract=c, service=self.service, description='Build', unit_price=Decimal('50000'),
                                    billing_model='one_time', start_date=self.today, sac_code='998314')
        c = cs.activate(c, self.u['manager'])
        self.assertTrue(DeliveryPlan.objects.filter(contract=c).exists())

    def test_a_failure_building_the_plan_never_blocks_activation(self):
        from unittest.mock import patch
        c = Contract.objects.create(client=self.client_obj, owner=self.u['manager'], title='x', start_date=self.today)
        ContractLine.objects.create(contract=c, service=self.service, description='x', unit_price=Decimal('1000'),
                                    billing_model='monthly', start_date=self.today, sac_code='998365')
        with patch('crm.modules.deliverables.services.create_plan', side_effect=RuntimeError('boom')):
            c = cs.activate(c, self.u['manager'])
        self.assertEqual(c.status, 'active')
        self.assertTrue(Notification.objects.filter(user=self.u['manager'], event_type='deliverable_failed').exists())


# ====================================================================== editing the plan

class PlanEditTests(DeliverableCase):
    def test_pausing_removes_untouched_future_items_and_resuming_brings_them_back_once(self):
        plan = self.plan()
        poster = plan.items.get(title='Poster')
        before = self.live(plan, 'Poster').count()
        self.assertGreater(before, 0)
        ds.set_paused(poster, self.u['sales'], True)
        self.assertEqual(self.live(plan, 'Poster').count(), 0)
        ds.set_paused(poster, self.u['sales'], False)
        self.assertEqual(self.live(plan, 'Poster').count(), before)
        self.assertEqual(self.instances(plan, 'Poster').count(), before)      # revived, not duplicated

    def test_work_already_started_is_never_rewritten_by_a_plan_edit(self):
        plan = self.plan()
        poster = plan.items.get(title='Poster')
        started = self.live(plan, 'Poster').order_by('due_date').last()
        ds.log_progress(started.pk, self.u['sales'], done=1)
        ds.set_paused(poster, self.u['sales'], True)
        started.refresh_from_db()
        self.assertEqual(started.status, 'in_progress')
        self.assertEqual(self.live(plan, 'Poster').count(), 1)

    def test_changing_the_weekday_moves_only_untouched_future_dates(self):
        plan = self.plan()
        poster = plan.items.get(title='Poster')
        kept = self.live(plan, 'Poster').order_by('due_date').last()
        ds.start(kept.pk, self.u['sales'])
        Deliverable.objects.filter(pk=kept.pk).update(locked=True)
        poster.weekdays = '0'                                            # Monday
        ds.save_item(plan, poster, self.u['sales'], creating=False)
        fresh = self.live(plan, 'Poster').exclude(pk=kept.pk)
        self.assertTrue(fresh.exists())
        self.assertTrue(all(d.due_date.weekday() == 0 for d in fresh))
        kept.refresh_from_db()
        self.assertEqual(kept.due_date.weekday(), 6)                     # the one in progress stays on its day

    def test_a_new_item_creates_its_dates_straight_away(self):
        plan = self.plan()
        item = PlanItem(title='Blog post', frequency='weekly', weekdays='2', start_date=self.today, quantity=1)
        ds.save_item(plan, item, self.u['sales'], creating=True)
        self.assertTrue(self.live(plan, 'Blog post').exists())
        self.assertTrue(all(d.due_date.weekday() == 2 for d in self.live(plan, 'Blog post')))
        self.assertEqual(plan.revisions.count(), 2)

    def test_an_invalid_item_is_refused(self):
        plan = self.plan()
        for kw in ({'title': ''}, {'frequency': 'weekly', 'weekdays': ''}, {'frequency': 'monthly', 'month_days': ''},
                   {'end_date': self.today - timedelta(days=3)}, {'quantity': 0}):
            data = dict(title='X', frequency='monthly', month_days='10', start_date=self.today, quantity=1)
            data.update(kw)
            with self.assertRaises(ValidationError, msg=kw):
                ds.save_item(plan, PlanItem(**data), self.u['sales'], creating=True)

    def test_removing_needs_a_reason_and_keeps_history(self):
        plan = self.plan()
        item = plan.items.get(title='Reels')
        with self.assertRaises(ValidationError):
            ds.remove_item(item, self.u['sales'], '')
        ds.remove_item(item, self.u['sales'], 'Client dropped reels')
        item.refresh_from_db()
        self.assertTrue(item.is_removed)
        self.assertEqual(self.live(plan, 'Reels').count(), 0)
        self.assertIn('Client dropped reels', plan.revisions.first().summary)
        self.assertTrue(AuditLog.objects.filter(action='deliverables.item_removed').exists())

    def test_a_frozen_period_is_left_alone(self):
        plan = self.plan()
        ds.freeze(plan, self.u['manager'], self.today + timedelta(days=12))
        plan.refresh_from_db()
        poster = plan.items.get(title='Poster')
        inside = list(self.live(plan, 'Poster').filter(due_date__lte=self.today + timedelta(days=12)).values_list('pk', flat=True))
        ds.set_paused(poster, self.u['sales'], True)
        self.assertEqual(self.live(plan, 'Poster').filter(pk__in=inside).count(), len(inside))      # locked
        self.assertFalse(self.live(plan, 'Poster').filter(due_date__gt=self.today + timedelta(days=12)).exists())

    def test_every_change_is_kept_as_a_revision_with_a_snapshot(self):
        plan = self.plan()
        item = PlanItem(title='Blog post', frequency='weekly', weekdays='2', start_date=self.today)
        ds.save_item(plan, item, self.u['sales'], creating=True)
        ds.set_preferences(plan, self.u['sales'], 'No red')
        rev = plan.revisions.first()
        self.assertEqual(rev.number, 3)
        self.assertEqual(rev.snapshot['client_preferences'], 'No red')
        self.assertIn('Blog post', [i['title'] for i in rev.snapshot['items']])

    def test_a_one_off_deliverable_is_added_and_left_alone_by_the_plan(self):
        plan = self.plan()
        d = ds.add_adhoc(plan, self.u['sales'], title='Diwali banner', due=self.today + timedelta(days=3), quantity=2,
                         assignee=self.u['sales_b'], instructions='Gold on maroon')
        self.assertTrue(d.locked)
        self.assertEqual(d.assignee, self.u['sales_b'])
        self.assertTrue(Notification.objects.filter(user=self.u['sales_b'], event_type='deliverable_assigned').exists())
        ds.set_paused(plan.items.get(title='Poster'), self.u['sales'], True)
        d.refresh_from_db()
        self.assertEqual(d.status, 'upcoming')


# ====================================================================== assignment

class AssignmentTests(DeliverableCase):
    def test_reassigning_a_client_moves_open_work_and_tells_both_people(self):
        plan = self.plan()
        item = plan.items.get(title='Reels')
        item.assignee = self.u['manager']
        item.save()
        ds.sync_item(item, self.u['sales'], old_assignee=self.u['sales'].pk)
        done = self.live(plan, 'Poster').first()
        ds.start(done.pk, self.u['sales'])
        ds.log_progress(done.pk, self.u['sales'], done=1)
        ds.deliver(done.pk, self.u['sales'], note='poster.png') if not done.requires_review else None
        moved = ds.reassign_plan(plan, self.u['sales_b'], self.u['manager'], account_owner=True, note='Sales is on leave')
        plan.refresh_from_db()
        self.assertEqual(plan.account_owner, self.u['sales_b'])
        self.assertEqual(plan.default_assignee, self.u['sales_b'])
        self.assertGreater(moved, 0)
        self.assertFalse(Deliverable.objects.filter(plan=plan, assignee=self.u['sales'], status__in=['upcoming', 'in_progress']).exists())
        self.assertTrue(self.live(plan, 'Reels').filter(assignee=self.u['manager']).exists())      # named on the item: stays
        for who in ('sales_b', 'sales'):
            self.assertTrue(Notification.objects.filter(user=self.u[who], event_type='deliverable_assigned').exists(), who)
        self.assertTrue(AuditLog.objects.filter(action='deliverables.plan_reassigned').exists())

    def test_new_items_after_a_reassign_go_to_the_new_person(self):
        plan = self.plan()
        ds.reassign_plan(plan, self.u['sales_b'], self.u['manager'])
        plan.refresh_from_db()
        item = PlanItem(title='Blog post', frequency='weekly', weekdays='2', start_date=self.today)
        ds.save_item(plan, item, self.u['manager'], creating=True)
        self.assertTrue(all(d.assignee == self.u['sales_b'] for d in self.live(plan, 'Blog post')))

    def test_reassigning_one_deliverable(self):
        plan = self.plan()
        d = self.live(plan, 'Monthly report').first() or self.live(plan, 'Reels').first()
        ds.reassign(d.pk, self.u['manager'], self.u['sales_b'], note='Better with reels')
        d.refresh_from_db()
        self.assertEqual(d.assignee, self.u['sales_b'])
        self.assertEqual(d.events.first().kind, 'reassigned')

    def test_an_inactive_person_cannot_be_given_work(self):
        plan = self.plan()
        self.u['sales_b'].is_active = False
        self.u['sales_b'].save()
        with self.assertRaises(ValidationError):
            ds.reassign_plan(plan, self.u['sales_b'], self.u['manager'])


# ====================================================================== the work flow

class WorkflowTests(DeliverableCase):
    def poster(self, plan=None):
        plan = plan or self.plan()
        return self.live(plan, 'Poster').order_by('due_date').first()

    def reels(self, plan=None):
        plan = plan or self.plan()
        return self.live(plan, 'Reels').order_by('due_date').first()

    def test_delivery_needs_the_review_first_and_a_note(self):
        d = self.poster()
        sales, manager = self.u['sales'], self.u['manager']
        ds.start(d.pk, sales)
        with self.assertRaises(ValidationError):
            ds.deliver(d.pk, sales, note='poster.png')                    # not reviewed yet
        d = ds.submit_review(d.pk, sales, note='poster-v1.png')
        self.assertEqual((d.status, d.review_state, d.reviewer), ('in_review', 'pending', manager))
        with self.assertRaises(ValidationError):
            ds.review(d.pk, sales, approve=True)                          # nobody approves their own work
        with self.assertRaises(ValidationError):
            ds.review(d.pk, manager, approve=False, note='')              # changes need saying what
        d = ds.review(d.pk, manager, approve=False, note='Logo is too small')
        self.assertEqual((d.status, d.review_state), ('in_progress', 'changes'))
        self.assertTrue(Notification.objects.filter(user=sales, event_type='deliverable_review', title__startswith='Changes').exists())
        ds.submit_review(d.pk, sales, note='poster-v2.png')
        d = ds.review(d.pk, manager, approve=True)
        self.assertEqual(d.review_state, 'approved')
        with self.assertRaises(ValidationError):
            ds.deliver(d.pk, sales, note='')                              # the note says what was delivered
        d = ds.deliver(d.pk, sales, note='poster-v2.png')
        self.assertEqual((d.status, d.quantity_done, d.delivered_by), ('delivered', 1, sales))
        self.assertFalse(d.was_late_against_contract)
        self.assertTrue(AuditLog.objects.filter(action='deliverables.delivered').exists())
        with self.assertRaises(ValidationError):
            ds.deliver(d.pk, sales, note='again')                         # cannot deliver twice

    def test_the_owner_may_approve_even_their_own_work(self):
        d = self.poster()
        owner = self.u['owner']
        Deliverable.objects.filter(pk=d.pk).update(assignee=owner)
        ds.submit_review(d.pk, owner, note='x.png')
        self.assertEqual(ds.review(d.pk, owner, approve=True).review_state, 'approved')

    def test_a_partial_delivery_is_tracked_and_a_short_one_needs_a_reason(self):
        d = self.reels()
        sales = self.u['sales']
        self.assertEqual(d.quantity, 2)
        d = ds.log_progress(d.pk, sales, done=1, note='first reel cut')
        self.assertEqual((d.quantity_done, d.status, d.percent), (1, 'in_progress', 50))
        with self.assertRaises(ValidationError):
            ds.log_progress(d.pk, sales, done=3)                          # more than promised
        with self.assertRaises(ValidationError):
            ds.deliver(d.pk, sales, note='reel-1.mp4', done=1)           # short, no reason
        d = ds.deliver(d.pk, sales, note='reel-1.mp4', done=1, shortfall_reason='Client sent one video only')
        self.assertEqual((d.status, d.quantity_done), ('delivered', 1))
        self.assertEqual(d.shortfall_reason, 'Client sent one video only')

    def test_the_month_summary_counts_units_not_items(self):
        plan = self.plan()
        d = self.reels(plan)
        ds.deliver(d.pk, self.u['sales'], note='reels.mp4', done=1, shortfall_reason='One only')
        summary = ds.month_summary(plan.client, d.period)
        row = next(r for r in summary['rows'] if r['title'] == 'Reels')
        self.assertEqual(row['delivered'], 1)
        self.assertGreaterEqual(row['promised'], 2)
        self.assertLess(summary['percent'], 100)

    def test_client_approval_is_timed_and_gates_delivery(self):
        plan = self.plan()
        item = PlanItem(title='Logo concept', frequency='once', start_date=self.today + timedelta(days=2), needs_client_approval=True)
        ds.save_item(plan, item, self.u['sales'], creating=True)
        d = self.live(plan, 'Logo concept').get()
        sales = self.u['sales']
        self.assertEqual(d.client_state, 'needed')
        with self.assertRaises(ValidationError):
            ds.deliver(d.pk, sales, note='logo.pdf')
        d = ds.send_to_client(d.pk, sales, note='WhatsApp')
        self.assertEqual((d.status, d.client_state), ('awaiting_client', 'awaiting'))
        self.assertIsNotNone(d.client_sent_at)
        with self.assertRaises(ValidationError):
            ds.client_answer(d.pk, sales, approved=False, note='')
        d = ds.client_answer(d.pk, sales, approved=False, note='Make it blue')
        self.assertEqual((d.status, d.client_state, d.client_revisions), ('in_progress', 'changes', 1))
        ds.send_to_client(d.pk, sales)
        d = ds.client_answer(d.pk, sales, approved=True)
        self.assertEqual(d.client_state, 'approved')
        self.assertIsNotNone(d.client_wait_days)
        self.assertEqual(ds.deliver(d.pk, sales, note='logo.pdf').status, 'delivered')

    def test_review_must_come_before_sending_to_the_client(self):
        plan = self.plan()
        item = PlanItem(title='Ad video', frequency='once', start_date=self.today + timedelta(days=2), requires_review=True,
                        needs_client_approval=True)
        ds.save_item(plan, item, self.u['sales'], creating=True)
        d = self.live(plan, 'Ad video').get()
        with self.assertRaises(ValidationError):
            ds.send_to_client(d.pk, self.u['sales'])

    def test_rescheduling_keeps_the_contract_date_and_needs_a_reason(self):
        d = self.reels()
        promised = d.original_due_date
        new = d.due_date + timedelta(days=3)
        with self.assertRaises(ValidationError):
            ds.reschedule(d.pk, self.u['sales'], new_date=new, reason='')
        with self.assertRaises(ValidationError):
            ds.reschedule(d.pk, self.u['sales'], new_date=self.today - timedelta(days=1), reason='in the past')
        d = ds.reschedule(d.pk, self.u['sales'], new_date=new, reason='Client has not sent photos')
        self.assertEqual((d.original_due_date, d.due_date, d.reschedule_count), (promised, new, 1))
        self.assertTrue(d.locked)
        self.assertEqual(d.period, promised.replace(day=1))
        self.assertTrue(AuditLog.objects.filter(action='deliverables.rescheduled').exists())

    def test_a_long_postponement_tells_the_account_owner(self):
        plan = self.plan()
        d = self.reels(plan)
        Deliverable.objects.filter(pk=d.pk).update(assignee=self.u['manager'])
        ds.reschedule(d.pk, self.u['manager'], new_date=d.original_due_date + timedelta(days=10), reason='Waiting on the client')
        self.assertTrue(Notification.objects.filter(user=self.u['sales'], event_type='deliverable_rescheduled').exists())

    def test_skipping_and_cancelling_need_a_reason_and_are_not_counted_as_delivered(self):
        plan = self.plan()
        d = self.reels(plan)
        with self.assertRaises(ValidationError):
            ds.close_out(d.pk, self.u['sales'], skip=True, reason='x')
        d = ds.close_out(d.pk, self.u['sales'], skip=True, reason='Client on holiday this week')
        self.assertEqual(d.status, 'skipped')
        self.assertTrue(AuditLog.objects.filter(action='deliverables.skipped').exists())
        row = next((r for r in ds.month_summary(plan.client, d.period)['rows'] if r['title'] == 'Reels'), None)
        self.assertEqual(row['skipped'], 2)

    def test_notes_and_instructions_are_kept_and_protect_the_item_from_plan_edits(self):
        plan = self.plan()
        d = self.live(plan, 'Poster').order_by('due_date').first()
        ds.set_instructions(d.pk, self.u['sales'], 'Mention the 20% offer')
        ds.add_note(d.pk, self.u['sales'], 'Called the client')
        d.refresh_from_db()
        self.assertTrue(d.locked)
        self.assertEqual(d.instructions, 'Mention the 20% offer')
        self.assertEqual(d.events.filter(kind='note').count(), 1)
        ds.set_paused(plan.items.get(title='Poster'), self.u['sales'], True)
        self.assertTrue(Deliverable.objects.filter(pk=d.pk, status='upcoming').exists())

    @override_settings()
    def test_assets_are_versioned_and_dangerous_files_refused(self):
        d = self.poster()
        with tempfile.TemporaryDirectory() as tmp, self.settings(MEDIA_ROOT=tmp):
            a1 = ds.add_asset(d.pk, self.u['sales'], name='Festive poster', kind='file', upload=SimpleUploadedFile('p.png', b'\x89PNG data'))
            a2 = ds.add_asset(d.pk, self.u['sales'], name='festive poster', kind='link', url='https://drive.example/p2')
            self.assertEqual((a1.version, a2.version), (1, 2))
            with self.assertRaises(ValidationError):
                ds.add_asset(d.pk, self.u['sales'], name='Bad', upload=SimpleUploadedFile('run.exe', b'MZ'))
            with self.assertRaises(ValidationError):
                ds.add_asset(d.pk, self.u['sales'], name='Nothing')


# ====================================================================== reminders and escalation

class ReminderTests(DeliverableCase):
    def plan(self, **kw):
        plan = super().plan(**kw)
        Deliverable.objects.filter(plan=plan).delete()          # only the items each test creates should be chased
        return plan

    def notes(self, user=None, **kw):
        qs = Notification.objects.filter(event_type__startswith='deliverable_', **kw)
        return qs.filter(user=user) if user else qs

    def test_24_hour_overdue_goes_to_the_assignee_manager_and_account_owner_once(self):
        plan = self.plan()
        d = self.overdue_item(plan, 2)                                     # at least 30 hours late
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(jobs.reminders(), 1)
        self.assertEqual(jobs.reminders(), 0)                              # a second run sends nothing
        d.refresh_from_db()
        self.assertEqual(d.reminder_level, 3)
        self.assertEqual(self.notes(self.u['sales'], event_type='deliverable_overdue').count(), 1)
        self.assertEqual(self.notes(self.u['manager'], event_type='deliverable_overdue').count(), 1)
        self.assertEqual(self.notes(self.u['owner'], event_type='deliverable_overdue').count(), 0)
        self.assertTrue(any('Overdue 24 hours' in m.subject for m in django_mail.outbox))

    def test_72_hours_escalates_to_the_owner_and_marks_it_missed(self):
        plan = self.plan()
        d = self.overdue_item(plan, 5)
        jobs.reminders()
        jobs.reminders()
        d.refresh_from_db()
        self.assertEqual(d.reminder_level, 4)
        self.assertIsNotNone(d.missed_at)
        self.assertEqual(self.notes(self.u['owner'], event_type='deliverable_overdue').count(), 1)
        self.assertEqual(self.notes(self.u['owner'], level='critical').count(), 1)
        self.assertEqual(d.events.filter(kind='missed').count(), 1)
        self.assertTrue(AuditLog.objects.filter(action='deliverables.missed').exists())

    def test_the_two_day_and_due_day_reminders_go_to_the_assignee_only(self):
        plan = self.plan()
        soon = self.overdue_item(plan, -2, title='Soon', status='upcoming', due_time=time(23, 59))
        jobs.reminders(now=timezone.make_aware(datetime.combine(self.today, time(7, 0))))
        soon.refresh_from_db()
        self.assertEqual(soon.reminder_level, 1)
        self.assertEqual(self.notes(self.u['sales'], event_type='deliverable_due').count(), 1)
        self.assertEqual(self.notes(self.u['manager']).count(), 0)
        today_item = self.overdue_item(plan, 0, title='Today', status='upcoming', due_time=time(23, 59))
        jobs.reminders(now=timezone.make_aware(datetime.combine(self.today, time(7, 0))))
        today_item.refresh_from_db()
        self.assertEqual(today_item.reminder_level, 1)                     # before 8 am only the early nudge
        jobs.reminders(now=timezone.make_aware(datetime.combine(self.today, time(9, 0))))
        today_item.refresh_from_db()
        self.assertEqual(today_item.reminder_level, 2)
        self.assertTrue(self.notes(self.u['sales'], title__startswith='Due today').exists())

    def test_two_overlapping_runs_cannot_both_send(self):
        plan = self.plan()
        d = self.overdue_item(plan, 2)
        # a second worker already claimed the step, so this run must stay quiet
        Deliverable.objects.filter(pk=d.pk).update(reminder_level=3)
        self.assertEqual(jobs.reminders(), 0)
        self.assertEqual(self.notes().count(), 0)

    def test_delivered_or_skipped_items_are_never_chased(self):
        plan = self.plan()
        self.overdue_item(plan, 5, status='delivered')
        self.overdue_item(plan, 5, title='Other', status='skipped')
        self.assertEqual(jobs.reminders(), 0)

    def test_a_closed_plan_stops_the_reminders(self):
        plan = self.plan()
        self.overdue_item(plan, 5)
        DeliveryPlan.objects.filter(pk=plan.pk).update(status='closed')
        self.assertEqual(jobs.reminders(), 0)

    def test_rescheduling_restarts_the_reminders_for_the_new_date(self):
        plan = self.plan()
        d = self.overdue_item(plan, 2)
        jobs.reminders()
        ds.reschedule(d.pk, self.u['sales'], new_date=self.today + timedelta(days=1), reason='Client delayed')
        d.refresh_from_db()
        self.assertEqual((d.reminder_level, d.missed_at), (0, None))

    def test_a_review_waiting_a_day_nudges_the_reviewer_once(self):
        plan = self.plan()
        d = self.overdue_item(plan, -3, status='in_review', requires_review=True, review_state='pending', reviewer=self.u['manager'],
                              review_requested_at=timezone.now() - timedelta(hours=26), review_note='poster.png')
        self.assertEqual(jobs.reminders(), 1)
        self.assertEqual(jobs.reminders(), 0)
        self.assertEqual(self.notes(self.u['manager'], event_type='deliverable_review').count(), 1)

    def test_a_silent_client_is_chased_at_two_and_five_days(self):
        plan = self.plan()
        d = self.overdue_item(plan, -9, status='awaiting_client', needs_client_approval=True, client_state='awaiting',
                              client_sent_at=timezone.now() - timedelta(days=3))
        jobs.reminders()
        jobs.reminders()
        self.assertEqual(self.notes(event_type='deliverable_client_wait').filter(user=self.u['sales']).count(), 1)
        Deliverable.objects.filter(pk=d.pk).update(client_sent_at=timezone.now() - timedelta(days=6))
        jobs.reminders()
        jobs.reminders()
        self.assertEqual(self.notes(event_type='deliverable_client_wait').filter(user=self.u['sales']).count(), 2)

    def test_the_morning_digest_is_one_message_per_person_per_day(self):
        plan = self.plan()
        self.overdue_item(plan, 1)
        self.overdue_item(plan, 0, title='Due now', status='upcoming', due_time=time(23, 59))
        self.assertEqual(jobs.digest(), 1)
        self.assertEqual(jobs.digest(), 0)
        n = self.notes(self.u['sales'], event_type='deliverable_digest').get()
        self.assertIn('1 due today', n.body)
        self.assertIn('1 overdue', n.body)

    def test_push_is_attempted_for_every_reminder(self):
        from unittest.mock import patch
        plan = self.plan()
        self.overdue_item(plan, 2)
        with patch('crm.core.notify.send_push') as push:
            jobs.reminders()
        self.assertTrue(push.called)


# ====================================================================== carry forward and contract changes

class ContractLinkTests(DeliverableCase):
    def test_an_amendment_carries_the_plan_forward_without_duplicating_dates(self):
        c = self.contract()
        old = DeliveryPlan.objects.get(contract=c)
        started = self.live(old, 'Poster').order_by('due_date').last()
        ds.start(started.pk, self.u['sales'])
        Deliverable.objects.filter(pk=started.pk).update(locked=True)
        ds.set_preferences(old, self.u['sales'], 'No red')
        new = cs.amend(c, self.u['manager'], effective_from=self.today + timedelta(days=5), reason='Price increase from next cycle')
        cs.activate(new, self.u['manager'])
        plan = DeliveryPlan.objects.get(contract=new)
        old.refresh_from_db()
        self.assertEqual((old.status, plan.status, plan.carried_from), ('closed', 'active', old))
        self.assertEqual(plan.client_preferences, 'No red')
        self.assertEqual(sorted(plan.items.values_list('title', flat=True)), sorted(old.items.values_list('title', flat=True)))
        self.assertTrue(all(i.carried_from_id for i in plan.items.all()))
        started.refresh_from_db()
        self.assertEqual(started.status, 'in_progress')                    # the started one stays with the old plan
        live = Deliverable.objects.filter(client=old.client, due_date__gte=self.today).exclude(status='cancelled')
        seen = [(d.title, d.original_due_date) for d in live]
        self.assertEqual(len(seen), len(set(seen)), 'a promise is due twice')
        self.assertTrue(Deliverable.objects.filter(plan=old, auto_cancelled=True).exists())
        self.assertEqual(plan.revisions.first().summary[:7], 'Carried')

    def test_a_renewal_continues_the_promises_into_the_new_term(self):
        c = self.contract(months=1)
        new = cs.renew(c, self.u['manager'], new_end=india.add_months(c.end_date, 12))
        plan = DeliveryPlan.objects.get(contract=new)
        self.assertEqual(plan.carried_from.contract, c)
        self.assertEqual(plan.account_owner, self.u['sales'])
        self.assertEqual(plan.items.count(), 3)

    def test_suspending_pauses_creation_and_resuming_restores_it(self):
        c = self.contract()
        plan = DeliveryPlan.objects.get(contract=c)
        cs.suspend(c, self.u['manager'], 'Client has not paid for two months')
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'paused')
        self.assertEqual(ds.generate(plan), 0)
        c.refresh_from_db()
        cs.resume(c, self.u['manager'])
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'active')

    def test_terminating_closes_the_plan_and_cancels_unstarted_future_work(self):
        c = self.contract()
        plan = DeliveryPlan.objects.get(contract=c)
        started = self.live(plan, 'Poster').order_by('due_date').last()
        ds.start(started.pk, self.u['sales'])
        Deliverable.objects.filter(pk=started.pk).update(locked=True)
        cs.terminate(c, self.u['manager'], on=self.today - timedelta(days=1), reason='Client closed their business')
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'closed')
        self.assertEqual(self.live(plan).count(), 1)
        started.refresh_from_db()
        self.assertEqual(started.status, 'in_progress')


# ====================================================================== workload and summaries

class InsightTests(DeliverableCase):
    def test_team_load_counts_items_not_hours(self):
        plan = self.plan()
        Deliverable.objects.filter(plan=plan).delete()
        self.overdue_item(plan, 3, title='Late one')
        self.overdue_item(plan, 0, title='Today one', status='upcoming', due_time=time(23, 59))
        self.overdue_item(plan, -3, title='This week', status='upcoming')
        self.overdue_item(plan, -20, title='Later', status='upcoming')
        self.overdue_item(plan, -5, title='With client', status='awaiting_client')
        self.overdue_item(plan, 9, title='Delivered', status='delivered')
        rows = ds.team_load([self.u['sales'], self.u['sales_b']])
        by = {r['user'].pk: r for r in rows}
        mine = by[self.u['sales'].pk]
        self.assertEqual((mine['open'], mine['overdue'], mine['today'], mine['week'], mine['later'], mine['waiting']), (5, 1, 1, 2, 1, 1))
        self.assertEqual(by[self.u['sales_b'].pk]['open'], 0)
        self.assertEqual(rows[0]['user'], self.u['sales'])                  # the one carrying the most is listed first

    def test_nothing_in_the_deliverables_carries_hours_any_more(self):
        plan = self.plan()
        d = self.live(plan, 'Poster').first()
        self.assertFalse(hasattr(d, 'est_minutes'))
        self.assertFalse(hasattr(d, 'minutes_left'))
        self.assertEqual(d.handled_by, 'Design team')                        # the team that produces it is what is recorded

    def test_client_health_ranks_the_clients_with_overdue_work_first(self):
        plan = self.plan()
        self.overdue_item(plan, 1, period=self.today.replace(day=1))
        health = ds.client_health(self.today.replace(day=1))
        self.assertEqual(health[0]['client_id'], plan.client_id)
        self.assertGreaterEqual(health[0]['overdue'], 1)


# ====================================================================== pages and access

class PageTests(DeliverableCase):
    def setUp(self):
        super().setUp()
        self.p = self.plan()
        self.d = self.live(self.p, 'Poster').order_by('due_date').first()

    def test_every_page_opens_for_the_people_who_should_see_it(self):
        self.login('sales')
        for name, args in (('deliverable_mine', []), ('deliverable_list', []), ('deliverable_detail', [self.d.pk]),
                           ('delivery_plan', [self.p.pk]), ('plan_item_create', [self.p.pk]), ('client_deliverables', [self.client_obj.pk]),
                           ('client_assets', [self.client_obj.pk]), ('followup_calendar', []), ('my_day', [])):
            r = self.client.get(reverse(f'crm:{name}', args=args))
            self.assertEqual(r.status_code, 200, name)
        self.login('manager')
        for name, args in (('deliverable_mine', []), ('deliverable_list', []), ('deliverable_workload', []),
                           ('service_deliverables', [self.service.pk]), ('template_item_create', [self.service.pk]),
                           ('template_item_edit', [self.tpl_poster.pk]), ('plan_item_edit', [self.p.items.first().pk])):
            self.assertEqual(self.client.get(reverse(f'crm:{name}', args=args)).status_code, 200, name)

    def test_the_detail_page_shows_what_the_client_wants_and_the_promised_date(self):
        self.login('sales')
        Deliverable.objects.filter(pk=self.d.pk).update(instructions='Use gold on maroon')
        r = self.client.get(reverse('crm:deliverable_detail', args=[self.d.pk]))
        self.assertContains(r, 'Use gold on maroon')
        self.assertContains(r, 'Use the red logo')
        self.assertContains(r, 'Promised in the contract')

    def test_an_executive_only_reaches_their_own_clients_work(self):
        self.login('sales_b')
        self.assertEqual(self.client.get(reverse('crm:deliverable_detail', args=[self.d.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('crm:deliverable_action', args=[self.d.pk, 'start'])).status_code, 404)
        self.assertEqual(self.client.get(reverse('crm:delivery_plan', args=[self.p.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('crm:deliverable_list')).context['total'], 0)

    def test_the_manager_sees_the_whole_team(self):
        self.login('manager')
        self.assertEqual(self.client.get(reverse('crm:deliverable_detail', args=[self.d.pk])).status_code, 200)
        self.assertGreater(self.client.get(reverse('crm:deliverable_list')).context['total'], 0)

    def test_roles_without_the_module_are_refused(self):
        for who in ('finance', 'hr', 'dev'):
            self.login(who)
            self.assertEqual(self.client.get(reverse('crm:deliverable_mine')).status_code, 403, who)
            self.assertEqual(self.client.get(reverse('crm:deliverable_detail', args=[self.d.pk])).status_code, 403, who)

    def test_an_executive_cannot_reassign_or_edit_the_standard_plans(self):
        self.login('sales')
        r = self.client.post(reverse('crm:deliverable_action', args=[self.d.pk, 'reassign']), {'to': self.u['sales_b'].pk})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.post(reverse('crm:delivery_plan_action', args=[self.p.pk, 'reassign']),
                                          {'to': self.u['sales_b'].pk}).status_code, 403)
        self.assertEqual(self.client.get(reverse('crm:service_deliverables', args=[self.service.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('crm:deliverable_workload')).status_code, 403)
        self.assertEqual(self.client.post(reverse('crm:delivery_plan_action', args=[self.p.pk, 'freeze']),
                                          {'through': str(self.today)}).status_code, 403)

    def test_actions_through_the_pages(self):
        self.login('sales')
        act = lambda verb, **data: self.client.post(reverse('crm:deliverable_action', args=[self.d.pk, verb]), data, follow=True)
        self.assertContains(act('start'), 'Started')
        self.assertContains(act('submit_review', note='poster.png'), 'Sent for review')
        self.login('manager')
        self.assertContains(act('review_approve', note='Good'), 'Approved')
        self.login('sales')
        r = act('deliver', note='')
        self.assertContains(r, 'is required')
        self.assertContains(act('deliver', note='poster.png'), 'Delivered')
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, 'delivered')

    def test_a_bad_number_or_date_is_a_message_not_an_error(self):
        self.login('sales')
        r = self.client.post(reverse('crm:deliverable_action', args=[self.live(self.p, 'Reels').first().pk, 'progress']), {'done': 'abc'}, follow=True)
        self.assertContains(r, 'Check the number')
        r = self.client.post(reverse('crm:deliverable_action', args=[self.d.pk, 'reschedule']), {'new_date': 'nope', 'reason': 'because'}, follow=True)
        self.assertContains(r, 'valid date')

    def test_cross_site_requests_without_a_token_are_refused(self):
        from django.test import Client
        self.login('sales')
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.u['sales'])
        r = strict.post(reverse('crm:deliverable_action', args=[self.d.pk, 'start']))
        self.assertNotEqual(r.status_code, 200)
        self.assertNotContains(r, 'Started', status_code=r.status_code)
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, 'upcoming')

    def test_adding_and_editing_a_plan_item_through_the_form(self):
        self.login('sales')
        url = reverse('crm:plan_item_create', args=[self.p.pk])
        bad = self.client.post(url, {'title': 'Blog', 'frequency': 'weekly', 'due_time': '18:00', 'quantity': 1,
                                     'start_date': self.today.isoformat()})
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, 'Choose at least one day')
        ok = self.client.post(url, {'title': 'Blog', 'frequency': 'weekly', 'weekdays': ['2'], 'due_time': '17:00', 'quantity': 1,
                                    'start_date': self.today.isoformat()})
        self.assertEqual(ok.status_code, 302)
        item = self.p.items.get(title='Blog')
        self.assertEqual(item.weekdays, '2')
        self.assertTrue(self.live(self.p, 'Blog').exists())
        edit = self.client.post(reverse('crm:plan_item_edit', args=[item.pk]),
                                {'title': 'Blog', 'frequency': 'monthly', 'month_days': '31', 'due_time': '17:00', 'quantity': 2,
                                 'start_date': self.today.isoformat()})
        self.assertEqual(edit.status_code, 302)
        item.refresh_from_db()
        self.assertEqual((item.frequency, item.month_days, item.quantity), ('monthly', '31', 2))

    def test_pause_resume_and_remove_through_the_pages(self):
        self.login('sales')
        item = self.p.items.get(title='Reels')
        self.client.post(reverse('crm:plan_item_action', args=[item.pk, 'pause']))
        item.refresh_from_db()
        self.assertTrue(item.is_paused)
        self.client.post(reverse('crm:plan_item_action', args=[item.pk, 'resume']))
        item.refresh_from_db()
        self.assertFalse(item.is_paused)
        r = self.client.post(reverse('crm:plan_item_action', args=[item.pk, 'remove']), {'reason': ''}, follow=True)
        self.assertContains(r, 'is required')

    def test_the_standard_plan_can_be_edited_by_a_manager(self):
        self.login('manager')
        url = reverse('crm:template_item_create', args=[self.service.pk])
        r = self.client.post(url, {'title': 'Ad report', 'frequency': 'monthly', 'month_days': '30', 'due_time': '18:00', 'quantity': 1})
        self.assertEqual(r.status_code, 302)
        row = ServiceDeliverable.objects.get(title='Ad report')
        self.assertEqual(row.month_days, '30')
        self.client.post(reverse('crm:template_item_delete', args=[row.pk]))
        self.assertFalse(ServiceDeliverable.objects.filter(pk=row.pk).exists())
        self.assertTrue(self.p.items.filter(title='Poster').exists())          # existing client plans keep their copy

    def test_the_plan_actions_freeze_preferences_generate_and_adhoc(self):
        self.login('manager')
        post = lambda verb, **data: self.client.post(reverse('crm:delivery_plan_action', args=[self.p.pk, verb]), data, follow=True)
        self.assertContains(post('preferences', client_preferences='Warm tone'), 'Preferences saved')
        self.assertContains(post('freeze', through=(self.today + timedelta(days=5)).isoformat()), 'Frozen')
        self.assertContains(post('unfreeze'), 'Freeze lifted')
        self.assertContains(post('generate'), 'already exists')
        self.assertContains(post('adhoc', title='Banner', due=(self.today + timedelta(days=2)).isoformat(), quantity=1), 'One-off')
        self.assertContains(post('reassign', to=self.u['sales_b'].pk, note='Cover'), 'Moved to')
        self.p.refresh_from_db()
        self.assertEqual(self.p.default_assignee, self.u['sales_b'])

    def test_the_calendar_shows_deliverables_for_the_person_and_hides_them_from_others(self):
        self.login('sales')
        r = self.client.get(reverse('crm:followup_calendar'), {'view': 'week', 'start': (self.d.due_date - timedelta(days=self.d.due_date.weekday())).isoformat()})
        self.assertContains(r, self.client_obj.display_name)
        self.assertContains(r, 'Poster')
        r = self.client.get(reverse('crm:followup_calendar'), {'view': 'month', 'year': self.d.due_date.year, 'month': self.d.due_date.month})
        self.assertContains(r, 'Poster')
        self.login('sales_b')
        r = self.client.get(reverse('crm:followup_calendar'), {'view': 'week', 'start': (self.d.due_date - timedelta(days=self.d.due_date.weekday())).isoformat()})
        self.assertNotContains(r, 'Poster')
        r = self.client.get(reverse('crm:followup_calendar'), {'show': 'followups', 'view': 'week'})
        self.assertEqual(r.status_code, 200)

    def test_project_tasks_appear_on_the_calendar_for_those_who_can_see_tasks(self):
        from crm.modules.projects import services as ps
        proj = ps.create_project(self.u['pm'], client=self.client_obj, name='Store build', manager=self.u['pm'], start_date=self.today,
                                 due_date=self.today + timedelta(days=30))
        ps.add_member(proj, self.u['pm'], user=self.u['dev'], allocation_percent=50)
        Task.objects.create(project=proj, title='Build the cart page', assignee=self.u['dev'], due_date=self.today + timedelta(days=1), created_by=self.u['pm'])
        self.login('dev')
        r = self.client.get(reverse('crm:followup_calendar'), {'view': 'week'})
        self.assertEqual(r.status_code, 200)
        week_start = self.today - timedelta(days=self.today.weekday())
        r = self.client.get(reverse('crm:followup_calendar'), {'view': 'week', 'start': (self.today + timedelta(days=1) - timedelta(days=(self.today + timedelta(days=1)).weekday())).isoformat()})
        self.assertContains(r, 'Build the cart page')
        self.assertIsNotNone(week_start)

    def test_my_day_lists_what_is_due_for_the_person(self):
        self.overdue_item(self.p, 1, title='Yesterday poster')
        self.login('sales')
        self.assertContains(self.client.get(reverse('crm:my_day')), 'Yesterday poster')

    def test_the_workload_page_lists_the_team(self):
        self.login('manager')
        r = self.client.get(reverse('crm:deliverable_workload'))
        self.assertContains(r, 'Team load')
        self.assertContains(r, 'Sales')

    def test_the_summary_page_and_the_email_to_the_client(self):
        ds.deliver(self.live(self.p, 'Reels').first().pk, self.u['sales'], note='reels.mp4', done=1, shortfall_reason='One only')
        self.login('sales')
        month = self.live(self.p, 'Reels').first().period.isoformat()
        r = self.client.get(reverse('crm:client_deliverables', args=[self.client_obj.pk]), {'month': month})
        self.assertContains(r, 'reels.mp4')
        django_mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse('crm:client_summary_send', args=[self.client_obj.pk]), {'month': month}, follow=True)
        self.assertContains(r, 'Summary emailed')
        self.assertEqual(len(django_mail.outbox), 1)
        self.assertIn('reels.mp4', django_mail.outbox[0].body)
        self.assertEqual(django_mail.outbox[0].to, ['anu@example.test'])

    def test_files_are_added_listed_and_downloaded_through_the_page_only_by_those_who_may(self):
        with tempfile.TemporaryDirectory() as tmp, self.settings(MEDIA_ROOT=tmp):
            self.login('sales')
            r = self.client.post(reverse('crm:deliverable_action', args=[self.d.pk, 'asset']),
                                 {'name': 'Festive poster', 'kind': 'file', 'file': SimpleUploadedFile('poster.png', b'png-bytes')}, follow=True)
            self.assertContains(r, 'Added to the repository')
            asset = self.d.assets.get()
            got = self.client.get(reverse('crm:deliverable_asset', args=[self.d.pk, asset.pk]))
            self.assertEqual(got.status_code, 200)
            self.assertEqual(b''.join(got.streaming_content), b'png-bytes')
            self.assertContains(self.client.get(reverse('crm:client_assets', args=[self.client_obj.pk])), 'Festive poster')
            self.login('sales_b')
            self.assertEqual(self.client.get(reverse('crm:deliverable_asset', args=[self.d.pk, asset.pk])).status_code, 404)

    def test_the_board_filters_and_exports(self):
        self.overdue_item(self.p, 3)
        self.login('manager')
        r = self.client.get(reverse('crm:deliverable_list'), {'quick': 'overdue'})
        self.assertEqual(r.context['total'], 1)
        r = self.client.get(reverse('crm:deliverable_list'), {'assignee': self.u['sales'].pk, 'status': 'upcoming'})
        self.assertGreater(r.context['total'], 0)
        csv = self.client.get(reverse('crm:deliverable_list'), {'export': 'csv'})
        self.assertEqual(csv.status_code, 200)
        self.assertIn('Delivery note', b''.join(csv.streaming_content).decode('utf-8-sig'))
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:deliverable_list'), {'export': 'csv'}).status_code, 403)


class CalendarFeedTests(DeliverableCase):
    def test_the_private_calendar_link_lists_the_persons_work_and_nobody_elses(self):
        plan = self.plan()
        feed = CalendarFeedToken.for_user(self.u['sales'])
        r = self.client.get(reverse('crm:calendar_feed', args=[feed.token]))              # no login: the link is the key
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'text/calendar; charset=utf-8')
        body = r.content.decode()
        self.assertIn('BEGIN:VCALENDAR', body)
        self.assertIn('SUMMARY:Deliver: Poster\\, Acme', body)
        self.assertIn('BEGIN:VALARM', body)
        other = CalendarFeedToken.for_user(self.u['sales_b'])
        self.assertNotIn('Poster', self.client.get(reverse('crm:calendar_feed', args=[other.token])).content.decode())
        self.assertEqual(self.client.get(reverse('crm:calendar_feed', args=['not-a-real-token'])).status_code, 404)
        self.assertIsNotNone(plan)

    def test_the_feed_includes_followups_and_tasks(self):
        from crm.models import FollowUp, Lead, LeadSource
        lead = Lead.objects.create(name='Ravi Stores', phone='+919800000001', owner=self.u['sales'], created_by=self.u['sales'],
                                   source=LeadSource.objects.first(), stage='contacted', next_follow_up_at=timezone.now() + timedelta(days=1))
        FollowUp.objects.create(lead=lead, due_at=timezone.now() + timedelta(days=1), assignee=self.u['sales'], purpose='Send quote')
        feed = CalendarFeedToken.for_user(self.u['sales'])
        body = self.client.get(reverse('crm:calendar_feed', args=[feed.token])).content.decode()
        self.assertIn('Ravi Stores', body)

    def test_a_reset_link_replaces_the_old_one(self):
        self.login('sales')
        old = CalendarFeedToken.for_user(self.u['sales']).token
        self.client.post(reverse('crm:calendar_feed_reset'))
        new = CalendarFeedToken.objects.get(user=self.u['sales']).token
        self.assertNotEqual(old, new)
        self.assertEqual(self.client.get(reverse('crm:calendar_feed', args=[old])).status_code, 404)

    def test_a_deactivated_person_has_no_feed(self):
        feed = CalendarFeedToken.for_user(self.u['sales_b'])
        self.u['sales_b'].is_active = False
        self.u['sales_b'].save()
        self.assertEqual(self.client.get(reverse('crm:calendar_feed', args=[feed.token])).status_code, 404)


class GrantMigrationTests(CrmTestCase):
    def test_default_roles_carry_the_new_module(self):
        from crm.models import Role
        grants = {r.code: r.grants.get('deliverables', {}) for r in Role.objects.all()}
        self.assertEqual(grants['sales_executive'], {'view': 'own', 'create': 'own', 'edit': 'own'})
        self.assertEqual(grants['sales_manager']['assign'], 'team')
        self.assertEqual(grants['owner']['configure'], 'company')
        self.assertEqual(grants['finance_executive'], {})


class BackfillTests(DeliverableCase):
    def old_contract(self):
        """A contract that was activated before the module existed: no plan."""
        c = Contract.objects.create(client=self.client_obj, owner=self.u['manager'], title='Old retainer', start_date=self.today,
                                    status='active', number='CT-OLD-1')
        ContractLine.objects.create(contract=c, service=self.service, description='Social', unit_price=Decimal('9000'),
                                    billing_model='monthly', start_date=self.today, sac_code='998365')
        return c

    def run_command(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('crm_deliverables_backfill', *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_changes_nothing_and_apply_creates_the_plan_once(self):
        c = self.old_contract()
        text = self.run_command()
        self.assertIn('CT-OLD-1', text)
        self.assertIn('Dry run', text)
        self.assertFalse(DeliveryPlan.objects.filter(contract=c).exists())
        self.run_command('--apply')
        plan = DeliveryPlan.objects.get(contract=c)
        self.assertEqual(plan.account_owner, self.u['sales'])
        self.assertGreater(plan.deliverables.count(), 0)
        count = plan.deliverables.count()
        self.assertIn('0 contract(s) to set up', self.run_command('--apply'))
        self.assertEqual(plan.deliverables.count(), count)
