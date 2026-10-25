"""Signal handlers: idempotent seed data after migrate; profile for every user."""
from django.conf import settings
from django.db.models.signals import post_migrate, post_save
from django.dispatch import receiver


@receiver(post_migrate)
def seed_after_migrate(sender, **kwargs):
    if getattr(sender, 'name', '') != 'crm':
        return
    from crm.core import bootstrap
    bootstrap.run()


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_profile(sender, instance, created, **kwargs):
    if created:
        from crm.models import UserProfile
        UserProfile.objects.get_or_create(user=instance)
