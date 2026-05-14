"""Package scope validation and summaries."""

from __future__ import annotations

from ..models import AuditEntry, OnboardingSubmission, Package, Project
from . import audit as audit_service

FEATURE_KEY_MAP = {
    'ecommerce': 'includes_ecommerce',
    'blog': 'includes_blog',
    'booking': 'includes_booking',
    'multi_vendor': 'includes_multi_vendor',
    'custom_domain': 'includes_custom_domain',
    'payment_gateway': 'includes_payment_gateway',
    'delivery_integration': 'includes_delivery_integration',
    'smtp_setup': 'includes_smtp_setup',
    'seo_basic': 'includes_seo_basic',
    'social_media_setup': 'includes_social_media_setup',
    'marketing_ads': 'includes_marketing_ads',
}

FEATURE_LABELS = {
    'ecommerce': 'E-commerce / storefront',
    'blog': 'Blog',
    'booking': 'Booking',
    'multi_vendor': 'Multi-vendor',
    'custom_domain': 'Custom domain',
    'payment_gateway': 'Payment gateway',
    'delivery_integration': 'Delivery integration',
    'smtp_setup': 'SMTP / email setup',
    'seo_basic': 'Basic SEO',
    'social_media_setup': 'Social media setup',
    'marketing_ads': 'Marketing / ads',
}

# Map onboarding website_requirements JSON keys to scope feature keys (if any).
ONBOARDING_REQ_TO_FEATURE = {
    'cart': 'ecommerce',
    'wishlist': 'ecommerce',
    'subscription': 'ecommerce',
    'login': 'ecommerce',
    'payment_gateway': 'payment_gateway',
    'booking': 'booking',
    'blog': 'blog',
}


def _package_scope_or_none(package) -> tuple:
    if not package or not getattr(package, 'pk', None):
        return None, None
    try:
        sc = package.scope
    except Exception:
        return package, None
    return package, sc


def validate_onboarding_requirements(submission: OnboardingSubmission) -> dict:
    """
    Checks website_requirements flags against the project's PackageScope.
    Non-blocking: returns violations / warnings / clean.
    """
    project = submission.project
    package = project.package
    _, scope = _package_scope_or_none(package)
    if scope is None:
        return {'violations': [], 'warnings': [], 'clean': True}

    data = submission.website_requirements or {}
    if not isinstance(data, dict):
        data = {}

    requested_features: set[str] = set()
    for req_key, truth in data.items():
        if not truth:
            continue
        feat = ONBOARDING_REQ_TO_FEATURE.get(req_key)
        if feat:
            requested_features.add(feat)

    violations: list[str] = []
    for feat in sorted(requested_features):
        field = FEATURE_KEY_MAP.get(feat)
        if not field:
            continue
        if not getattr(scope, field, False):
            violations.append(feat)

    warnings: list[str] = []
    for feat, field in FEATURE_KEY_MAP.items():
        if getattr(scope, field, False) and feat not in requested_features:
            warnings.append(feat)

    return {
        'violations': violations,
        'warnings': warnings,
        'clean': len(violations) == 0,
    }


def get_scope_summary(package: Package | None) -> dict:
    """
    Structured summary for triage UI and project detail.
    """
    _, scope = _package_scope_or_none(package)
    if scope is None:
        return {
            'features': [],
            'limits': {},
            'exclusions': [],
            'notes': '',
        }

    features = []
    for key, field in FEATURE_KEY_MAP.items():
        features.append(
            {
                'key': key,
                'label': FEATURE_LABELS.get(key, key.replace('_', ' ').title()),
                'included': bool(getattr(scope, field, False)),
            }
        )

    exclusions_raw = (scope.exclusions or '').strip()
    exclusions = [x.strip() for x in exclusions_raw.split(',') if x.strip()]

    return {
        'features': features,
        'limits': {
            'max_pages': scope.max_pages,
            'max_products': scope.max_products,
            'max_admin_users': scope.max_admin_users,
            'storage_gb': scope.storage_gb,
            'support_months': scope.support_months,
            'revision_rounds': scope.revision_rounds,
        },
        'exclusions': exclusions,
        'notes': scope.scope_notes or '',
    }


def enforce_scope_on_project_create(project: Project) -> list[str]:
    """
    After project creation: warn if package scope missing.
    Does not block. Logs AuditEntry category SCOPE, action scope_checked.
    """
    warnings: list[str] = []
    package = project.package
    _, scope = _package_scope_or_none(package)
    if package is None:
        warnings.append('Project has no package assigned — scope not checked.')
    elif scope is None:
        warnings.append(
            f'Package "{package.name}" has no scope record yet — run migrations or save package.'
        )

    audit_service.log_event(
        category=AuditEntry.EventCategory.SCOPE,
        action='scope_checked',
        object_type='Project',
        object_id=project.pk,
        object_repr=str(project)[:200],
        actor=None,
        project=project,
        after_state={'warnings': warnings},
        note='; '.join(warnings) if warnings else 'ok',
    )
    return warnings
