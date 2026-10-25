"""Administration services (Part 13.6): users, roles, teams, settings, gateway secrets.

Every change writes an audit row with before and after values. Permission changes take
effect on the user's next request, because grants are cached per request only.
"""
from __future__ import annotations

import secrets
import string

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from crm.core import audit, crypto, permissions

SENSITIVE_COMPANY_FIELDS = ('legal_name', 'gstin', 'pan', 'state_code', 'bank_name', 'bank_account_name', 'bank_account_number',
                            'bank_ifsc', 'bank_branch', 'upi_id', 'gst_registered', 'lut_enabled')


def _snapshot(obj, fields):
    return {f: getattr(obj, f) for f in fields}


def save_settings(company, form, actor, *, reason: str = '', sensitive=()) -> dict:
    """Save a settings form; a reason is required when any sensitive field changes."""
    fields = [f for f in form._meta.fields if f in form.cleaned_data]
    before = _snapshot(company.__class__.objects.get(pk=company.pk), fields)
    after = {f: form.cleaned_data[f] for f in fields}
    changes = audit.diff(before, after)
    if not changes:
        return {}
    touched = [f for f in changes if f in sensitive]
    if touched and len(reason.strip()) < 10:
        raise ValidationError(f'Give a reason of at least 10 characters for changing: '
                              f'{", ".join(company._meta.get_field(f).verbose_name for f in touched)}.')
    with transaction.atomic():
        obj = form.save(commit=False)
        obj.updated_by = actor
        obj.save()
        audit.log('settings.changed', obj, actor=actor, changes=changes, reason=reason.strip())
    return changes


# ---------------------------------------------------------------- users

def temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    core = ''.join(secrets.choice(alphabet) for _ in range(12))
    return f'{core[:4]}-{core[4:8]}-{core[8:]}!'


def _check_roles(actor, roles):
    from crm.modules.hr.services import role_grantable
    for r in roles:
        if not role_grantable(actor, r):
            raise ValidationError(f'You cannot grant "{r.name}": it holds permissions wider than your own.')


def create_user(actor, *, username, first_name, last_name, email, roles, team=None, department=None, manager=None,
                designation='', receives_leads=False):
    from crm.models import UserProfile
    User = get_user_model()
    if User.objects.filter(username__iexact=username).exists():
        raise ValidationError('That username is taken.')
    if email and User.objects.filter(email__iexact=email).exists():
        raise ValidationError('Another user already has this email address.')
    _check_roles(actor, roles)
    password = temp_password()
    with transaction.atomic():
        user = User.objects.create_user(username=username, email=email, password=password, first_name=first_name, last_name=last_name)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.team, profile.department, profile.manager = team, department, manager
        profile.designation, profile.receives_leads, profile.must_change_password = designation, receives_leads, True
        profile.save()
        profile.roles.set(roles)
        audit.log('user.created', user, actor=actor, changes={'roles': [r.code for r in roles], 'team': str(team or ''),
                                                              'email': email})
    return user, password


def update_access(user, actor, *, roles, team, department, manager, designation, receives_leads, open_lead_cap,
                  reason: str):
    """Change roles and placement. Granting is limited to the actor's own permissions."""
    profile = user.crm_profile
    if user.pk == actor.pk and set(roles) != set(profile.roles.all()):
        raise ValidationError('You cannot change your own roles. Ask another administrator.')
    if manager is not None and manager.pk == user.pk:
        raise ValidationError('A user cannot be their own manager.')
    old_roles = set(profile.roles.all())
    added = [r for r in roles if r not in old_roles]
    _check_roles(actor, added)
    if old_roles - set(roles) and any(r.code == 'owner' for r in old_roles - set(roles)):
        from crm.models import Role
        owners = Role.objects.get(code='owner').profiles.filter(user__is_active=True).exclude(pk=profile.pk).count()
        if not owners:
            raise ValidationError('This is the last active Owner. Make someone else Owner first.')
    before = {'roles': sorted(r.code for r in old_roles), 'team': str(profile.team or ''), 'department': str(profile.department or ''),
              'manager': str(profile.manager or ''), 'designation': profile.designation, 'receives_leads': profile.receives_leads,
              'open_lead_cap': profile.open_lead_cap}
    after = {'roles': sorted(r.code for r in roles), 'team': str(team or ''), 'department': str(department or ''),
             'manager': str(manager or ''), 'designation': designation, 'receives_leads': receives_leads, 'open_lead_cap': open_lead_cap}
    changes = audit.diff(before, after)
    if not changes:
        return {}
    if 'roles' in changes and len(reason.strip()) < 10:
        raise ValidationError('Give a reason of at least 10 characters for a role change.')
    with transaction.atomic():
        profile.team, profile.department, profile.manager = team, department, manager
        profile.designation, profile.receives_leads, profile.open_lead_cap = designation, receives_leads, open_lead_cap
        profile.save()
        profile.roles.set(roles)
        audit.log('user.access_changed' if 'roles' in changes else 'user.updated', user, actor=actor, changes=changes, reason=reason)
        if 'roles' in changes:
            audit.security_event('permission_change', user=user, detail=f'roles {before["roles"]} -> {after["roles"]} by {actor}')
    permissions.clear_cache(user)
    return changes


