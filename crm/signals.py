from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import EmployeeProfile, OnboardingSubmission, Package, PackageScope, Project
from .services.onboarding import get_or_create_onboarding
from .services.provisioning import create_default_provisioning


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_crm_profile(sender, instance, created, **kwargs):
    if created:
        EmployeeProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=Project)
def create_onboarding_for_project(sender, instance, created, **kwargs):
    if created:
        get_or_create_onboarding(instance)


@receiver(post_save, sender=Package)
def ensure_package_scope(sender, instance, **kwargs):
    PackageScope.objects.get_or_create(package=instance)


@receiver(post_save, sender=OnboardingSubmission)
def init_provisioning_after_onboarding_submit(sender, instance, **kwargs):
    """Provisioning starts only after the client fully submits onboarding (not on project create)."""
    if instance.is_fully_submitted():
        create_default_provisioning(instance.project)
