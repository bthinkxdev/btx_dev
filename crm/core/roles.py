"""Default role templates (Part 3.2) and the sensitive-action matrix (Part 3.3).

Every cell is editable by the administrator from Settings -> Roles; these are
only the starting values seeded by `crm_bootstrap`.
"""
from __future__ import annotations

from .permissions import ACTIONS, FIELD_GROUPS, MODULES


def _all(scope='company', exclude=()):
    return {a: scope for a in ACTIONS if a not in exclude}


def _grant(**modules):
    return {m: dict(a) for m, a in modules.items()}


SALES_VIEW = {'view': 'own'}

ROLE_TEMPLATES = [
    {
        'code': 'owner', 'name': 'Owner / Super Admin', 'home': 'crm:dashboard_owner',
        'description': 'Everything, settings, approvals and audit.',
        'grants': {m: _all() for m in MODULES},
        'field_groups': list(FIELD_GROUPS),
    },
    {
        'code': 'sales_manager', 'name': 'Sales Manager', 'home': 'crm:pipeline',
        'description': 'Assign leads, approve discounts, quotations and payment links within limit, team targets.',
        'grants': _grant(
            leads={'view': 'team', 'create': 'team', 'edit': 'team', 'assign': 'team', 'export': 'team',
                   'archive': 'team', 'approve': 'team', 'import': 'team'},
            deals={'view': 'team', 'edit': 'team', 'configure': 'team'},
            quotations={'view': 'team', 'create': 'team', 'edit': 'team', 'approve': 'team', 'cancel': 'team', 'export': 'team'},
            payment_links={'view': 'team', 'create': 'team', 'approve': 'team', 'cancel': 'team'},
            payments={'view': 'team', 'create': 'team'},
            refunds={'view': 'team', 'create': 'team'},
            invoices={'view': 'team'},
            clients={'view': 'team', 'create': 'team', 'edit': 'team', 'approve': 'team'},
            services={'view': 'company'},
            contracts={'view': 'team', 'create': 'team', 'edit': 'team'},
            projects={'view': 'team'},
            tickets={'view': 'team', 'create': 'team'},
            commissions={'view': 'team', 'approve': 'team'},
            reports={'view': 'team', 'export': 'team'},
            leave={'view': 'own', 'create': 'own'},
            attendance={'view': 'own', 'create': 'own'},
            expenses={'view': 'own', 'create': 'own'},
            chats={'view': 'company'},
            ai_analyst={'view': 'company'},
        ),
        'field_groups': ['internal_notes', 'cost_price'],
    },
    {
        'code': 'sales_executive', 'name': 'Sales Executive', 'home': 'crm:my_day',
        'description': 'Own leads, quotations, payment links within limit, follow-ups.',
        'grants': _grant(
            leads={'view': 'own', 'create': 'own', 'edit': 'own'},
            deals={'view': 'own', 'edit': 'own'},
            quotations={'view': 'own', 'create': 'own', 'edit': 'own', 'cancel': 'own'},
            payment_links={'view': 'own', 'create': 'own', 'cancel': 'own'},
            payments={'view': 'own', 'create': 'own'},
            invoices={'view': 'own'},
            clients={'view': 'own', 'create': 'own', 'edit': 'own'},
            services={'view': 'company'},
            commissions={'view': 'own'},
            reports={'view': 'own'},
            leave={'view': 'own', 'create': 'own'},
            attendance={'view': 'own', 'create': 'own'},
            expenses={'view': 'own', 'create': 'own'},
        ),
        'field_groups': ['internal_notes'],
    },
    {
        'code': 'telecaller', 'name': 'Telecaller / Lead Coordinator', 'home': 'crm:lead_list',
        'description': 'Create, import, qualify and assign leads.',
        'grants': _grant(
            # Own leads plus the unassigned queue (anyone with 'assign' sees it); not other executives' leads.
            leads={'view': 'own', 'create': 'own', 'edit': 'own', 'assign': 'company', 'import': 'company'},
            services={'view': 'company'},
            leave={'view': 'own', 'create': 'own'},
            attendance={'view': 'own', 'create': 'own'},
        ),
        'field_groups': ['internal_notes'],
    },
    {
        'code': 'finance_executive', 'name': 'Finance Executive', 'home': 'crm:dashboard_finance',
        'description': 'Invoices, receipts, manual payment verification, expenses, bank matching.',
        'grants': _grant(
            invoices={'view': 'company', 'create': 'company', 'edit': 'company', 'export': 'company'},
            receipts={'view': 'company', 'create': 'company'},
            payments={'view': 'company', 'create': 'company', 'approve': 'company', 'export': 'company'},
            payment_links={'view': 'company', 'create': 'company', 'cancel': 'company'},
            refunds={'view': 'company', 'create': 'company', 'refund': 'company'},
            quotations={'view': 'company'},
            clients={'view': 'company', 'edit': 'company'},
            services={'view': 'company'},
            contracts={'view': 'company'},
            expenses={'view': 'company', 'create': 'company', 'edit': 'company', 'approve': 'company', 'export': 'company'},
            vendors={'view': 'company', 'create': 'company', 'edit': 'company'},
            banking={'view': 'company', 'create': 'company', 'import': 'company'},
            accounting={'view': 'company'},
            reports={'view': 'company', 'export': 'company'},
            leave={'view': 'own', 'create': 'own'},
            attendance={'view': 'own', 'create': 'own'},
        ),
        'field_groups': ['tax_ids', 'bank_details', 'internal_notes', 'cost_price'],
    },
    {
        'code': 'accountant', 'name': 'Accountant', 'home': 'crm:accounting_dashboard',
        'description': 'Journals, ledgers, reconciliations, period close, statements, tax reports.',
        'grants': _grant(
            accounting={'view': 'company', 'create': 'company', 'edit': 'company', 'approve': 'company',
                        'export': 'company', 'configure': 'company', 'override': 'company'},
            banking={'view': 'company', 'create': 'company', 'import': 'company', 'approve': 'company'},
            invoices={'view': 'company', 'create': 'company', 'edit': 'company', 'approve': 'company',
                      'cancel': 'company', 'export': 'company', 'override': 'company'},
            receipts={'view': 'company', 'create': 'company'},
            payments={'view': 'company', 'create': 'company', 'approve': 'company', 'export': 'company'},
            payment_links={'view': 'company', 'approve': 'company'},
            refunds={'view': 'company', 'approve': 'company', 'refund': 'company'},
            quotations={'view': 'company', 'override': 'company'},
            clients={'view': 'company'},
            services={'view': 'company', 'configure': 'company'},
            contracts={'view': 'company'},
            expenses={'view': 'company', 'approve': 'company', 'export': 'company'},
            vendors={'view': 'company'},
            reports={'view': 'company', 'export': 'company'},
            audit={'view': 'company'},
            leave={'view': 'own', 'create': 'own'},
            attendance={'view': 'own', 'create': 'own'},
        ),
        'field_groups': ['tax_ids', 'bank_details', 'internal_notes', 'cost_price'],
    },
    {
        'code': 'hr_manager', 'name': 'HR Manager', 'home': 'crm:hr_dashboard',
        'description': 'Employees, attendance, leave, payroll preparation.',
        'grants': _grant(
            employees={'view': 'company', 'create': 'company', 'edit': 'company', 'export': 'company',
                       'reveal_sensitive': 'company', 'archive': 'company'},
            attendance={'view': 'company', 'create': 'company', 'edit': 'company', 'approve': 'company', 'import': 'company'},
            leave={'view': 'company', 'create': 'company', 'approve': 'company', 'configure': 'company'},
            payroll={'view': 'company', 'create': 'company', 'edit': 'company'},
            commissions={'view': 'company'},
            reports={'view': 'company'},
            expenses={'view': 'own', 'create': 'own'},
        ),
        'field_groups': ['salary', 'bank_details', 'tax_ids', 'internal_notes'],
    },
    {
        'code': 'project_manager', 'name': 'Project Manager', 'home': 'crm:project_list',
        'description': 'Projects, tasks, milestones, delivery costs.',
        'grants': _grant(
            projects={'view': 'own', 'create': 'company', 'edit': 'own', 'approve': 'own', 'export': 'own'},
            tasks={'view': 'team', 'create': 'team', 'edit': 'team', 'approve': 'team', 'assign': 'team'},
            tickets={'view': 'company', 'create': 'company', 'edit': 'company', 'assign': 'company'},
            clients={'view': 'company'},
            contracts={'view': 'company'},
            services={'view': 'company'},
            reports={'view': 'own'},
            leave={'view': 'own', 'create': 'own'},
            attendance={'view': 'own', 'create': 'own'},
            expenses={'view': 'own', 'create': 'own'},
        ),
        'field_groups': ['internal_notes', 'cost_price'],
    },
    {
        'code': 'team_member', 'name': 'Developer / Designer / Tester', 'home': 'crm:my_day',
        'description': 'Assigned tasks, timesheets, blockers.',
        'grants': _grant(
            projects={'view': 'own'},
            tasks={'view': 'own', 'create': 'own', 'edit': 'own'},
            tickets={'view': 'own', 'edit': 'own'},
            leave={'view': 'own', 'create': 'own'},
            attendance={'view': 'own', 'create': 'own'},
            expenses={'view': 'own', 'create': 'own'},
        ),
        'field_groups': [],
    },
    {
        'code': 'business_manager', 'name': 'Business Manager (AI)', 'home': 'crm:dashboard_owner',
        'description': 'Service-account role for the MCP server: create, view and edit business records '
                       'company-wide. No delete, approve, override or configure. Accounting, banking and '
                       'payroll are view-only here - an AI may read financial reports but not post entries, '
                       'manage bank accounts or touch payroll runs.',
        'grants': _grant(
            **{m: {'view': 'company', 'create': 'company', 'edit': 'company', 'export': 'company'}
               for m in ('leads', 'deals', 'quotations', 'payment_links', 'payments', 'invoices', 'receipts',
                         'clients', 'services', 'contracts', 'projects', 'tasks', 'tickets', 'expenses', 'vendors',
                         'employees', 'attendance', 'leave', 'commissions', 'reports')},
            accounting={'view': 'company'}, banking={'view': 'company'}, payroll={'view': 'company'},
            refunds={'view': 'company'}, audit={'view': 'company'},
        ),
        'field_groups': ['internal_notes', 'tax_ids', 'cost_price', 'salary'],
    },
    {
        'code': 'auditor', 'name': 'Auditor', 'home': 'crm:dashboard_owner',
        'description': 'Read-only across approved modules, export reports.',
        'grants': {m: {'view': 'company'} for m in MODULES if m not in ('payroll', 'employees', 'settings', 'chats', 'ai_analyst')}
        | {'reports': {'view': 'company', 'export': 'company'}, 'audit': {'view': 'company', 'export': 'company'}},
        'field_groups': ['tax_ids'],
    },
]
