"""Idempotent seed data: roles, chart of accounts, numbering series, defaults (run by crm_bootstrap / post_migrate)."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from . import posting as P
from .numbering import DEFAULT_SERIES
from .roles import ROLE_TEMPLATES

MCP_SERVICE_ACCOUNT = 'business_manager_ai'

# (code, name, type, group, system_code)
CHART_OF_ACCOUNTS = [
    ('1000', 'Bank accounts', 'asset', 'Bank', ''),
    ('1010', 'Primary bank account', 'asset', 'Bank', 'BANK_PRIMARY'),
    ('1050', 'Payment gateway clearing', 'asset', 'Bank', P.GATEWAY_CLEARING),
    ('1060', 'Cash and petty cash', 'asset', 'Cash', 'CASH'),
    ('1100', 'Accounts receivable', 'asset', 'Receivables', P.AR),
    ('1150', 'Advances paid to vendors', 'asset', 'Receivables', 'VENDOR_ADVANCES'),
    ('1200', 'TDS receivable', 'asset', 'Tax', P.TDS_RECEIVABLE),
    ('1210', 'Input CGST', 'asset', 'Tax', P.INPUT_CGST),
    ('1211', 'Input SGST', 'asset', 'Tax', P.INPUT_SGST),
    ('1212', 'Input IGST', 'asset', 'Tax', P.INPUT_IGST),
    ('1300', 'Prepaid expenses', 'asset', 'Current assets', 'PREPAID'),
    ('1500', 'Fixed assets', 'asset', 'Fixed assets', 'FIXED_ASSETS'),
    ('1510', 'Accumulated depreciation', 'asset', 'Fixed assets', 'ACC_DEPRECIATION'),
    ('1600', 'Security deposits', 'asset', 'Current assets', 'DEPOSITS'),
    ('1900', 'Suspense', 'asset', 'Current assets', P.SUSPENSE),
    ('2000', 'Accounts payable', 'liability', 'Payables', P.AP),
    ('2100', 'Customer advances (unearned)', 'liability', 'Current liabilities', P.CUSTOMER_ADVANCES),
    ('2200', 'Output CGST', 'liability', 'Tax', P.OUTPUT_CGST),
    ('2201', 'Output SGST', 'liability', 'Tax', P.OUTPUT_SGST),
    ('2202', 'Output IGST', 'liability', 'Tax', P.OUTPUT_IGST),
    ('2210', 'GST payable', 'liability', 'Tax', 'GST_PAYABLE'),
    ('2300', 'TDS payable', 'liability', 'Tax', P.TDS_PAYABLE),
    ('2310', 'PF, ESI and professional tax payable', 'liability', 'Statutory', P.STATUTORY_PAYABLE),
    ('2400', 'Salaries payable', 'liability', 'Payroll', P.SALARIES_PAYABLE),
    ('2410', 'Reimbursements payable', 'liability', 'Payroll', P.REIMBURSEMENTS_PAYABLE),
    ('2500', 'Deferred revenue', 'liability', 'Current liabilities', P.DEFERRED_REVENUE),
    ('2600', 'Loans', 'liability', 'Loans', 'LOANS'),
    ('3000', 'Owner capital', 'equity', 'Equity', P.OWNER_CAPITAL),
    ('3100', 'Owner drawings', 'equity', 'Equity', P.OWNER_DRAWINGS),
    ('3200', 'Retained earnings', 'equity', 'Equity', P.RETAINED_EARNINGS),
    ('4000', 'Service revenue', 'income', 'Revenue', P.SERVICE_REVENUE),
    ('4100', 'Recurring revenue', 'income', 'Revenue', P.RECURRING_REVENUE),
    ('4200', 'Other income', 'income', 'Other income', 'OTHER_INCOME'),
    ('4210', 'Interest income', 'income', 'Other income', P.INTEREST_INCOME),
    ('4300', 'Bad-debt recovery', 'income', 'Other income', P.BAD_DEBT_RECOVERY),
    ('4900', 'Discounts allowed', 'income', 'Contra revenue', P.DISCOUNTS_ALLOWED),
    ('5000', 'Project labour cost', 'direct_cost', 'Direct costs', 'PROJECT_LABOUR'),
    ('5100', 'Subcontractors', 'direct_cost', 'Direct costs', 'SUBCONTRACTORS'),
    ('5200', 'Software and hosting for delivery', 'direct_cost', 'Direct costs', 'DELIVERY_HOSTING'),
    ('6000', 'Salaries', 'expense', 'People', P.SALARY_EXPENSE),
    ('6010', 'Employer contributions', 'expense', 'People', P.EMPLOYER_CONTRIB),
    ('6020', 'Commissions', 'expense', 'People', P.COMMISSION_EXPENSE),
    ('6100', 'Rent', 'expense', 'Operations', 'RENT'),
    ('6200', 'Marketing', 'expense', 'Sales and marketing', 'MARKETING'),
    ('6300', 'Payment gateway fees', 'expense', 'Finance costs', P.GATEWAY_FEES),
    ('6310', 'GST on gateway fees', 'expense', 'Finance costs', P.GST_ON_FEES),
    ('6320', 'Bank charges', 'expense', 'Finance costs', P.BANK_CHARGES),
    ('6400', 'Software subscriptions', 'expense', 'Operations', 'SOFTWARE'),
    ('6500', 'Travel', 'expense', 'Operations', 'TRAVEL'),
    ('6600', 'Professional fees', 'expense', 'Operations', 'PROFESSIONAL_FEES'),
    ('6700', 'Bad debts', 'expense', 'Finance costs', P.BAD_DEBTS),
    ('6800', 'Depreciation', 'expense', 'Operations', 'DEPRECIATION'),
    ('6900', 'Round-off', 'expense', 'Other', P.ROUND_OFF),
    ('6950', 'Office and miscellaneous', 'expense', 'Operations', 'MISC'),
]

EXPENSE_CATEGORIES = [
    ('Rent', 'RENT', 'blocked', '194I', '10.00', '240000.00', False),
    ('Marketing and ads', 'MARKETING', 'eligible', '', '0', '0', False),
    ('Software subscriptions', 'SOFTWARE', 'eligible', '', '0', '0', False),
    ('Travel', 'TRAVEL', 'blocked', '', '0', '0', False),
    ('Professional fees', 'PROFESSIONAL_FEES', 'eligible', '194J', '10.00', '30000.00', False),
    ('Subcontractors', 'SUBCONTRACTORS', 'eligible', '194C', '1.00', '30000.00', True),
    ('Hosting for delivery', 'DELIVERY_HOSTING', 'eligible', '', '0', '0', True),
    ('Office and miscellaneous', 'MISC', 'eligible', '', '0', '0', False),
]

SOURCES = [
    ('Website form', 'website'), ('Meta lead forms', 'meta'), ('Google Ads', 'google'), ('Phone call', 'phone'),
    ('Missed call', 'missed_call'), ('Email to sales', 'email'), ('WhatsApp', 'whatsapp'),
    ('Manual entry', 'manual'), ('CSV import', 'csv'), ('Referral', 'referral'), ('Existing client', 'existing_client'),
]

LEAVE_TYPES = [('CL', 'Casual leave', '12', True), ('SL', 'Sick leave', '12', True), ('EL', 'Earned leave', '15', True),
               ('LOP', 'Leave without pay', '0', False)]

SALARY_COMPONENTS = [
    ('basic', 'Basic', 'earning', 'fixed', 1), ('hra', 'House rent allowance', 'earning', 'pct_basic', 2),
    ('special', 'Special allowance', 'earning', 'fixed', 3),
    ('pf_employee', 'PF (employee)', 'deduction', 'pct_basic', 10), ('pt', 'Professional tax', 'deduction', 'fixed', 11),
    ('tds', 'TDS on salary', 'deduction', 'fixed', 12),
    ('pf_employer', 'PF (employer)', 'employer', 'pct_basic', 20),
]

MESSAGE_TEMPLATES = [
    ('quotation_send', 'Send quotation', 'Quotation {{ number }} from {{ company }}',
     'Dear {{ name }},\n\nPlease find our quotation {{ number }} for {{ total }} attached.\n'
     'You can also view it online: {{ link }}\n\nIt is valid until {{ valid_until }}.\n\nRegards,\n{{ sender }}\n{{ company }}'),
    ('payment_link_send', 'Send payment link', 'Payment request {{ amount }} from {{ company }}',
     'Dear {{ name }},\n\nPlease use this secure link to pay {{ amount }} for {{ description }}:\n{{ link }}\n\n'
     'The link is valid until {{ expires }}.\n\nRegards,\n{{ sender }}\n{{ company }}'),
    ('invoice_send', 'Send invoice', 'Invoice {{ number }} from {{ company }}',
     'Dear {{ name }},\n\nPlease find invoice {{ number }} for {{ total }} attached, due on {{ due_date }}.\n'
     '{% if link %}Pay securely online: {{ link }}\n{% endif %}\nRegards,\n{{ sender }}\n{{ company }}'),
    ('invoice_reminder', 'Payment reminder', 'Reminder: invoice {{ number }} — {{ balance }} due',
     'Dear {{ name }},\n\nThis is a reminder that {{ balance }} is due on invoice {{ number }} (due {{ due_date }}).\n'
     '{% if link %}Pay securely online: {{ link }}\n{% endif %}\nIf you have already paid, please ignore this message.\n\n'
     'Regards,\n{{ company }}'),
    ('statement_send', 'Statement of account', 'Statement of account from {{ company }}',
     'Dear {{ name }},\n\nPlease find your statement of account attached. Outstanding balance: {{ balance }}.\n\n'
     'Regards,\n{{ company }}'),
]


@transaction.atomic
def run(verbose: bool = False) -> dict:
    from crm.models import (Account, BankAccount, CompanySettings, ExpenseCategory, LeadSource, LeaveType,
                            MessageTemplate, Role, SalaryComponent, SequenceSeries)
    out = {}
    CompanySettings.get()

    created = 0
    for tpl in ROLE_TEMPLATES:
        role, was_created = Role.objects.get_or_create(code=tpl['code'], defaults={
            'name': tpl['name'], 'description': tpl['description'], 'grants': tpl['grants'],
            'field_groups': tpl['field_groups'], 'home': tpl['home'], 'is_system': True})
        created += was_created
    out['roles'] = created

    created = 0
    by_code = {}
    for code, name, typ, group, system in CHART_OF_ACCOUNTS:
        acc, was_created = Account.objects.get_or_create(code=code, defaults={
            'name': name, 'account_type': typ, 'group': group, 'system_code': system, 'is_system': bool(system),
            'allow_manual': system not in (P.AR, P.CUSTOMER_ADVANCES, P.GATEWAY_CLEARING)})
        by_code[code] = acc
        created += was_created
    parent = by_code['1000']
    Account.objects.filter(code__in=['1010', '1050', '1060'], parent__isnull=True).update(parent=parent)
    out['accounts'] = created

    if not BankAccount.objects.exists():
        BankAccount.objects.create(name='Primary bank account', kind='bank', ledger_account=by_code['1010'],
                                   is_default_receipt=True)
        BankAccount.objects.create(name='Payment gateway', kind='gateway', ledger_account=by_code['1050'])
        BankAccount.objects.create(name='Petty cash', kind='cash', ledger_account=by_code['1060'])

    for doc_type, (name, prefix) in DEFAULT_SERIES.items():
        SequenceSeries.objects.get_or_create(doc_type=doc_type, defaults={'name': name, 'prefix': prefix,
                                                                          'padding': 4})
    for name, channel in SOURCES:
        LeadSource.objects.get_or_create(name=name, defaults={'channel': channel})
    for name, sys_code, itc, tds, rate, threshold, direct in EXPENSE_CATEGORIES:
        acc = Account.objects.get(system_code=sys_code)
        ExpenseCategory.objects.get_or_create(name=name, defaults={
            'account': acc, 'itc_default': itc, 'tds_section': tds, 'tds_rate': Decimal(rate),
            'tds_threshold': Decimal(threshold), 'is_direct_cost': direct})
    for code, name, quota, paid in LEAVE_TYPES:
        LeaveType.objects.get_or_create(code=code.lower(), defaults={'name': name, 'annual_quota': Decimal(quota),
                                                                     'is_paid': paid})
    for code, name, kind, calc, sort in SALARY_COMPONENTS:
        defaults = {'name': name, 'kind': kind, 'calculation': calc, 'sort': sort}
        if code == 'hra':
            defaults['default_value'] = Decimal('40')
        if code in ('pf_employee', 'pf_employer'):
            defaults['default_value'] = Decimal('12')
        if code == 'pt':
            defaults['default_value'] = Decimal('200')
            defaults['prorate'] = False
        SalaryComponent.objects.get_or_create(code=code, defaults=defaults)
    for code, name, subject, body in MESSAGE_TEMPLATES:
        MessageTemplate.objects.get_or_create(code=code, channel='email', version=1,
                                              defaults={'name': name, 'subject': subject, 'body': body})

    from django.contrib.auth import get_user_model
    from crm.models import UserProfile
    User = get_user_model()
    bm_user, bm_created = User.objects.get_or_create(username=MCP_SERVICE_ACCOUNT, defaults={
        'first_name': 'Business Manager', 'last_name': '(AI)', 'email': f'{MCP_SERVICE_ACCOUNT}@service.local',
        'is_active': True})
    if bm_created:
        bm_user.set_unusable_password()
        bm_user.save()
    profile, _ = UserProfile.objects.get_or_create(user=bm_user)
    profile.roles.set(Role.objects.filter(code='business_manager'))
    profile.receives_leads = False
    profile.save()
    out['mcp_service_account'] = bm_created
    return out
