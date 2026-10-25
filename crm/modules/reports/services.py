"""Shared report calculations (Part 13.2). Definitions never blur: revenue, invoicing, collections, profit."""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum

from crm.core.money import ZERO, q2


def project_cost(project) -> dict:
    """Timesheet hours x member cost rate + direct/allocated expenses (Part 10.4)."""
    from crm.models import ExpenseAllocation, ProjectMember, TimeEntry
    rates = dict(ProjectMember.objects.filter(project=project).values_list('user_id', 'cost_rate'))
    labour = ZERO
    hours = Decimal('0')
    for e in TimeEntry.objects.filter(project=project).exclude(status='rejected'):
        hours += e.hours
        labour += q2(e.hours * rates.get(e.user_id, ZERO))
    direct = sum((x.amount_before_tax for x in project.expenses.filter(status__in=['approved', 'partially_paid', 'paid'],
                                                                           allocations__isnull=True)), ZERO)
    allocated = ExpenseAllocation.objects.filter(project=project, expense__status__in=['approved', 'partially_paid', 'paid']) \
        .aggregate(s=Sum('amount'))['s'] or ZERO
    return {'hours': hours, 'labour': labour, 'expenses': direct + allocated, 'total': labour + direct + allocated}


def project_revenue(project, basis: str = 'invoiced') -> Decimal:
    from crm.models import Invoice
    inv = Invoice.objects.filter(project=project, status__in=['issued', 'partially_paid', 'paid', 'credit_noted'])
    billed = inv.aggregate(s=Sum('taxable_total'))['s'] or ZERO
    credited = sum((cn.taxable_value for i in inv for cn in i.credit_notes.filter(status='issued')), ZERO)
    return billed - credited


def client_profitability(client) -> dict:
    from crm.models import CreditNote, ExpenseAllocation, Invoice, Project
    inv = Invoice.objects.filter(client=client, status__in=['issued', 'partially_paid', 'paid', 'credit_noted', 'written_off'])
    revenue = (inv.aggregate(s=Sum('taxable_total'))['s'] or ZERO) - \
        (CreditNote.objects.filter(invoice__client=client, status='issued').aggregate(s=Sum('taxable_value'))['s'] or ZERO)
    projects = []
    cost_total = ZERO
    for p in Project.objects.filter(client=client):
        c = project_cost(p)
        r = project_revenue(p)
        cost_total += c['total']
        projects.append({'project': p, 'revenue': r, 'cost': c['total'], 'margin': r - c['total']})
    allocated = ExpenseAllocation.objects.filter(client=client, project__isnull=True,
                                                 expense__status__in=['approved', 'partially_paid', 'paid']).aggregate(s=Sum('amount'))['s'] or ZERO
    cost_total += allocated
    margin = revenue - cost_total
    return {'revenue': revenue, 'direct_cost': cost_total, 'allocated': allocated, 'margin': margin,
            'margin_pct': q2(margin * 100 / revenue) if revenue else None, 'projects': projects}
