"""Celery tasks (used when REDIS_URL / CELERY_BROKER_URL is configured). Each wraps an idempotent job."""
from celery import shared_task


@shared_task(name='crm.tasks.deliver_email', autoretry_for=(Exception,), max_retries=3, retry_backoff=True)
def deliver_email(log_id):
    from crm.core.notify import deliver
    deliver(log_id)


@shared_task(name='crm.tasks.run_frequent_jobs')
def run_frequent_jobs():
    from crm.core.jobs import run_group
    return run_group('frequent')


@shared_task(name='crm.tasks.run_daily_jobs')
def run_daily_jobs():
    from crm.core.jobs import run_group
    return run_group('daily')
