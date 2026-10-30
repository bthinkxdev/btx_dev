"""Lead engine timers (Part 4.9): unassigned alerts, first-response SLA, overdue escalation, stale,
stage aging, win-back resurfacing, silent sources, inbox retries. Every job is idempotent:
alert levels and dedupe keys stop repeats."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from crm.core import india, notify
from crm.models import CompanySettings, FollowUp, Lead, LeadInbox, LeadSource
from crm.models.leads import OPEN_STAGES

from . import services as svc


REMINDER_STAGES = (30, 5)   # minutes before a follow-up is due


def _url(lead):
    return reverse('crm:lead_detail', args=[lead.pk])


def unassigned_alerts() -> int:
    c = CompanySettings.get()
    now = timezone.now()
    n = 0
    assigners = None
    for lead in Lead.objects.filter(owner__isnull=True, stage='new', archived_at__isnull=True):
        age = (now - lead.created_at).total_seconds() / 60
        # Try assignment again first: someone may have become available.
        svc.auto_assign(lead)
        lead.refresh_from_db()
        if lead.owner_id:
            continue
        if lead.unassigned_alert_level < 1 and age >= c.unassigned_manager_alert_minutes:
            assigners = assigners or notify.users_with_perm('leads', 'assign')
            notify.notify(assigners, f'Unassigned lead waiting: {lead}', body=f'Waiting {int(age)} minutes. Assign it now.',
                          url=_url(lead), event_type='unassigned_lead', level='warning', dedupe_key=f'unassigned1:{lead.pk}')
            Lead.objects.filter(pk=lead.pk).update(unassigned_alert_level=1)
            n += 1
        if lead.unassigned_alert_level < 2 and age >= c.unassigned_owner_alert_minutes:
            notify.notify(notify.owners(), f'Lead unassigned for {int(age)} minutes: {lead}', url=_url(lead),
                          event_type='unassigned_lead', level='critical', dedupe_key=f'unassigned2:{lead.pk}', email=True)
            Lead.objects.filter(pk=lead.pk).update(unassigned_alert_level=2)
            n += 1
    return n


def first_response_sla() -> int:
    now = timezone.now()
    n = 0
    for lead in Lead.objects.filter(stage='new', owner__isnull=False, first_contacted_at__isnull=True,
                                    archived_at__isnull=True).select_related('owner'):
        due = svc.sla_due(lead.assigned_at or lead.created_at)
        if now > due:
            recipients = [lead.owner, notify.manager_of(lead.owner)]
            created = notify.notify(recipients, f'First-response SLA breached: {lead}',
                                    body='Contact the lead now.', url=_url(lead), event_type='sla_breach',
                                    level='critical', dedupe_key=f'sla:{lead.pk}')
            n += bool(created)
    return n


def follow_up_reminders_and_escalation() -> int:
    c = CompanySettings.get()
    now = timezone.now()
    n = 0
    # Reminders 30 and 5 minutes before the due time, to the assignee and their manager. The dedupe key
    # carries the due time, so a postponed follow-up is reminded again for its new time.
    upcoming = FollowUp.objects.filter(status='pending', due_at__gt=now, due_at__lte=now + timedelta(minutes=REMINDER_STAGES[0]),
                                       lead__archived_at__isnull=True).select_related('lead', 'assignee')
    for fu in upcoming:
        left = (fu.due_at - now).total_seconds() / 60
        for stage in REMINDER_STAGES:
            if left > stage:
                continue
            # A follow-up scheduled inside the 30-minute window skips straight to the 5-minute reminder.
            if stage == REMINDER_STAGES[0] and left < REMINDER_STAGES[0] - 5:
                continue
            if stage == REMINDER_STAGES[0] and left <= REMINDER_STAGES[1]:
                continue
            mgr = notify.manager_of(fu.assignee)
            when = timezone.localtime(fu.due_at)
            created = notify.notify(
                [fu.assignee, mgr], f'In {stage} min: {fu.get_kind_display().lower()} with {fu.lead.name} at {when:%H:%M}',
                body=(fu.purpose or '') + ('' if fu.assignee is None else f' ({fu.assignee.get_full_name() or fu.assignee})'),
                url=_url(fu.lead), event_type='follow_up_reminder', level='warning' if stage <= 5 else 'info',
                dedupe_key=f'remind{stage}:{fu.pk}:{int(fu.due_at.timestamp())}')
            n += bool(created)
        FollowUp.objects.filter(pk=fu.pk, reminded_at__isnull=True).update(reminded_at=now)
    # Overdue: owner at due time, manager at +4h, company owner at +24h.
    for fu in FollowUp.objects.filter(status='pending', due_at__lte=now, lead__archived_at__isnull=True,
                                      escalation_level__lt=3).select_related('lead', 'assignee'):
        late = now - fu.due_at
        level = fu.escalation_level
        if level < 1:
            notify.notify([fu.assignee], f'Overdue: {fu.get_kind_display().lower()} with {fu.lead.name}', url=_url(fu.lead),
                          event_type='follow_up_overdue', level='warning', dedupe_key=f'overdue1:{fu.pk}:{int(fu.due_at.timestamp())}')
            level = 1
        if level < 2 and late >= timedelta(hours=c.overdue_manager_hours):
            mgr = notify.manager_of(fu.assignee)
            notify.notify([mgr] if mgr else notify.users_with_perm('leads', 'assign'),
                          f'Follow-up {int(late.total_seconds() // 3600)} h overdue: {fu.lead.name} ({fu.assignee.get_full_name()})',
                          url=_url(fu.lead), event_type='follow_up_overdue', level='warning',
                          dedupe_key=f'overdue2:{fu.pk}:{int(fu.due_at.timestamp())}')
            level = 2
        if level < 3 and late >= timedelta(hours=c.overdue_owner_hours):
            notify.notify(notify.owners(), f'Follow-up over a day late: {fu.lead.name} ({fu.assignee.get_full_name()})',
                          url=_url(fu.lead), event_type='follow_up_overdue', level='critical',
                          dedupe_key=f'overdue3:{fu.pk}:{int(fu.due_at.timestamp())}')
            level = 3
        if level != fu.escalation_level:
            FollowUp.objects.filter(pk=fu.pk).update(escalation_level=level)
            n += 1
    return n


def stale_and_aging() -> int:
    c = CompanySettings.get()
    now = timezone.now()
    n = 0
    open_leads = Lead.objects.filter(stage__in=OPEN_STAGES, archived_at__isnull=True, owner__isnull=False).select_related('owner')
    for lead in open_leads.filter(Q(last_activity_at__lt=now - timedelta(days=c.stale_days)) | Q(last_activity_at__isnull=True)):
        idle = (now - (lead.last_activity_at or lead.created_at)).days
        if lead.stale_alert_level < 1:
            notify.notify([lead.owner], f'Stale lead: no activity for {idle} days — {lead}', url=_url(lead),
                          event_type='stale_lead', level='warning', dedupe_key=f'stale1:{lead.pk}:{int(lead.last_activity_at.timestamp())}' if lead.last_activity_at else f'stale1:{lead.pk}')
            Lead.objects.filter(pk=lead.pk).update(stale_alert_level=1)
            n += 1
        if lead.stale_alert_level < 2 and idle >= c.stale_escalate_days:
            mgr = notify.manager_of(lead.owner)
            notify.notify([mgr] if mgr else notify.owners(), f'Lead idle {idle} days: {lead} ({lead.owner.get_full_name()})',
                          url=_url(lead), event_type='stale_lead', level='warning')
            Lead.objects.filter(pk=lead.pk).update(stale_alert_level=2)
            n += 1
    limits = {'qualified': c.aging_qualified_days, 'quotation_sent': c.aging_quotation_sent_days,
              'negotiation': c.aging_negotiation_days}
    for stage, days in limits.items():
        for lead in open_leads.filter(stage=stage, stage_changed_at__lt=now - timedelta(days=days)).exclude(aging_alerted_stage=stage):
            mgr = notify.manager_of(lead.owner)
            notify.notify([lead.owner, mgr], f'{lead} has been in {lead.get_stage_display()} for {lead.days_in_stage} days',
                          url=_url(lead), event_type='stage_aging', level='warning')
            Lead.objects.filter(pk=lead.pk).update(aging_alerted_stage=stage)
            n += 1
    return n


def win_back() -> int:
    """Lost leads resurface on their win-back date as a task to the owner (Part 4.9)."""
    today = india.today()
    n = 0
    for lead in Lead.objects.filter(stage='lost', win_back_date__lte=today, archived_at__isnull=True).select_related('owner'):
        assignee = lead.owner if (lead.owner and lead.owner.is_active) else None
        if assignee is None:
            users = notify.users_with_perm('leads', 'assign')
            assignee = users[0] if users else None
        if assignee is None:
            continue
        FollowUp.objects.create(lead=lead, kind='call', due_at=india.next_working_day_at(), assignee=assignee,
                                purpose='Win-back: lost on ' + (timezone.localtime(lead.lost_at).strftime('%d-%b-%Y') if lead.lost_at else ''))
        Lead.objects.filter(pk=lead.pk).update(win_back_date=None)
        notify.notify([assignee], f'Win-back today: {lead}', url=_url(lead), event_type='win_back')
        n += 1
    return n


def silent_sources() -> int:
    now = timezone.now()
    n = 0
    for s in LeadSource.objects.filter(is_active=True, is_paused=False, expected_min_per_day__gt=0):
        window = timedelta(hours=s.silent_window_hours or 24)
        last = s.last_lead_at or s.created_at
        if now - last > window and not s.is_silent:
            LeadSource.objects.filter(pk=s.pk).update(is_silent=True, silent_alerted_at=now)
            recipients = notify.owners() + notify.users_with_perm('leads', 'import')
            notify.notify(recipients, f'Lead source silent: {s.name}', body=f'No leads for {int((now - last).total_seconds() // 3600)} hours.',
                          url=reverse('crm:source_list'), event_type='source_silent', level='critical', email=True)
            n += 1
    return n


def retry_inbox() -> int:
    n = 0
    for item in LeadInbox.objects.filter(Q(status='failed', next_retry_at__lte=timezone.now()) |
                                         Q(status='received', received_at__lt=timezone.now() - timedelta(minutes=2))):
        svc.ingest(item)
        n += 1
    return n
