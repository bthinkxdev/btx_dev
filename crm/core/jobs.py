"""
Job runner (Part 13.4). Every run is recorded as a JobRun with an idempotency key
(job name + time slot), so running the scheduler twice in the same slot is a no-op.
Run from cron (`python manage.py crm_run_jobs`) or Celery beat (crm.tasks).
"""
from __future__ import annotations

import logging
import traceback
from importlib import import_module

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger('crm.jobs')

# name -> (dotted path, group, slot minutes)
JOBS = {
    'leads.unassigned_alerts': ('crm.modules.leads.jobs.unassigned_alerts', 'frequent', 5),
    'leads.first_response_sla': ('crm.modules.leads.jobs.first_response_sla', 'frequent', 5),
    'leads.follow_ups': ('crm.modules.leads.jobs.follow_up_reminders_and_escalation', 'frequent', 5),
    'leads.retry_inbox': ('crm.modules.leads.jobs.retry_inbox', 'frequent', 5),
    'leads.stale_and_aging': ('crm.modules.leads.jobs.stale_and_aging', 'frequent', 60),
    'leads.silent_sources': ('crm.modules.leads.jobs.silent_sources', 'frequent', 60),
    'leads.win_back': ('crm.modules.leads.jobs.win_back', 'daily', 1440),
    'approvals.remind': ('crm.core.approvals.remind_and_escalate', 'frequent', 15),
    'approvals.expire': ('crm.core.approvals.expire_overdue', 'frequent', 15),
    'posting.retry': ('crm.core.posting.retry_failed', 'frequent', 5),
    'email.retry': ('crm.core.notify.retry_failed_emails', 'frequent', 15),
    'quotations.expire': ('crm.modules.quotations.jobs.expire_and_remind', 'frequent', 60),
    'payments.expire_links': ('crm.modules.payments.jobs.expire_links', 'frequent', 15),
    'payments.abandoned': ('crm.modules.payments.jobs.abandoned_and_unpaid', 'frequent', 60),
    'payments.backstop': ('crm.modules.payments.jobs.gateway_backstop', 'frequent', 15),
    'payments.reprocess_events': ('crm.modules.payments.jobs.reprocess_failed_events', 'frequent', 15),
    'billing.dunning': ('crm.modules.billing.jobs.dunning', 'daily', 1440),
    'billing.recurring': ('crm.modules.contracts.jobs.generate_recurring_invoices', 'daily', 1440),
    'billing.tds_reminders': ('crm.modules.billing.jobs.tds_certificate_reminders', 'daily', 1440),
    'contracts.renewals': ('crm.modules.contracts.jobs.renewal_alerts', 'daily', 1440),
    'projects.blockers': ('crm.modules.projects.jobs.escalate_blockers', 'frequent', 60),
    'tickets.sla': ('crm.modules.projects.jobs.ticket_sla', 'frequent', 15),
    'expenses.recurring': ('crm.modules.expenses.jobs.generate_recurring', 'daily', 1440),
    'audit.checkpoint': ('crm.core.audit.write_checkpoint', 'daily', 1440),
    'integrity.nightly': ('crm.core.integrity.run_checks', 'daily', 1440),
    'housekeeping.idempotency': ('crm.core.jobs.purge_idempotency_keys', 'daily', 1440),
}


def _slot_key(name: str, minutes: int) -> str:
    now = timezone.now()
    slot = int(now.timestamp() // (minutes * 60))
    return f'{name}:{slot}'


def run_job(name: str, force: bool = False) -> dict:
    from crm.models import JobRun
    path, _, minutes = JOBS[name]
    key = _slot_key(name, minutes) + (f':manual:{timezone.now().timestamp()}' if force else '')
    try:
        with transaction.atomic():
            run = JobRun.objects.create(job=name, idempotency_key=key[:160], status='running',
                                        started_at=timezone.now(), attempts=1)
    except IntegrityError:
        return {'job': name, 'status': 'skipped', 'reason': 'already ran in this slot'}
    module_path, func_name = path.rsplit('.', 1)
    try:
        func = getattr(import_module(module_path), func_name)
        result = func()
        run.status = 'succeeded'
        run.result = {'value': result if isinstance(result, (int, str, float, type(None))) else str(result)}
    except Exception as exc:
        run.status = 'failed'
        run.error = ''.join(traceback.format_exception(exc))[-4000:]
        logger.exception('Job %s failed', name)
    run.finished_at = timezone.now()
    run.save()
    return {'job': name, 'status': run.status}


def run_group(group: str) -> list[dict]:
    return [run_job(name) for name, (_, g, _) in JOBS.items() if g == group or group == 'all']


def purge_idempotency_keys() -> int:
    from datetime import timedelta
    from crm.models import IdempotencyKey
    deleted, _ = IdempotencyKey.objects.filter(created_at__lt=timezone.now() - timedelta(days=7)).delete()
    return deleted
