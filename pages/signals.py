"""Signals for blog → newsletter notifications."""
from django.db import transaction
from django.dispatch import receiver
from django.db.models.signals import post_save, pre_save

from .models import BlogPost
from .newsletter_tasks import schedule_blog_subscriber_notifications


@receiver(pre_save, sender=BlogPost)
def blog_post_track_previous_published(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = BlogPost.objects.only('is_published').get(pk=instance.pk)
            instance._blog_prev_published = old.is_published
        except BlogPost.DoesNotExist:
            instance._blog_prev_published = None
    else:
        instance._blog_prev_published = None


@receiver(post_save, sender=BlogPost)
def blog_post_notify_subscribers_on_publish(sender, instance, created, **kwargs):
    if not instance.is_published:
        BlogPost.objects.filter(pk=instance.pk).update(
            notification_job_started_at=None,
            subscriber_notification_completed_at=None,
        )
        return

    prev = getattr(instance, '_blog_prev_published', None)
    newly_published = (created and instance.is_published) or (prev is False)

    if not newly_published:
        return
    if instance.subscriber_notification_completed_at:
        return
    if instance.notification_job_started_at:
        return

    post_id = instance.pk

    def _commit():
        schedule_blog_subscriber_notifications(post_id)

    transaction.on_commit(_commit)
