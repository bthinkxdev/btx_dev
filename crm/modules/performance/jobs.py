"""Scheduled work for performance. Both jobs are safe to run more than once.

* ``snapshot``: records, per salesperson, the facts that cannot be rebuilt later (open pipeline, overdue follow-ups at the
  end of the day). One row per person per day, updated in place.
* ``missing_next_action``: no open lead may be left without a follow-up. If one is, a follow-up is set for the owner on the
  next working day and the owner and their manager are told. Each such fix is counted against the owner's profile."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils import timezone

from crm.core import audit, india, notify, permissions
from crm.models import FollowUp, Lead, StaffDayStat
from crm.models.leads import OPEN_STAGES

logger = logging.getLogger('crm.performance')

SALES_ROLES = ('sales_executive', 'sales_manager', 'telecaller')
STALE_DAYS = 3


def sales_staff() -> list:
    """Active people who sell: the sales roles, or anyone who receives leads. Administrators are left out."""
    User = get_user_model()
    qs = User.objects.filter(is_active=True).filter(Q(crm_profile__roles__code__in=SALES_ROLES) | Q(crm_profile__receives_leads=True)).distinct()
    return [u for u in qs.select_related('crm_profile') if not permissions.is_super_admin(u)]


def snapshot(day=None, now=None) -> int:
    now = now or timezone.now()
    day = day or india.today()
    n = 0
    for u in sales_staff():
        openq = Lead.objects.filter(owner=u, stage__in=OPEN_STAGES, archived_at__isnull=True)
        stale = openq.filter(Q(last_activity_at__lt=now - timedelta(days=STALE_DAYS)) |
                             Q(last_activity_at__isnull=True, created_at__lt=now - timedelta(days=STALE_DAYS))).count()
        StaffDayStat.objects.update_or_create(user=u, day=day, defaults={
            'open_leads': openq.count(), 'open_pipeline': openq.aggregate(s=Sum('value'))['s'] or 0, 'stale_leads': stale,
            'overdue_followups': FollowUp.objects.filter(assignee=u, status='pending', due_at__lt=now).count()})
        n += 1
    return n


def missing_next_action(now=None) -> int:
    """Give every open, owned lead that has no pending follow-up one, and tell the owner."""
    from crm.modules.leads import services as leads
    now = now or timezone.now()
    today = india.today()
    fixed = 0
    qs = (Lead.objects.filter(stage__in=OPEN_STAGES, archived_at__isnull=True, owner__isnull=False, owner__is_active=True)
          .exclude(follow_ups__status='pending').select_related('owner').distinct())
    for lead in qs:
        try:
            due = india.next_working_day_at(start=now)
            leads.schedule_follow_up(lead, kind='call', due_at=due, assignee=lead.owner, actor=None,
                                     purpose='Set automatically: this lead had no follow-up')
        except Exception:
            logger.exception('Could not set a follow-up on lead %s', lead.pk)
            continue
        fixed += 1
        stat, _ = StaffDayStat.objects.get_or_create(user=lead.owner, day=today)
        StaffDayStat.objects.filter(pk=stat.pk).update(auto_scheduled=F('auto_scheduled') + 1)
        audit.log('lead.followup_auto_scheduled', lead, changes={'owner': str(lead.owner), 'due': due})
        url = reverse('crm:lead_detail', args=[lead.pk])
        notify.notify([lead.owner, notify.manager_of(lead.owner)], f'{lead} had no follow-up, so one was set for {due:%a %d %b}',
                      body='Every open lead needs a dated next action. Open it and set the right one.', url=url,
                      event_type='no_next_action', level='warning', dedupe_key=f'nofu:{lead.pk}:{today:%Y%m%d}')
    return fixed
