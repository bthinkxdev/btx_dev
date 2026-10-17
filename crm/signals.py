from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import EmployeeProfile, Lead, NotificationEvent, Package, PackageScope


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_crm_profile(sender, instance, created, **kwargs):
    if created:
        EmployeeProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=Package)
def ensure_package_scope(sender, instance, **kwargs):
    PackageScope.objects.get_or_create(package=instance)


@receiver(post_save, sender=Lead)
def ensure_lead_has_next_activity(sender, instance, created, **kwargs):
    """Fresh-lead protection: a brand-new lead must never sit with no next action."""
    if not created:
        return
    from .services.pipeline import start_lead_activity

    start_lead_activity(instance)


@receiver(pre_save, sender=Lead)
def _capture_lead_old_employee(sender, instance, **kwargs):
    """Stash the previous owner so post_save can tell a reassignment from the
    initial self-assignment-at-creation (which needs no push)."""
    if instance.pk:
        instance._old_employee_id = (
            Lead.objects.filter(pk=instance.pk).values_list('employee_id', flat=True).first()
        )
    else:
        instance._old_employee_id = None


@receiver(post_save, sender=Lead)
def notify_lead_assigned(sender, instance, created, **kwargs):
    """'Lead assigned to you' push — fires on any future reassignment flow
    automatically, since it watches the field itself rather than one view."""
    if created:
        return
    old_employee_id = getattr(instance, '_old_employee_id', None)
    if instance.employee_id and instance.employee_id != old_employee_id:
        from django.urls import reverse

        from .push import send_push_to_user

        send_push_to_user(
            instance.employee,
            kind=NotificationEvent.Kind.LEAD_ASSIGNED,
            title='Lead assigned to you',
            body=f'{instance.name} has been assigned to you.',
            url=reverse('crm:lead_detail', args=[instance.pk]),
            lead=instance,
        )


def _notify_new_website_lead(sender, instance, created, **kwargs):
    """Shared receiver for both website-lead sources (contact form + quote
    request) — connected explicitly below rather than via @receiver so this
    module doesn't import pages.models at class-definition time for every
    other receiver in the file too."""
    if not created:
        return
    from django.urls import reverse

    from .push import send_push_to_admins

    send_push_to_admins(
        kind=NotificationEvent.Kind.NEW_WEBSITE_LEAD,
        title='New website lead',
        body=f'{instance.name} just submitted the website form.',
        url=reverse('crm:website_leads'),
    )


def _connect_website_lead_signals():
    from pages.models import ContactSubmission, QuoteRequest

    post_save.connect(
        _notify_new_website_lead, sender=ContactSubmission,
        dispatch_uid='crm_notify_new_contact_submission',
    )
    post_save.connect(
        _notify_new_website_lead, sender=QuoteRequest,
        dispatch_uid='crm_notify_new_quote_request',
    )


_connect_website_lead_signals()
