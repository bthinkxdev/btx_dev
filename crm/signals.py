from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import EmployeeProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_crm_profile(sender, instance, created, **kwargs):
    if created:
        EmployeeProfile.objects.get_or_create(user=instance)
