"""
Background newsletter delivery for new blog posts.

Sends one email per subscriber with a configurable delay between messages to
respect SMTP rate limits. Runs in a daemon thread after the HTTP request commits.

For higher volume or crash recovery, prefer a task queue (Celery, RQ, Dramatiq)
instead of threads — same sending logic can be moved to a shared task function.
"""
from __future__ import annotations

import logging
import threading
import time

from django.conf import settings
from django.core.mail import send_mail
from django.db import close_old_connections, transaction
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)


def schedule_blog_subscriber_notifications(post_id: int) -> None:
    """Claim the send job and start a daemon thread (call from transaction.on_commit)."""
    from .models import BlogPost

    with transaction.atomic():
        updated = BlogPost.objects.filter(
            pk=post_id,
            is_published=True,
            notification_job_started_at__isnull=True,
            subscriber_notification_completed_at__isnull=True,
        ).update(notification_job_started_at=timezone.now())

    if not updated:
        return

    thread = threading.Thread(
        target=_blog_notification_worker,
        args=(post_id,),
        name=f'blog-newsletter-{post_id}',
        daemon=True,
    )
    thread.start()
    logger.info('Started blog newsletter thread for post_id=%s', post_id)


def _blog_notification_worker(post_id: int) -> None:
    close_old_connections()
    try:
        _run_blog_notification_batch(post_id)
    except Exception:
        logger.exception('Blog newsletter worker failed for post_id=%s', post_id)
    finally:
        try:
            from .models import BlogPost

            BlogPost.objects.filter(pk=post_id).update(
                subscriber_notification_completed_at=timezone.now()
            )
        except Exception:
            logger.exception('Could not mark blog post %s notification completed', post_id)
        close_old_connections()


def _run_blog_notification_batch(post_id: int) -> None:
    from .models import BlogPost, NewsletterSubscriber

    try:
        post = BlogPost.objects.get(pk=post_id)
    except BlogPost.DoesNotExist:
        logger.warning('Blog post %s gone; skipping newsletter', post_id)
        return

    if not post.is_published:
        logger.info('Post %s unpublished; skipping newsletter sends', post_id)
        return

    base = getattr(settings, 'SITE_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')
    article_path = reverse('pages:blog_post', kwargs={'slug': post.slug})
    article_url = f'{base}{article_path}'

    interval = float(getattr(settings, 'NEWSLETTER_EMAIL_INTERVAL_SECONDS', 2.0))
    interval = max(0.5, min(interval, 60.0))

    from_email = settings.DEFAULT_FROM_EMAIL
    subject = f'New on BThinkX Dev: {post.title}'

    qs = NewsletterSubscriber.objects.filter(is_active=True).order_by('id')
    subscribers = list(qs)
    if not subscribers:
        logger.info('No active subscribers for blog post %s', post_id)
        return

    logger.info(
        'Sending blog newsletter for post_id=%s to %d subscribers (interval=%ss)',
        post_id,
        len(subscribers),
        interval,
    )

    for i, sub in enumerate(subscribers):
        if not BlogPost.objects.filter(pk=post_id, is_published=True).exists():
            logger.info('Post %s unpublished mid-send; stopping', post_id)
            break

        unsub_path = reverse(
            'pages:newsletter_unsubscribe', kwargs={'token': sub.unsubscribe_token}
        )
        unsub_url = f'{base}{unsub_path}'

        body = (
            f'Hi,\n\n'
            f'We published a new article:\n\n'
            f'{post.title}\n\n'
            f'{post.excerpt[:500]}{"…" if len(post.excerpt) > 500 else ""}\n\n'
            f'Read it here:\n{article_url}\n\n'
            f'— BThinkX Dev\n\n'
            f'Unsubscribe: {unsub_url}\n'
        )

        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=from_email,
                recipient_list=[sub.email],
                fail_silently=False,
            )
        except Exception as e:
            logger.exception(
                'Newsletter send failed for %s (post %s): %s',
                sub.email,
                post_id,
                e,
            )

        if i < len(subscribers) - 1:
            time.sleep(interval)
