"""Sidebar navigation. Items are hidden unless the user holds the permission (Part 2.2 'Hidden (permission)')."""
from __future__ import annotations

from django.urls import NoReverseMatch, reverse

from . import permissions

# (key, label, icon, [ (label, url_name, module, action[, minimum_scope]) ... ])
NAV = [
    ('MENU', [
        ('home', 'My Day', 'sun', [('My Day', 'crm:my_day', None, None)]),
        ('dashboards', 'Dashboards', 'grid', [
            ('Owner', 'crm:dashboard_owner', 'reports', 'view', 'company'),
            ('Sales', 'crm:dashboard_sales', 'leads', 'view'),
            ('Finance', 'crm:dashboard_finance', 'invoices', 'view', 'company'),
            ('Delivery', 'crm:dashboard_delivery', 'projects', 'view'),
            ('HR', 'crm:hr_dashboard', 'employees', 'view', 'company'),
        ]),
        ('approvals', 'Approvals', 'shield', [('Approvals', 'crm:approval_list', None, None)]),
    ]),
    ('SALES', [
        ('leads', 'Leads', 'users', [
            ('All leads', 'crm:lead_list', 'leads', 'view'),
            ('Follow-up calendar', 'crm:followup_calendar', 'leads', 'view'),
            ('Import leads', 'crm:lead_import', 'leads', 'import'),
            ('Inbound inbox', 'crm:inbox_list', 'leads', 'assign'),
            ('Duplicates', 'crm:duplicate_list', 'leads', 'assign'),
            ('Stale and unassigned', 'crm:stale_leads', 'leads', 'assign'),
            ('Lost-lead analysis', 'crm:lost_analysis', 'leads', 'view'),
            ('Sources and campaigns', 'crm:source_list', 'leads', 'configure'),
            ('Website chats', 'crm:chat_list', 'chats', 'view'),
        ]),
        ('pipeline', 'Pipeline', 'board', [
            ('Pipeline board', 'crm:pipeline', 'deals', 'view'),
            ('Targets and performance', 'crm:targets', 'deals', 'view'),
        ]),
        ('quotations', 'Quotations', 'document', [('Quotations', 'crm:quotation_list', 'quotations', 'view')]),
        ('clients', 'Clients', 'briefcase', [
            ('Client directory', 'crm:client_list', 'clients', 'view'),
            ('Service catalogue', 'crm:service_list', 'services', 'view'),
        ]),
    ]),
    ('MONEY', [
        ('collections', 'Collections', 'link', [
            ('Payment links', 'crm:link_list', 'payment_links', 'view'),
            ('Payments', 'crm:payment_list', 'payments', 'view'),
            ('Verify manual payments', 'crm:payment_verify_queue', 'payments', 'approve'),
            ('Allocation', 'crm:allocation', 'payments', 'create'),
            ('Refunds', 'crm:refund_list', 'refunds', 'view'),
            ('Gateway events', 'crm:gateway_events', 'payments', 'approve'),
        ]),
        ('billing', 'Invoices', 'invoice', [
            ('Invoices', 'crm:invoice_list', 'invoices', 'view'),
            ('Credit notes', 'crm:credit_note_list', 'invoices', 'view'),
            ('Receivables ageing', 'crm:receivables', 'invoices', 'view'),
            ('Customer advances', 'crm:advances', 'payments', 'view'),
            ('TDS receivable', 'crm:tds_receivable', 'payments', 'view'),
        ]),
        ('expenses', 'Expenses', 'receipt', [
            ('Expenses and bills', 'crm:expense_list', 'expenses', 'view'),
            ('Approval queue', 'crm:expense_approvals', 'expenses', 'approve'),
            ('Vendors', 'crm:vendor_list', 'vendors', 'view'),
            ('Recurring expenses', 'crm:recurring_expense_list', 'expenses', 'approve'),
            ('TDS payable', 'crm:tds_payable', 'expenses', 'approve'),
        ]),
        ('accounting', 'Accounting', 'book', [
            ('Accounting dashboard', 'crm:accounting_dashboard', 'accounting', 'view'),
            ('Chart of accounts', 'crm:account_list', 'accounting', 'view'),
            ('Journal entries', 'crm:journal_list', 'accounting', 'view'),
            ('General ledger', 'crm:general_ledger', 'accounting', 'view'),
            ('Trial balance', 'crm:trial_balance', 'accounting', 'view'),
            ('Profit and loss', 'crm:profit_loss', 'accounting', 'view'),
            ('Balance sheet', 'crm:balance_sheet', 'accounting', 'view'),
            ('Cash flow', 'crm:cash_flow', 'accounting', 'view'),
            ('GST and TDS reports', 'crm:tax_reports', 'accounting', 'view'),
            ('Periods', 'crm:period_list', 'accounting', 'view'),
        ]),
        ('banking', 'Banking', 'bank', [
            ('Bank accounts', 'crm:bank_account_list', 'banking', 'view'),
            ('Reconciliation', 'crm:reconciliation', 'banking', 'view'),
            ('Settlements', 'crm:settlement_list', 'banking', 'view'),
        ]),
    ]),
    ('DELIVERY', [
        ('contracts', 'Contracts', 'folder', [
            ('Contracts', 'crm:contract_list', 'contracts', 'view'),
            ('Renewals', 'crm:renewals', 'contracts', 'view'),
            ('Recurring billing', 'crm:billing_schedule', 'contracts', 'view'),
        ]),
        ('projects', 'Projects', 'tasks', [
            ('Project portfolio', 'crm:project_list', 'projects', 'view'),
            ('My tasks', 'crm:task_list', 'tasks', 'view'),
            ('Timesheets', 'crm:timesheet', 'tasks', 'view'),
            ('Timesheet approvals', 'crm:timesheet_approvals', 'tasks', 'approve'),
            ('Blockers and overdue', 'crm:blockers', 'tasks', 'view'),
        ]),
        ('tickets', 'Support tickets', 'ticket', [('Tickets', 'crm:ticket_list', 'tickets', 'view')]),
    ]),
    ('PEOPLE', [
        ('hr', 'HR', 'id', [
            ('Employees', 'crm:employee_list', 'employees', 'view', 'company'),
            ('Attendance', 'crm:attendance', 'attendance', 'view', 'company'),
            ('Leave', 'crm:leave_list', 'leave', 'view', 'company'),
            ('Holidays and shifts', 'crm:holiday_list', 'leave', 'configure', 'company'),
            ('HR approvals', 'crm:hr_approvals', 'leave', 'approve', 'company'),
        ]),
        ('payroll', 'Payroll', 'payroll', [
            ('Payroll runs', 'crm:payroll_list', 'payroll', 'view'),
            ('Salary structures', 'crm:salary_structures', 'payroll', 'edit'),
            ('Commissions', 'crm:commission_list', 'commissions', 'view'),
            ('My payslips', 'crm:my_payslips', '', ''),
        ]),
    ]),
    ('INSIGHTS', [
        ('reports', 'Reports', 'chart', [
            ('Report catalogue', 'crm:report_index', 'reports', 'view'),
            ('AI sales analyst', 'crm:ai_analyst', 'ai_analyst', 'view'),
        ]),
        ('admin', 'Administration', 'settings', [
            ('Company settings', 'crm:settings_company', 'settings', 'configure'),
            ('Users and roles', 'crm:user_list', 'settings', 'configure'),
            ('Permission matrix', 'crm:role_list', 'settings', 'configure'),
            ('Teams and departments', 'crm:team_list', 'settings', 'configure'),
            ('Approval and SLA policy', 'crm:settings_policy', 'settings', 'configure'),
            ('Tax and numbering', 'crm:settings_tax', 'settings', 'configure'),
            ('Payment gateway', 'crm:settings_gateway', 'settings', 'configure'),
            ('Message templates', 'crm:template_list', 'settings', 'configure'),
            ('Jobs and health', 'crm:job_list', 'automation', 'view'),
            ('Audit log', 'crm:audit_log', 'audit', 'view'),
            ('Security events', 'crm:security_events', 'audit', 'view'),
        ]),
    ]),
]


