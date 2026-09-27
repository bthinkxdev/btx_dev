from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import EmployeeProfile, Lead, Package, PackageScope


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