def deactivate(user, actor, *, reason: str, reassign: bool = True) -> dict:
    from crm.modules.hr.services import end_sessions
    from crm.modules.leads.services import reassign_open_work
    if user.pk == actor.pk:
        raise ValidationError('You cannot deactivate yourself.')
    if not user.is_active:
        raise ValidationError('This user is already inactive.')
    if len(reason.strip()) < 10:
        raise ValidationError('Give a reason of at least 10 characters.')
    if user.crm_profile.roles.filter(code='owner').exists():
        from crm.models import Role
        others = Role.objects.get(code='owner').profiles.filter(user__is_active=True).exclude(user=user).count()
        if not others:
            raise ValidationError('This is the last active Owner and cannot be deactivated.')
    with transaction.atomic():
        user.is_active = False
        user.save(update_fields=['is_active'])
        p = user.crm_profile
        p.receives_leads = False
        p.is_available = False
        p.save(update_fields=['receives_leads', 'is_available'])
        summary = reassign_open_work(user, actor, reason=f'User deactivated: {reason}') if reassign else {'moved': 0, 'unassigned': 0, 'total': 0}
        sessions = end_sessions(user)
        audit.log('user.deactivated', user, actor=actor, reason=reason, changes={'leads': summary, 'sessions_ended': sessions})
        audit.security_event('account_deactivated', user=user, detail=reason[:200])
    return {**summary, 'sessions': sessions}


def reactivate(user, actor, reason: str):
    if user.is_active:
        raise ValidationError('This user is already active.')
    if len(reason.strip()) < 10:
        raise ValidationError('Give a reason of at least 10 characters.')
    user.is_active = True
    user.save(update_fields=['is_active'])
    p = user.crm_profile
    p.is_available = True
    p.failed_logins = 0
    p.locked_until = None
    p.save(update_fields=['is_available', 'failed_logins', 'locked_until'])
    audit.log('user.reactivated', user, actor=actor, reason=reason)


def reset_access(user, actor, *, reset_two_factor: bool, reason: str) -> str:
    """Unlock, issue a one-time password the user must change, sign out everywhere."""
    from crm.modules.hr.services import end_sessions
    if user.pk == actor.pk:
        raise ValidationError('Use My profile to change your own password.')
    if len(reason.strip()) < 10:
        raise ValidationError('Give a reason of at least 10 characters.')
    password = temp_password()
    with transaction.atomic():
        user.set_password(password)
        user.save(update_fields=['password'])
        p = user.crm_profile
        p.failed_logins, p.locked_until, p.must_change_password = 0, None, True
        fields = ['failed_logins', 'locked_until', 'must_change_password']
        if reset_two_factor:
            p.totp_enabled, p.totp_secret = False, ''
            fields += ['totp_enabled', 'totp_secret']
        p.save(update_fields=fields)
        n = end_sessions(user)
        audit.log('user.access_reset', user, actor=actor, reason=reason, changes={'two_factor_reset': reset_two_factor, 'sessions_ended': n})
        audit.security_event('access_reset', user=user, detail=f'by {actor}; 2FA reset: {reset_two_factor}')
    return password


# ---------------------------------------------------------------- roles

def save_role_grants(role, actor, *, grants: dict, field_groups: list, denied_groups: list, reason: str) -> dict:
    """Replace a role's matrix. Never above the actor's own permissions; the Owner role keeps full settings access."""
    if len(reason.strip()) < 10:
        raise ValidationError('Give a reason of at least 10 characters for a permission change.')
    clean: dict = {}
    for module, actions in grants.items():
        if module not in permissions.MODULES:
            continue
        for action, scope in actions.items():
            if action not in permissions.ACTIONS or scope not in permissions.SCOPE_RANK:
                continue
            mine = permissions.get_scope(actor, module, action)
            if mine is None or permissions.SCOPE_RANK[mine] < permissions.SCOPE_RANK[scope]:
                raise ValidationError(f'You cannot grant {permissions.ACTIONS[action].lower()} on '
                                      f'{permissions.MODULES[module]} at {scope} scope: it is wider than your own.')
            clean.setdefault(module, {})[action] = scope
    if role.code == 'owner' and clean.get('settings', {}).get('configure') != 'company':
        raise ValidationError('The Owner role must keep Settings: configure at company scope, or nobody could administer the system.')
    groups = [g for g in field_groups if g in permissions.FIELD_GROUPS]
    denied = [g for g in denied_groups if g in permissions.FIELD_GROUPS]
    before = {'grants': role.grants, 'field_groups': role.field_groups, 'denied_field_groups': role.denied_field_groups}
    after = {'grants': clean, 'field_groups': groups, 'denied_field_groups': denied}
    changes = audit.diff(before, after)
    if not changes:
        return {}
    role.grants, role.field_groups, role.denied_field_groups = clean, groups, denied
    role.save()
    audit.log('role.permissions_changed', role, actor=actor, changes=changes, reason=reason)
    audit.security_event('permission_change', detail=f'role {role.code} changed by {actor}: {reason[:150]}')
    return changes