# Phone bottom bar: (label, url_name, module, action, icon). First three the user may open, then "More".
MOBILE_TABS = [
    ('My Day', 'crm:my_day', None, None, 'sun'),
    ('Leads', 'crm:lead_list', 'leads', 'view', 'users'),
    ('Follow-ups', 'crm:followup_calendar', 'leads', 'view', 'calendar'),
    ('Pipeline', 'crm:pipeline', 'deals', 'view', 'board'),
    ('Dashboard', 'crm:dashboard_finance', 'invoices', 'view', 'grid'),
    ('Projects', 'crm:project_list', 'projects', 'view', 'tasks'),
    ('Tasks', 'crm:task_list', 'tasks', 'view', 'tasks'),
    ('Approvals', 'crm:approval_list', None, None, 'shield'),
]


def mobile_tabs(user, current_url_name: str, limit: int = 4) -> list:
    tabs = []
    for label, url_name, module, action, icon in MOBILE_TABS:
        if module and not permissions.has_perm(user, module, action):
            continue
        if url_name == 'crm:dashboard_finance':
            from crm.modules.reports import dashboards
            if not dashboards.can_open(user, 'finance'):
                continue
        try:
            href = reverse(url_name)
        except NoReverseMatch:
            continue
        stem = url_name.split(':', 1)[1].rsplit('_', 1)[0]
        tabs.append({'label': label, 'href': href, 'icon': icon,
                     'active': bool(current_url_name) and (current_url_name == url_name.split(':', 1)[1]
                                                           or current_url_name.startswith(stem + '_'))})
        if len(tabs) == limit:
            break
    return tabs


def build_nav(user, current_url_name: str) -> list:
    groups = []
    for group_label, items in NAV:
        visible_items = []
        for key, label, icon, children in items:
            kids = []
            for clabel, url_name, module, action, *rest in children:
                if module and not permissions.has_perm(user, module, action):
                    continue
                if rest and not permissions.has_scope(user, module, action, rest[0]):
                    continue
                try:
                    href = reverse(url_name)
                except NoReverseMatch:
                    continue
                kids.append({'label': clabel, 'href': href, 'active': current_url_name == url_name.split(':', 1)[1]})
            if not kids:
                continue
            prefixes = {c[1].split(':', 1)[1].rsplit('_', 1)[0] + '_' for c in children}
            family = bool(current_url_name) and any(current_url_name.startswith(p) for p in prefixes)
            visible_items.append({'key': key, 'label': label, 'icon': icon, 'children': kids,
                                  'single': len(children) == 1,
                                  'active': any(k['active'] for k in kids) or family})
        if visible_items:
            groups.append({'label': group_label, 'items': visible_items})
    return groups
