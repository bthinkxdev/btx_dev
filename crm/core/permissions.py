"""
Central permission service (Part 3, 15.1).

Every request goes through a three-part check:
  1. does the user hold the action permission for the module,
  2. is the record inside the user's data scope (own / team / department / company),
  3. does the record's status allow the action (checked by the service layer).

Roles are bundles of grants an administrator edits; effective permission is the
union of a user's roles, with the widest scope winning. Sensitive field groups
use explicit deny-over-allow.
"""
from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied
from django.db.models import Q

logger = logging.getLogger('crm.permissions')

MODULES = {
    'leads': 'Leads',
    'deals': 'Pipeline and targets',
    'quotations': 'Quotations',
    'payment_links': 'Payment links',
    'payments': 'Payments',
    'invoices': 'Invoices',
    'receipts': 'Receipts',
    'refunds': 'Refunds',
    'clients': 'Clients',
    'services': 'Service catalogue',
    'contracts': 'Contracts',
    'projects': 'Projects',
    'tasks': 'Tasks and timesheets',
    'tickets': 'Support tickets',
    'expenses': 'Expenses',
    'vendors': 'Vendors',
    'accounting': 'Accounting',
    'banking': 'Banking',
    'employees': 'Employees',
    'attendance': 'Attendance',
    'leave': 'Leave',
    'payroll': 'Payroll',
    'commissions': 'Commissions',
    'reports': 'Reports',
    'automation': 'Automation and jobs',
    'settings': 'Settings',
    'audit': 'Audit log',
}

ACTIONS = {
    'view': 'View',
    'create': 'Create',
    'edit': 'Edit',
    'archive': 'Delete or archive',
    'approve': 'Approve',
    'export': 'Export',
    'assign': 'Assign',
    'cancel': 'Cancel',
    'refund': 'Refund',
    'override': 'Override',
    'reveal_sensitive': 'Reveal sensitive',
    'import': 'Import',
    'configure': 'Configure',
}

SCOPES = ['own', 'team', 'department', 'company']
SCOPE_RANK = {s: i for i, s in enumerate(SCOPES)}
SCOPE_LABELS = {'own': 'Own', 'team': 'Team', 'department': 'Department', 'company': 'Company'}

FIELD_GROUPS = {
    'salary': 'Salary',
    'bank_details': 'Bank details',
    'tax_ids': 'PAN / GSTIN / tax identifiers',
    'gateway_credentials': 'Gateway credentials',
    'internal_notes': 'Internal notes',
    'cost_price': 'Cost price and margin',
}

# How each module's records relate to a user for scope evaluation.
OWNER_FIELDS = {
    'leads': ('owner',),
    'deals': ('owner',),
    'quotations': ('owner',),
    'payment_links': ('owner',),
    'payments': ('client__owner',),
    'invoices': ('owner',),
    'receipts': ('client__owner',),
    'refunds': ('payment__client__owner',),
    'clients': ('owner',),
    'contracts': ('owner',),
    'projects': ('manager', 'members__user'),
    'tasks': ('assignee', 'project__manager'),
    'tickets': ('assignee',),
    'expenses': ('created_by', 'employee'),
    'employees': ('user',),
    'attendance': ('employee__user',),
    'leave': ('employee__user',),
    'payroll': ('employee__user',),
    'commissions': ('user',),
}
MULTI_VALUED = ('members__',)


def _profile(user):
    try:
        return user.crm_profile
    except Exception:
        return None


def _load(user) -> dict:
    """Build and cache the user's effective grants for this request."""
    cache = getattr(user, '_crm_perm_cache', None)
    if cache is not None:
        return cache
    grants: dict[str, dict[str, str]] = {}
    allow_groups: set[str] = set()
    deny_groups: set[str] = set()
    role_codes: set[str] = set()
    if user.is_authenticated and user.is_active:
        if user.is_superuser:
            grants = {m: {a: 'company' for a in ACTIONS} for m in MODULES}
            allow_groups = set(FIELD_GROUPS)
            role_codes = {'owner'}
        else:
            profile = _profile(user)
            if profile is not None:
                for role in profile.roles.all():
                    role_codes.add(role.code)
                    for module, actions in (role.grants or {}).items():
                        for action, scope in (actions or {}).items():
                            if scope not in SCOPE_RANK:
                                continue
                            current = grants.setdefault(module, {}).get(action)
                            if current is None or SCOPE_RANK[scope] > SCOPE_RANK[current]:
                                grants[module][action] = scope
                    allow_groups.update(role.field_groups or [])
                    deny_groups.update(role.denied_field_groups or [])
    cache = {'grants': grants, 'groups': allow_groups - deny_groups, 'roles': role_codes}
    try:
        user._crm_perm_cache = cache
    except AttributeError:  # AnonymousUser is immutable-ish
        pass
    return cache


