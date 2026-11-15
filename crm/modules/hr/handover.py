"""When someone leaves, everything they were accountable for goes to a named person, so no customer, contract, deliverable
or task is ever left with a login that no longer works.

The new owner is the leaver's manager. If there is no manager with a login, an administrator takes it. The move is
written to the audit log and the manager is told, so the work can then be shared out on purpose."""
from __future__ import annotations

from crm.core import audit, notify


def target_for(employee):
    """Who takes over: the manager when they have an active login, else the first administrator."""
    mgr = employee.manager.user if employee.manager_id and employee.manager.user_id else None
    if mgr is not None and mgr.is_active:
        return mgr
    owners = notify.owners()
    return owners[0] if owners else None


OPEN_QUOTES = ['draft', 'awaiting_approval', 'approved', 'sent', 'viewed']
LIVE_CONTRACTS = ['draft', 'pending_approval', 'active', 'suspended']
LIVE_PROJECTS = ['planned', 'active', 'on_hold']
OPEN_DELIVERABLE = ['upcoming', 'in_progress', 'in_review', 'awaiting_client']
OPEN_TASK_EXCLUDE = ['done', 'cancelled']


def _querysets(user):
    from crm.models import Client, Contract, Deliverable, DeliveryPlan, PaymentLink, PlanItem, Project, Quotation, Task
    return {
        'clients': Client.objects.filter(owner=user, archived_at__isnull=True),
        'contracts': Contract.objects.filter(owner=user, status__in=LIVE_CONTRACTS),
        'plans': DeliveryPlan.objects.filter(account_owner=user).exclude(status='closed'),
        'deliverables': Deliverable.objects.filter(assignee=user, status__in=OPEN_DELIVERABLE),
        'quotations': Quotation.objects.filter(owner=user, status__in=OPEN_QUOTES),
        'links': PaymentLink.objects.filter(owner=user, status__in=['draft', 'active', 'partially_paid']),
        'projects': Project.objects.filter(manager=user, status__in=LIVE_PROJECTS),
        'tasks': Task.objects.filter(assignee=user).exclude(status__in=OPEN_TASK_EXCLUDE),
        'plan_items': PlanItem.objects.filter(assignee=user, is_removed=False),
    }


def preview(user) -> dict:
    """How much would move, for the offboarding screen."""
    q = _querysets(user)
    return {k: q[k].count() for k in ('clients', 'contracts', 'deliverables', 'quotations', 'projects', 'plans')}


def reassign_accounts(user, to, actor, reason: str = '') -> dict:
    """Move customers, contracts, delivery plans and open deliverables, quotations, payment links, projects and tasks."""
    if to is None or not to.is_active:
        return {'skipped': 'no one to hand over to'}
    from crm.models import Deliverable, DeliveryPlan, PlanItem
    q = _querysets(user)
    out = {
        'clients': q['clients'].update(owner=to),
        'contracts': q['contracts'].update(owner=to),
        'quotations': q['quotations'].update(owner=to),
        'links': q['links'].update(owner=to),
        'projects': q['projects'].update(manager=to),
        'tasks': q['tasks'].update(assignee=to),
    }
    out['plans'] = q['plans'].update(account_owner=to)
    DeliveryPlan.objects.filter(default_assignee=user).exclude(status='closed').update(default_assignee=to)
    out['deliverables'] = q['deliverables'].update(assignee=to)
    Deliverable.objects.filter(reviewer=user, status__in=OPEN_DELIVERABLE).update(reviewer=to)
    PlanItem.objects.filter(reviewer=user, is_removed=False).update(reviewer=to)
    q['plan_items'].update(assignee=None)                     # the plan default is now the new person
    audit.log('employee.accounts_reassigned', user, actor=actor, reason=reason, changes={'to': str(to), **out})
    return out
