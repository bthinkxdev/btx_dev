from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import EmployeeProfile, Package, PackageScope


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_crm_profile(sender, instance, created, **kwargs):
    if created:
        EmployeeProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=Package)
def ensure_package_scope(sender, instance, **kwargs):
    PackageScope.objects.get_or_create(package=instance)
