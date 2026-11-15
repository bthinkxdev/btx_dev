"""Scheduled work for the AI business staff. Every job is safe to run twice.

The jobs run on the hourly slot and decide for themselves whether it is time: the snapshot keeps today's row current,
the morning briefing goes out from 8 am, the evening one from 7 pm, each once a day."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from crm.core import india, notify
from crm.models import DailySnapshot, DeskBriefing

from . import assistant, metrics, rules
from .views import home_url

logger = logging.getLogger('crm.desk')

MORNING_HOUR = 8
EVENING_HOUR = 19


def take_snapshot(d=None) -> DailySnapshot:
    """Store the day's numbers, team board, goals and position. Run for today it simply refreshes the row."""
    d = d or india.today()
    ctx = metrics.full_context(d, fresh=True)
    data = {'numbers': ctx['numbers'], 'team': ctx['team'], 'goals': ctx['goals'], 'stock': ctx['stock'],
            'delivery': {k: v for k, v in ctx['delivery'].items() if k not in ('overdue_items',)},
            'revenue': ctx['revenue'], 'flags': ctx['flags'], 'weak_areas': ctx['weak_areas']}
    snap, _ = DailySnapshot.objects.update_or_create(date=d, defaults={'data': data})
    return snap


def snapshot() -> int:
    """Hourly: keep today's snapshot current, and make sure yesterday's was finalised."""
    today = india.today()
    n = 0
    yesterday = today - timedelta(days=1)
    if not DailySnapshot.objects.filter(date=yesterday).exists():
        take_snapshot(yesterday)
        n += 1
    take_snapshot(today)
    return n + 1


def _plain(text: str, limit: int = 480) -> str:
    text = re.sub(r'\*\*|__|_(?=\w)|(?<=\w)_', '', text)
    text = re.sub(r'\s*\n+\s*', ' ', text).strip()
    return text[:limit]


def _send(kind: str) -> int:
    today = india.today()
    brief = assistant.generate_briefing(kind, today)
    titles = {'morning': 'Your morning briefing is ready', 'evening': 'Your evening business report is ready'}
    n = notify.notify(notify.owners(), titles[kind], body=_plain(brief.text), url=home_url(),
                      event_type='desk_briefing', level='info', email=(kind == 'evening'),
                      dedupe_key=f'desk-{kind}:{today:%Y%m%d}')
    return n or 1


def morning() -> int:
    """From 8 am: write the morning briefing once, suggest today's actions and tell the owner."""
    now = timezone.localtime()
    if now.hour < MORNING_HOUR:
        return 0
    if DeskBriefing.objects.filter(kind='morning', for_date=india.today()).exists():
        return 0
    return _send('morning')


def evening() -> int:
    """From 7 pm: write the evening report once and email it to the owner."""
    now = timezone.localtime()
    if now.hour < EVENING_HOUR:
        return 0
    if DeskBriefing.objects.filter(kind='evening', for_date=india.today()).exists():
        return 0
    return _send('evening')


def suggestions() -> int:
    """Hourly: add any new suggested actions the rules find (each is created once per day)."""
    ctx = metrics.full_context(fresh=True)
    return rules.suggest_actions(ctx)
