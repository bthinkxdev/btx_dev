"""Temporary placeholders for routes whose modules are still being built."""
from django.shortcuts import render
from django.urls import path

from crm.core.views import staff_required

NAMES = [
    'dashboard_owner', 'dashboard_sales', 'dashboard_finance', 'dashboard_delivery', 'hr_dashboard',
    'pipeline', 'targets', 'quotation_list', 'client_list', 'service_list', 'link_list', 'payment_list',
    'payment_verify_queue', 'allocation', 'refund_list', 'gateway_events', 'invoice_list', 'credit_note_list',
    'receivables', 'advances', 'tds_receivable', 'expense_list', 'expense_approvals', 'vendor_list',
    'recurring_expense_list', 'tds_payable', 'accounting_dashboard', 'account_list', 'journal_list',
    'general_ledger', 'trial_balance', 'profit_loss', 'balance_sheet', 'tax_reports', 'period_list',
    'bank_account_list', 'reconciliation', 'settlement_list', 'contract_list', 'renewals', 'billing_schedule',
    'project_list', 'task_list', 'timesheet', 'timesheet_approvals', 'blockers', 'ticket_list', 'employee_list',
    'attendance', 'leave_list', 'holiday_list', 'hr_approvals', 'payroll_list', 'salary_structures',
    'commission_list', 'report_index', 'settings_company', 'user_list', 'role_list', 'team_list',
    'settings_policy', 'settings_tax', 'settings_gateway', 'template_list', 'job_list', 'audit_log',
    'security_events', 'task_create', 'expense_create', 'payment_record',
]


@staff_required
def coming(request, **kwargs):
    return render(request, 'crm/errors/403.html', {'message': 'This page is being built.'})


PK_NAMES = ['quotation_create', 'quotation_detail', 'link_detail', 'contract_create', 'project_create', 'client_detail',
            'task_detail', 'invoice_detail']


def patterns(existing: set):
    out = [path(f'_pending/{n}/', coming, name=n) for n in NAMES if n not in existing]
    for n in PK_NAMES:
        if n not in existing:
            out += [path(f'_pending/{n}/', coming, name=n), path(f'_pending/{n}/<int:pk>/', coming, name=n)]
    return out
