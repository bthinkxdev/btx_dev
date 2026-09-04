"""CRM role-based access for Phase 3 provisioning and credential vault."""

from django.contrib.auth import get_user_model

from .models import EmployeeProfile, ProjectCredential

User = get_user_model()

ROLE_ADMIN = 'admin'
ROLE_DEV = 'dev'
ROLE_SUPPORT = 'support'
ROLE_SALES = 'sales'
ROLE_FINANCE = 'finance'


def is_crm_developer(user) -> bool:
    """Logged-in users with CRM role *dev* (not superusers)."""
    return bool(
        user
        and user.is_authenticated
        and not user.is_superuser
        and get_crm_role(user) == ROLE_DEV
    )


def can_access_sales_pipeline(user) -> bool:
    """
    Leads, follow-ups, packages, performance, renewals list, achievements.
    Developers work on delivery only — excluded here.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return get_crm_role(user) != ROLE_DEV


def can_view_financial_data(user) -> bool:
    """Deal value, advance, revenue, package pricing columns, etc."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return get_crm_role(user) != ROLE_DEV


def is_sales_manager(user) -> bool:
    """Sales manager flag (EmployeeProfile.is_sales_manager) — not superuser, not a separate crm_role."""
    if not user or not user.is_authenticated or user.is_superuser:
        return False
    try:
        return bool(user.crm_profile.is_sales_manager)
    except EmployeeProfile.DoesNotExist:
        return False


def can_view_all_sales_data(user) -> bool:
    """Sales managers (and admins) see/edit the whole sales team's leads, follow-ups, achievements."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return get_crm_role(user) == ROLE_ADMIN or is_sales_manager(user)


def get_crm_role(user) -> str:
    if not user or not user.is_authenticated:
        return ROLE_SALES
    if user.is_superuser:
        return ROLE_ADMIN
    try:
        prof = user.crm_profile
        return prof.crm_role or ROLE_SALES
    except EmployeeProfile.DoesNotExist:
        return ROLE_SALES


def can_access_operations_dashboard(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV, ROLE_SUPPORT, ROLE_SALES)
    )


def can_manage_provisioning(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV)
    )


def can_view_secret_material(user) -> bool:
    """Decrypt passwords / API secrets in UI."""
    return user.is_authenticated and (
        user.is_superuser or get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV)
    )


def can_edit_credentials(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV)
    )


def can_view_credential_audit(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV)
    )


def can_complete_handover(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV, ROLE_SUPPORT)
    )


def can_manage_portal(user) -> bool:
    """Activate / deactivate tenant handover portal (admin or developer)."""
    return user.is_authenticated and (
        user.is_superuser or get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV)
    )


def can_access_renewals_dashboard(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser
        or get_crm_role(user) in (ROLE_ADMIN, ROLE_SUPPORT, ROLE_SALES)
    )


def can_access_audit_trail(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV)
    )


def can_access_change_requests(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser
        or get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV, ROLE_SUPPORT)
    )


def can_edit_package_scope(user) -> bool:
    return user.is_authenticated and (
        user.is_superuser or get_crm_role(user) == ROLE_ADMIN
    )


def can_send_renewal_reminder_manual(user) -> bool:
    """Manual reminder resend (admin only)."""
    return user.is_authenticated and (user.is_superuser or get_crm_role(user) == ROLE_ADMIN)


def can_access_billing(user) -> bool:
    """Billing, ledger, statements, and payment recording (admin only)."""
    return user.is_authenticated and (user.is_superuser or get_crm_role(user) == ROLE_ADMIN)


def can_access_finance(user) -> bool:
    """
    Finance module (income, expense, management reports).
    Admin, support, and finance roles. Sales and developers excluded.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return get_crm_role(user) in (ROLE_ADMIN, ROLE_SUPPORT, ROLE_FINANCE)


def credential_type_is_secret(ct: str) -> bool:
    return ct in (
        ProjectCredential.CredentialType.API_KEY,
        ProjectCredential.CredentialType.WEBHOOK_SECRET,
        ProjectCredential.CredentialType.SMTP_SECRET,
        ProjectCredential.CredentialType.INFRA,
    )


def credential_allowed_for_role(user, cred: ProjectCredential) -> bool:
    """
    Whether this user may see this credential row at all (list/detail shell).
    """
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if cred.visibility_level == ProjectCredential.VisibilityLevel.INTERNAL:
        return get_crm_role(user) in (ROLE_ADMIN, ROLE_DEV)
    role = get_crm_role(user)
    if role == ROLE_ADMIN:
        return True
    if role == ROLE_DEV:
        return cred.visibility_level in (
            ProjectCredential.VisibilityLevel.SHARED,
            ProjectCredential.VisibilityLevel.CLIENT,
            ProjectCredential.VisibilityLevel.SALES,
            ProjectCredential.VisibilityLevel.SUPPORT,
            ProjectCredential.VisibilityLevel.DEV,
            ProjectCredential.VisibilityLevel.ADMIN,
        ) and not credential_type_is_secret(cred.credential_type)
    if role == ROLE_SALES:
        return False
    return False


def credential_decrypt_allowed(user, cred: ProjectCredential) -> bool:
    """May decrypt password / secret fields for reveal/copy."""
    if cred.visibility_level == ProjectCredential.VisibilityLevel.INTERNAL:
        return can_view_secret_material(user)
    if credential_type_is_secret(cred.credential_type):
        return can_view_secret_material(user)
    role = get_crm_role(user)
    if role in (ROLE_ADMIN, ROLE_DEV):
        return True
    if role == ROLE_SUPPORT and cred.visibility_level in (
        ProjectCredential.VisibilityLevel.SHARED,
        ProjectCredential.VisibilityLevel.CLIENT,
        ProjectCredential.VisibilityLevel.SUPPORT,
    ):
        return cred.credential_type in (
            ProjectCredential.CredentialType.ADMIN_LOGIN,
            ProjectCredential.CredentialType.DELIVERY_LOGIN,
            ProjectCredential.CredentialType.MAILBOX_LOGIN,
        )
    return False


def client_portal_credential_projection(cred: ProjectCredential) -> dict:
    """
    Safe dict for tenant handover / client-visible exports only.
    Never includes decrypted secrets or secret-bearing types.
    Tenant portal: SHARED visibility only.
    """
    if cred.visibility_level != ProjectCredential.VisibilityLevel.SHARED:
        return {}
    if cred.credential_type not in (
        ProjectCredential.CredentialType.ADMIN_LOGIN,
        ProjectCredential.CredentialType.DELIVERY_LOGIN,
        ProjectCredential.CredentialType.MAILBOX_LOGIN,
    ):
        return {}
    return {
        'label': cred.label,
        'username': cred.username,
        'login_url': cred.login_url,
        'provider_name': cred.provider_name,
        'provider_type': cred.provider_type,
    }
