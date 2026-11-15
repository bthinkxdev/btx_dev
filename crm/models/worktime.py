"""Working time: when each person logged in, took breaks and logged out.

One ``WorkPeriod`` runs from login to logout and never crosses midnight (a day that is still open at midnight is closed
and a new one starts with the first activity of the next day). Breaks sit inside a period. Working time is the length of
the period minus its breaks. Administrators (owner and super admins) are not tracked."""
from __future__ import annotations

from datetime import timedelta

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import USER

START_SOURCES = [('login', 'Logged in'), ('resume', 'Back on another device'), ('day_start', 'New day')]
END_REASONS = [('', 'Still working'), ('logout', 'Logged out'), ('idle', 'Signed out after 12 hours idle'),
               ('day_end', 'Day ended'), ('admin', 'Closed by a manager')]


class WorkPeriod(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='work_periods')
    day = models.DateField(db_index=True, help_text='The calendar day (IST) this period belongs to.')
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField()
    start_source = models.CharField(max_length=10, choices=START_SOURCES, default='login')
    end_reason = models.CharField(max_length=10, choices=END_REASONS, blank=True)

    class Meta:
        ordering = ['started_at']
        indexes = [models.Index(fields=['user', 'day'])]
        constraints = [
            models.UniqueConstraint(fields=['user'], condition=Q(ended_at__isnull=True), name='crm_one_open_work_period'),
            models.CheckConstraint(check=Q(ended_at__isnull=True) | Q(ended_at__gte=models.F('started_at')), name='crm_work_period_order'),
        ]

    def __str__(self):
        return f'{self.user} {self.started_at:%d %b %H:%M}'

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def end_or_now(self, now=None):
        return self.ended_at or now or timezone.now()

    def break_seconds(self, now=None) -> int:
        now = now or timezone.now()
        return int(sum(((b.ended_at or now) - b.started_at).total_seconds() for b in self.breaks.all()))

    def span_seconds(self, now=None) -> int:
        return max(0, int((self.end_or_now(now) - self.started_at).total_seconds()))

    def worked_seconds(self, now=None) -> int:
        return max(0, self.span_seconds(now) - self.break_seconds(now))


class BreakPeriod(models.Model):
    ENDS = [('', 'On break'), ('back', 'Back to work'), ('logout', 'Logged out'), ('idle', 'Signed out after 12 hours idle'),
            ('day_end', 'Day ended'), ('admin', 'Closed by a manager')]
    period = models.ForeignKey(WorkPeriod, on_delete=models.CASCADE, related_name='breaks')
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=10, choices=ENDS, blank=True)

    class Meta:
        ordering = ['started_at']
        constraints = [
            models.UniqueConstraint(fields=['period'], condition=Q(ended_at__isnull=True), name='crm_one_open_break'),
            models.CheckConstraint(check=Q(ended_at__isnull=True) | Q(ended_at__gte=models.F('started_at')), name='crm_break_order'),
        ]

    @property
    def seconds(self) -> int:
        return max(0, int(((self.ended_at or timezone.now()) - self.started_at).total_seconds()))


def fmt_hm(seconds: int) -> str:
    """4h 05m, or 12m when under an hour."""
    seconds = max(0, int(seconds))
    h, m = divmod(seconds // 60, 60)
    return f'{h}h {m:02d}m' if h else f'{m}m'


IDLE_LIMIT = timedelta(hours=12)
