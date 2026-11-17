"""Scheduled work for client photos. Both jobs are safe to run twice: submitting a batch is guarded by its own state, and every
reminder carries a dedupe key for the day it is about."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from crm.core import india, notify
from crm.models import ImageAsset, ImageSubmission
from crm.models.images import ACTIVE, WORKING

from . import services

logger = logging.getLogger('crm.images')

DRAFT_GRACE_MINUTES = 30
UNASSIGNED_HOURS = 4
CLIENT_WAIT_DAYS = 3


def stale_drafts() -> int:
    """A client who uploaded photos and then lost signal or closed the app never pressed Send. After half an hour the photos are
    sent for them, so nothing they uploaded is ever lost. Empty drafts older than two days are cleared."""
    cutoff = timezone.now() - timedelta(minutes=DRAFT_GRACE_MINUTES)
    sent = 0
    for sub in ImageSubmission.objects.filter(submitted_at__isnull=True, updated_at__lt=cutoff, assets__isnull=False).distinct():
        try:
            services.submit(sub, sub.submitted_by, '', auto=True)
            sent += 1
        except Exception:
            logger.exception('Could not send draft %s', sub.pk)
    ImageSubmission.objects.filter(submitted_at__isnull=True, assets__isnull=True, created_at__lt=timezone.now() - timedelta(days=2)).delete()
    return sent


def reminders() -> int:
    """Deadlines, late work, photos nobody has picked up, and photos waiting too long for the client."""
    today = india.today()
    now = timezone.now()
    made = 0
    qs = ImageAsset.objects.filter(archived_at__isnull=True, submission__submitted_at__isnull=False).select_related('client', 'client__owner', 'editor', 'coordinator')

    for a in qs.filter(status__in=WORKING + ['internal_review'], deadline__isnull=False, deadline__lte=today + timedelta(days=1), deadline__gte=today):
        when = 'today' if a.deadline == today else 'tomorrow'
        people = [a.editor] if a.editor_id else []
        made += notify.notify(people, f'{a.number} is due {when}', body=f'{a.client.display_name}. Due {a.deadline:%d %b}.', url=services.url_for(a),
                              event_type='image_deadline_soon', dedupe_key=f'imgdue:{a.pk}:{a.deadline:%Y%m%d}:{when}')

    late = list(qs.filter(status__in=ACTIVE, deadline__lt=today))
    for a in late:
        people = ([a.editor] if a.editor_id and a.status in WORKING + ['internal_review'] else []) + services.coordinators_for(a)
        made += notify.notify(people, f'{a.number} is late', body=f'{a.client.display_name}. Was due {a.deadline:%d %b}.', url=services.url_for(a),
                              event_type='image_overdue', level='warning', dedupe_key=f'imglate:{a.pk}:{today:%Y%m%d}')
    if late:
        made += notify.notify(list(services.users_with_role(services.HR_ROLE)) + list(notify.owners()), f'{len(late)} client photo{"s are" if len(late) != 1 else " is"} late',
                              body='Open the photo list to see who holds them.', url='/crm/images/list/?quick=overdue', event_type='image_overdue_digest',
                              dedupe_key=f'imglatedigest:{today:%Y%m%d}')

    cutoff = now - timedelta(hours=UNASSIGNED_HOURS)
    for a in qs.filter(status__in=('new', 'ready'), editor__isnull=True, created_at__lt=cutoff):
        made += notify.notify(services.coordinators_for(a), f'{a.number} has no editor yet', body=f'{a.client.display_name} sent it more than {UNASSIGNED_HOURS} hours ago.',
                              url=services.url_for(a), event_type='image_unassigned', level='warning', dedupe_key=f'imgunassigned:{a.pk}:{today:%Y%m%d}')

    for a in qs.filter(status='client_review', client_wait_since__lt=now - timedelta(days=CLIENT_WAIT_DAYS)):
        days = (now - a.client_wait_since).days
        made += notify.notify(services.coordinators_for(a), f'{a.client.display_name} has not answered on {a.number}',
                              body=f'Waiting {days} days. A quick call may help.', url=services.url_for(a), event_type='image_client_wait',
                              dedupe_key=f'imgwait:{a.pk}:{today.isocalendar()[1]}')
        made += notify.notify(list(services.client_users(a.client)), f'Your photo {a.number} is waiting for you',
                              body='Please approve it or ask for a change.', url=services.url_for(a), event_type='image_client_nudge',
                              dedupe_key=f'imgnudge:{a.pk}:{today.isocalendar()[1]}')
    return made
