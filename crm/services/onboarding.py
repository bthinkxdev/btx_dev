"""Client onboarding: submissions, section saves, verification, staff summary."""

from django.db import transaction
from django.utils import timezone

from ..models import AuditEntry, OnboardingSubmission, Project
from ..utils import log_activity
from . import audit as audit_service

SECTION_STATUS_FIELD = {
    'business_info': 'business_info_status',
    'contact': 'contact_status',
    'branding': 'branding_status',
    'requirements': 'requirements_status',
    'content': 'content_status',
    'documents': 'documents_status',
    'payment_kyc': 'payment_kyc_status',
    'agreement': 'agreement_status',
}

SECTION_MODEL_FIELDS = {
    'business_info': (
        'business_name',
        'tagline',
        'business_description',
        'years_in_business',
        'industry',
        'target_audience',
        'competitors',
        'usp',
    ),
    'contact': (
        'contact_phone',
        'whatsapp_number',
        'contact_email',
        'office_address',
        'google_maps_url',
        'instagram_url',
        'facebook_url',
        'youtube_url',
        'website_url',
    ),
    'branding': ('brand_colors', 'brand_fonts', 'brand_notes'),
    'requirements': ('reference_websites', 'website_requirements'),
    'content': (
        'about_us',
        'privacy_policy',
        'refund_policy',
        'shipping_policy',
        'terms_and_conditions',
        'faq',
    ),
    'documents': (
        'logo_file',
        'gst_certificate',
        'pan_document',
        'business_registration',
        'brand_guidelines',
    ),
    'payment_kyc': (
        'owner_aadhaar_front',
        'owner_aadhaar_back',
        'bank_name',
        'bank_account_number',
        'bank_ifsc',
        'bank_branch_name',
        'phone_registered_bank',
        'phone_registered_aadhaar',
        'phone_registered_pan',
        'kyc_privacy_acknowledged',
    ),
}

SUMMARY_SECTIONS = (
    ('business_info', 'Business Info', 'business_info_status'),
    ('contact', 'Contact', 'contact_status'),
    ('branding', 'Branding', 'branding_status'),
    ('requirements', 'Website requirements', 'requirements_status'),
    ('content', 'Content & policies', 'content_status'),
    ('documents', 'Documents', 'documents_status'),
    ('payment_kyc', 'Payment & owner KYC', 'payment_kyc_status'),
    ('agreement', 'Agreement', 'agreement_status'),
)


PUBLIC_ONBOARDING_FLOW = (
    ('business_info', 'business_info_status', 'Business information'),
    ('contact', 'contact_status', 'Contact & presence'),
    ('branding', 'branding_status', 'Branding'),
    ('requirements', 'requirements_status', 'Website requirements'),
    ('content', 'content_status', 'Content & policies'),
    ('documents', 'documents_status', 'Documents'),
    ('payment_kyc', 'payment_kyc_status', 'Payment gateway & owner KYC'),
)


def get_public_onboarding_step_meta(submission: OnboardingSubmission) -> tuple[list[dict], str | None]:
    """
    Sequential client onboarding: each step is unlocked only after the previous step
    has been saved at least once (status != pending).

    Returns (steps, first_open_slug) where first_open_slug is the first unlocked step
    that is not yet saved (for default-open accordion); None if all saved.
    """
    prev_status_attr: str | None = None
    steps: list[dict] = []
    first_open: str | None = None
    for slug, status_attr, title in PUBLIC_ONBOARDING_FLOW:
        st = getattr(submission, status_attr)
        saved = st != OnboardingSubmission.SectionStatus.PENDING
        unlocked = prev_status_attr is None or (
            getattr(submission, prev_status_attr)
            != OnboardingSubmission.SectionStatus.PENDING
        )
        if unlocked and not saved and first_open is None:
            first_open = slug
        steps.append(
            {
                'slug': slug,
                'title': title,
                'status_attr': status_attr,
                'saved': saved,
                'unlocked': unlocked,
                'status': st,
            }
        )
        prev_status_attr = status_attr
    return steps, first_open


def assert_section_save_allowed(submission: OnboardingSubmission, section: str) -> None:
    """Raises ValueError if the client tries to save a section before the prior one was saved."""
    slugs = [s[0] for s in PUBLIC_ONBOARDING_FLOW]
    if section not in slugs:
        raise ValueError('Invalid section.')
    idx = slugs.index(section)
    if idx == 0:
        return
    prev_slug = slugs[idx - 1]
    prev_attr = SECTION_STATUS_FIELD[prev_slug]
    if getattr(submission, prev_attr) == OnboardingSubmission.SectionStatus.PENDING:
        raise ValueError('Previous section must be saved first.')


@transaction.atomic
def get_or_create_onboarding(project) -> OnboardingSubmission:
    """
    Returns existing OnboardingSubmission for the project,
    or creates a blank one. Never raises on a valid project.
    """
    obj, _ = OnboardingSubmission.objects.get_or_create(project=project)
    return obj


def _maybe_set_partial_status(submission, section_name: str) -> None:
    attr = SECTION_STATUS_FIELD.get(section_name)
    if not attr:
        return
    cur = getattr(submission, attr)
    if cur not in (
        OnboardingSubmission.SectionStatus.VERIFIED,
        OnboardingSubmission.SectionStatus.SUBMITTED,
    ):
        setattr(submission, attr, OnboardingSubmission.SectionStatus.PARTIAL)


