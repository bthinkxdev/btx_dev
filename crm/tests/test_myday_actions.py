"""My Day: Postpone and Complete follow-up quick actions."""
from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from .base import CrmTestCase


class MyDayActionTests(CrmTestCase):
    def setUp(self):
        super().setUp()
        from crm.models import FollowUp, LeadSource
        from crm.modules.leads import services as svc
        self.owner = self.u['sales']
        src = LeadSource.objects.create(name='Test site', channel='website', default_owner=self.owner)
        self.lead = svc.create_lead(svc.LeadInput(name='Priya Nair', phone='9876501234', source=src), actor=self.owner)
        self.fu = FollowUp.objects.filter(lead=self.lead, status='pending').first() or svc.schedule_follow_up(
            self.lead, kind='call', due_at=timezone.now() + timedelta(hours=1), assignee=self.owner, actor=self.owner)

    def test_my_day_renders_postpone_form_with_hidden_id(self):
        self.login('sales')
        html = self.client.get(reverse('crm:my_day')).content.decode()
        self.assertIn('postpone-quick-form', html)

    def test_postpone_from_my_day(self):
        self.login('sales')
        new_due = timezone.localtime(timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        r = self.client.post(reverse('crm:follow_up_quick', args=[self.fu.pk, 'postpone']),
                             {'postpone-new_due': new_due, 'postpone-reason': 'customer_request'}, follow=True)
        self.fu.refresh_from_db()
        msgs = [str(m) for m in r.context['messages']] if r.context else []
        self.assertEqual(self.fu.postpone_count, 1, msgs)

    def test_complete_from_my_day(self):
        self.login('sales')
        r = self.client.get(reverse('crm:lead_detail', args=[self.lead.pk]) + f'?modal=complete&fu={self.fu.pk}')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn(f'value="{self.fu.pk}"', html)
        new_due = timezone.localtime(timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        r = self.client.post(reverse('crm:lead_action', args=[self.lead.pk, 'complete']),
                             {'complete-follow_up': self.fu.pk, 'complete-outcome': 'Spoke', 'complete-next_due': new_due,
                              'complete-next_kind': 'call'})
        self.fu.refresh_from_db()
        self.assertEqual(self.fu.status, "done")

    def test_complete_quick_action(self):
        self.login('sales')
        new_due = timezone.localtime(timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('crm:follow_up_quick', args=[self.fu.pk, 'complete']),
                         {'complete-outcome': 'Spoke', 'complete-next_due': new_due, 'complete-next_kind': 'call'})
        self.fu.refresh_from_db()
        self.assertEqual(self.fu.status, 'done')


class FollowUpReminderTests(CrmTestCase):
    def test_30_and_5_minute_reminders_go_to_executive_and_manager(self):
        from crm.models import FollowUp, LeadSource, Notification
        from crm.modules.leads import jobs, services as svc
        owner = self.u['sales']
        src = LeadSource.objects.create(name='Rem site', channel='website', default_owner=owner)
        lead = svc.create_lead(svc.LeadInput(name='Remind Me', phone='9876501299', source=src), actor=owner)
        FollowUp.objects.filter(lead=lead).delete()
        fu = FollowUp.objects.create(lead=lead, kind='call', due_at=timezone.now() + timedelta(minutes=29), assignee=owner, created_by=owner)
        jobs.follow_up_reminders_and_escalation()
        jobs.follow_up_reminders_and_escalation()   # idempotent
        q = Notification.objects.filter(event_type='follow_up_reminder')
        self.assertEqual(q.filter(user=owner, title__startswith='In 30 min').count(), 1)
        self.assertEqual(q.filter(user=self.u['manager'], title__startswith='In 30 min').count(), 1)
        fu.due_at = timezone.now() + timedelta(minutes=4)
        fu.save()
        jobs.follow_up_reminders_and_escalation()
        self.assertEqual(q.filter(user=owner, title__startswith='In 5 min').count(), 1)