def create_role(actor, *, name: str, code: str, description: str, copy_from=None):
    from crm.models import Role
    if Role.objects.filter(code=code).exists() or Role.objects.filter(name__iexact=name).exists():
        raise ValidationError('A role with this name or code exists.')
    grants = dict(copy_from.grants) if copy_from else {}
    from crm.modules.hr.services import role_grantable
    probe = Role(grants=grants)
    if not role_grantable(actor, probe):
        raise ValidationError('You cannot copy a role that is wider than your own permissions.')
    role = Role.objects.create(name=name, code=code, description=description, grants=grants,
                               field_groups=list(copy_from.field_groups) if copy_from else [],
                               denied_field_groups=list(copy_from.denied_field_groups) if copy_from else [],
                               home=copy_from.home if copy_from else 'crm:my_day')
    audit.log('role.created', role, actor=actor, changes={'copied_from': copy_from.code if copy_from else ''})
    return role


def matrix(role) -> list:
    rows = []
    for m, label in permissions.MODULES.items():
        cells = [(a, (role.grants or {}).get(m, {}).get(a, '')) for a in permissions.ACTIONS]
        rows.append({'module': m, 'label': label, 'cells': cells})
    return rows


def compare(a, b) -> list:
    out = []
    for m, label in permissions.MODULES.items():
        for act, alabel in permissions.ACTIONS.items():
            x = (a.grants or {}).get(m, {}).get(act, '')
            y = (b.grants or {}).get(m, {}).get(act, '')
            if x != y:
                out.append({'module': label, 'action': alabel, 'a': x or '—', 'b': y or '—'})
    return out


# ---------------------------------------------------------------- gateway

def save_gateway(company, actor, *, provider: str, app_id: str, methods: list, secret: str = '', webhook_secret: str = '',
                 live: bool | None = None, reason: str = '') -> dict:
    """Secrets are write-only: stored encrypted, never shown again, never written to the audit log."""
    changes = {}
    if provider != company.gateway_provider:
        if provider == 'sandbox' and company.gateway_live:
            raise ValidationError('The built-in sandbox cannot be used in live mode. Switch to test mode first.')
        changes['provider'] = [company.gateway_provider, provider]
        company.gateway_provider = provider
    if app_id != company.gateway_app_id:
        changes['app_id'] = [company.gateway_app_id[:4] + '…' if company.gateway_app_id else '', app_id[:4] + '…' if app_id else '']
        company.gateway_app_id = app_id
    if sorted(methods) != sorted(company.gateway_methods or []):
        changes['methods'] = [company.gateway_methods, methods]
        company.gateway_methods = methods
    if secret:
        company.gateway_secret_enc = crypto.encrypt(secret)
        company.gateway_secret_rotated_at = timezone.now()
        changes['secret'] = ['(hidden)', 'replaced']
    if webhook_secret:
        company.gateway_webhook_secret_enc = crypto.encrypt(webhook_secret)
        changes['webhook_secret'] = ['(hidden)', 'replaced']
    if live is not None and live != company.gateway_live:
        if not permissions.has_role(actor, 'owner') and not actor.is_superuser:
            raise ValidationError('Only the Owner can switch between test and live mode.')
        if live and company.gateway_provider != 'cashfree':
            raise ValidationError('Live mode needs a real provider (Cashfree).')
        if len(reason.strip()) < 10:
            raise ValidationError('Give a reason of at least 10 characters for switching mode.')
        changes['live'] = [company.gateway_live, live]
        company.gateway_live = live
    if not changes:
        return {}
    company.updated_by = actor
    company.save()
    audit.log('settings.gateway_changed', company, actor=actor, changes=changes, reason=reason)
    return changes


def test_gateway() -> tuple[bool, str]:
    from crm.modules.payments.gateway import get_adapter
    adapter = get_adapter()
    ok, msg = adapter.ready()
    if not ok:
        return False, msg
    probe = getattr(adapter, 'test_connection', None)
    if probe is None:
        return True, f'{adapter.provider.capitalize()} is ready ({adapter.mode}).'
    return probe()