def clear_cache(user):
    if hasattr(user, '_crm_perm_cache'):
        del user._crm_perm_cache


def get_scope(user, module: str, action: str) -> str | None:
    if not user or not user.is_authenticated:
        return None
    return _load(user)['grants'].get(module, {}).get(action)


def has_perm(user, module: str, action: str) -> bool:
    return get_scope(user, module, action) is not None


def has_scope(user, module: str, action: str, minimum: str = 'company') -> bool:
    """True when the user's scope on module.action is at least `minimum` (e.g. company-wide only)."""
    scope = get_scope(user, module, action)
    return scope is not None and SCOPE_RANK[scope] >= SCOPE_RANK[minimum]


def has_any(user, module: str) -> bool:
    return bool(user and user.is_authenticated and _load(user)['grants'].get(module))


def has_role(user, *codes: str) -> bool:
    return bool(set(codes) & _load(user)['roles']) if user and user.is_authenticated else False


def can_see(user, field_group: str) -> bool:
    return bool(user and user.is_authenticated and field_group in _load(user)['groups'])


def denied(user, module: str, action: str, request=None, detail: str = ''):
    """Raise 403 and write a security event (Part 4.12: 'Try to delete a lead via URL -> 403 and audit event')."""
    from crm.core import audit
    audit.security_event('forbidden', user=user, request=request,
                         detail=f'{module}.{action} {detail}'.strip())
    raise PermissionDenied(f'You do not have access to {MODULES.get(module, module)}. '
                           f'Ask an administrator for the {ACTIONS.get(action, action)} permission.')


def require(user, module: str, action: str, request=None):
    if not has_perm(user, module, action):
        denied(user, module, action, request)


def scope_q(user, module: str, action: str, owner_fields: tuple[str, ...] | None = None) -> Q | None:
    """Q restricting a queryset to the user's scope. None means 'all rows'. Raises if no grant."""
    scope = get_scope(user, module, action)
    if scope is None:
        raise PermissionDenied(f'You do not have access to {MODULES.get(module, module)}.')
    if scope == 'company':
        return None
    fields = owner_fields or OWNER_FIELDS.get(module, ('owner',))
    profile = _profile(user)
    q = Q()
    for f in fields:
        q |= Q(**{f: user})
        if scope == 'team' and profile and profile.team_id:
            q |= Q(**{f'{f}__crm_profile__team_id': profile.team_id})
        if scope == 'department' and profile:
            if profile.department_id:
                q |= Q(**{f'{f}__crm_profile__department_id': profile.department_id})
            if profile.team_id:
                q |= Q(**{f'{f}__crm_profile__team_id': profile.team_id})
    return q


def scoped(qs, user, module: str, action: str = 'view', owner_fields: tuple[str, ...] | None = None, extra_q: Q | None = None):
    q = scope_q(user, module, action, owner_fields)
    if q is None:
        return qs
    if extra_q is not None:
        q |= extra_q
    qs = qs.filter(q)
    fields = owner_fields or OWNER_FIELDS.get(module, ())
    if any(m in f for f in fields for m in MULTI_VALUED):
        qs = qs.distinct()
    return qs


def in_scope(user, module: str, action: str, obj, owner_fields=None, extra_q: Q | None = None) -> bool:
    try:
        q = scope_q(user, module, action, owner_fields)
    except PermissionDenied:
        return False
    if q is None:
        return True
    if extra_q is not None:
        q |= extra_q
    return type(obj)._default_manager.filter(q, pk=obj.pk).exists()


def require_object(user, module: str, action: str, obj, request=None, owner_fields=None, extra_q: Q | None = None):
    if not in_scope(user, module, action, obj, owner_fields, extra_q):
        denied(user, module, action, request, detail=f'{type(obj).__name__}:{obj.pk}')


def team_member_ids(user) -> list[int]:
    """Users in the same team (for manager pickers and scope-limited dropdowns)."""
    from django.contrib.auth import get_user_model
    profile = _profile(user)
    User = get_user_model()
    if not profile or not profile.team_id:
        return [user.pk]
    return list(User.objects.filter(crm_profile__team_id=profile.team_id).values_list('pk', flat=True))


def is_manager_of(manager, user) -> bool:
    if manager.is_superuser:
        return True
    p = _profile(user)
    if p and p.manager_id == manager.pk:
        return True
    return bool(p and p.team_id and p.team.manager_id == manager.pk)
