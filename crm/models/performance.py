"""Facts about a person that cannot be worked out afterwards, so they are recorded every night.

"How many follow-ups were overdue at the end of that day" or "what was the open pipeline then" can only be known on the
day. Everything else on the performance profile is calculated from the records themselves."""
from __future__ import annotations

from django.db import models

from .base import USER, MoneyField


class StaffDayStat(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='crm_day_stats')
    day = models.DateField(db_index=True)
    open_leads = models.PositiveIntegerField(default=0)
    open_pipeline = MoneyField()
    stale_leads = models.PositiveIntegerField(default=0, help_text='Open leads with no activity for 3 days.')
    overdue_followups = models.PositiveIntegerField(default=0, help_text='Pending and past due at the end of the day.')
    auto_scheduled = models.PositiveIntegerField(default=0, help_text='Follow-ups the system had to set because none existed.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-day']
        constraints = [models.UniqueConstraint(fields=['user', 'day'], name='crm_staffdaystat_unique')]


class DailyReport(models.Model):
    """A person's end-of-day report, frozen the moment it is made. It has a code so HR or an administrator can check that a
    report shared in a chat is genuine, and a new row is made if the day changes after it, so nothing is ever edited."""
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='crm_daily_reports')
    day = models.DateField(db_index=True)
    seq = models.PositiveSmallIntegerField(default=1)
    code = models.CharField(max_length=24, unique=True)
    text = models.TextField()
    data = models.JSONField(default=dict)
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    shown_at = models.DateTimeField(null=True, blank=True)
    copied_at = models.DateTimeField(null=True, blank=True)
    copy_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['-day', '-seq']
        constraints = [models.UniqueConstraint(fields=['user', 'day', 'seq'], name='crm_dailyreport_unique')]

    def __str__(self):
        return f'{self.code} {self.user} {self.day}'
