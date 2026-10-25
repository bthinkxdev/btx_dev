"""A single, MCP-friendly "whole business" snapshot.

Deliberately a new, pure function rather than a refactor of dashboards.owner() - that
function returns UI-coupled Card objects built around reverse() URLs and icon/tone
metadata for template rendering. This composes the same underlying pure numeric helpers
(profit_loss, cash_flow, mrr, ageing, cash_position) directly.
"""
from __future__ import annotations

from django.utils import timezone

from crm.core import india
from crm.core.money import ZERO


def business_overview(start=None, end=None) -> dict:
    from django.db.models import Count, Sum

    from crm.modules.accounting import reports as acc_reports
    from crm.modules.billing import services as billing_svc
    from crm.modules.contracts import services as contract_svc
    from crm.modules.reports import dashboards
    from crm.models import Invoice, Lead, Project, Ticket
    from crm.models.leads import OPEN_STAGES

    end = end or india.today()
    start = start or india.month_start(end)

    open_leads = Lead.objects.filter(stage__in=OPEN_STAGES)
    pipeline_value = open_leads.aggregate(s=Sum('value'))['s'] or ZERO
    active_projects = Project.objects.filter(status='active').count()
    overdue_tickets = Ticket.objects.filter(status__in=('open', 'in_progress', 'waiting'),
                                            sla_due_at__lt=timezone.now()).count()
    overdue_invoices = Invoice.objects.filter(status__in=('issued', 'partially_paid'), due_date__lt=end).count()

    _by_client, ageing_totals, _buckets = billing_svc.ageing(as_of=end)

    return {
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'pipeline': {'open_leads': open_leads.count(), 'open_value': str(pipeline_value)},
        'profit_loss': acc_reports.profit_loss(start, end),
        'cash_flow': acc_reports.cash_flow(start, end),
        'cash_position': str(dashboards.cash_position()),
        'mrr': str(contract_svc.mrr(end)),
        'receivables_ageing': ageing_totals,
        'delivery': {'active_projects': active_projects, 'overdue_tickets': overdue_tickets,
                    'overdue_invoices': overdue_invoices},
    }
