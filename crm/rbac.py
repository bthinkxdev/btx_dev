"""CRM role-based access: sales pipeline vs. developer role."""

from django.contrib.auth import get_user_model

from .models import EmployeeProfile

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
    """Leads, follow-ups, packages, performance, billing. Developers are excluded."""
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
    """Sales managers (and admins) see/edit the whole sales team's leads and follow-ups."""
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
