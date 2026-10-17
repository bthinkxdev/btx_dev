"""
Celery entry point for the follow-up reminder job, for deployments that run
Celery beat + a worker + Redis. Wraps the exact same `run()` the
`send_followup_reminders` management command calls, so there is one
implementation whichever way it's scheduled — a plain cron calling the
management command works just as well if Celery/Redis isn't set up yet.
"""
from celery import shared_task


@shared_task
def send_followup_reminders():
    from crm.management.commands.send_followup_reminders import run

    overdue, r30, r5 = run()
    return {'overdue': overdue, 'reminder_30': r30, 'reminder_5': r5}