@transaction.atomic
def save_onboarding_section(
    submission,
    section_name: str,
    data: dict,
    *,
    updated_by,
) -> OnboardingSubmission:
    """
    Updates fields belonging to the given section_name on submission.
    Valid section_name values: 'business_info', 'contact', 'branding',
    'requirements', 'content', 'documents', 'payment_kyc'.

    Sets the corresponding `<section>_status` to SectionStatus.PARTIAL
    if not already VERIFIED or SUBMITTED.

    Logs an ActivityLog entry on submission.project.client.lead if it exists.
    """
    fields = SECTION_MODEL_FIELDS.get(section_name)
    if not fields:
        raise ValueError('Invalid section_name')
    submission = OnboardingSubmission.objects.select_for_update().get(pk=submission.pk)
    for name in fields:
        if name not in data:
            continue
        setattr(submission, name, data[name])
    _maybe_set_partial_status(submission, section_name)
    submission.save()
    lead = submission.project.client.lead
    if lead is not None:
        log_activity(lead, 'onboarding_section_saved', section_name)
    return submission


@transaction.atomic
def submit_onboarding(submission, *, ip_address: str) -> OnboardingSubmission:
    """
    Called when the client submits the final agreement.
    Sets: terms_accepted=True, terms_accepted_at=now(),
    terms_accepted_ip=ip_address, submitted_at=now(),
    agreement_status=SUBMITTED.

    Updates Project.status to Status.ONBOARDING_SUBMITTED.

    Logs ActivityLog on the lead: action='onboarding_submitted'.

    Raises ValueError if terms_accepted is already True
    (idempotency guard — do not allow double submission).
    """
    submission = OnboardingSubmission.objects.select_for_update().get(pk=submission.pk)
    if submission.terms_accepted:
        raise ValueError('Onboarding already submitted.')
    now = timezone.now()
    submission.terms_accepted = True
    submission.terms_accepted_at = now
    submission.terms_accepted_ip = ip_address or None
    submission.submitted_at = now
    submission.agreement_status = OnboardingSubmission.SectionStatus.SUBMITTED
    submission.save()
    project = Project.objects.select_for_update().get(pk=submission.project_id)
    project.status = Project.Status.ONBOARDING_SUBMITTED
    project.save()
    lead = submission.project.client.lead
    if lead is not None:
        log_activity(lead, 'onboarding_submitted', '')
    audit_service.log_event(
        category=AuditEntry.EventCategory.ONBOARDING,
        action='submitted',
        object_type='OnboardingSubmission',
        object_id=submission.pk,
        object_repr=str(submission)[:200],
        actor=None,
        project=project,
        after_state={'submitted_at': submission.submitted_at.isoformat() if submission.submitted_at else None},
        ip_address=ip_address,
    )
    return submission


@transaction.atomic
def verify_section(
    submission,
    section_name: str,
    new_status: str,
    *,
    updated_by,
) -> OnboardingSubmission:
    """
    Internal staff sets a section status to VERIFIED or REJECTED.
    Logs ActivityLog: action='onboarding_section_verified',
    note: f'{section_name}:{new_status}'.
    Raises ValueError if new_status is not VERIFIED or REJECTED.
    """
    if new_status not in (
        OnboardingSubmission.SectionStatus.VERIFIED,
        OnboardingSubmission.SectionStatus.REJECTED,
    ):
        raise ValueError('new_status must be VERIFIED or REJECTED.')
    attr = SECTION_STATUS_FIELD.get(section_name)
    if not attr:
        raise ValueError('Invalid section_name for verification.')
    submission = OnboardingSubmission.objects.select_for_update().get(pk=submission.pk)
    setattr(submission, attr, new_status)
    submission.save()
    lead = submission.project.client.lead
    if lead is not None:
        log_activity(
            lead,
            'onboarding_section_verified',
            f'{section_name}:{new_status}',
        )
    audit_service.log_event(
        category=AuditEntry.EventCategory.ONBOARDING,
        action='section_verified',
        object_type='OnboardingSubmission',
        object_id=submission.pk,
        object_repr=str(submission)[:200],
        actor=updated_by,
        project=submission.project,
        after_state={section_name: new_status},
    )
    return submission


def get_onboarding_summary(submission) -> dict:
    """
    Returns a dict summarising section completion for display
    in the internal project detail view.
    """
    sections = []
    for name, label, status_attr in SUMMARY_SECTIONS:
        entry = {
            'name': name,
            'label': label,
            'completion': submission.section_completion_percent(name),
        }
        if status_attr:
            entry['status'] = getattr(submission, status_attr)
        else:
            pct = submission.section_completion_percent(name)
            if pct >= 100:
                entry['status'] = OnboardingSubmission.SectionStatus.SUBMITTED
            elif pct > 0:
                entry['status'] = OnboardingSubmission.SectionStatus.PARTIAL
            else:
                entry['status'] = OnboardingSubmission.SectionStatus.PENDING
        labels = dict(OnboardingSubmission.SectionStatus.choices)
        entry['status_label'] = labels.get(entry['status'], entry['status'])
        sections.append(entry)
    return {
        'sections': sections,
        'overall': submission.overall_completion_percent(),
        'is_submitted': submission.is_fully_submitted(),
    }
