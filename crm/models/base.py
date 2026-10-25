"""Shared model building blocks (Part 14.3)."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

USER = settings.AUTH_USER_MODEL


def MoneyField(verbose_name=None, **kwargs):  # noqa: N802 - field factory
    """DECIMAL(15,2) in INR. Floats are forbidden (Part 1.5)."""
    kwargs.setdefault('max_digits', 15)
    kwargs.setdefault('decimal_places', 2)
    kwargs.setdefault('default', Decimal('0.00'))
    return models.DecimalField(verbose_name, **kwargs)


def RateField(verbose_name=None, **kwargs):  # noqa: N802
    """DECIMAL(5,2) for tax and percentage rates."""
    kwargs.setdefault('max_digits', 5)
    kwargs.setdefault('decimal_places', 2)
    kwargs.setdefault('default', Decimal('0.00'))
    return models.DecimalField(verbose_name, **kwargs)


class TimeStamped(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='+', editable=False)
    updated_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='+', editable=False)

    class Meta:
        abstract = True


class Versioned(models.Model):
    """Optimistic locking: a stale write is rejected (Part 2.11 / 14.3)."""
    row_version = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        abstract = True


class Archivable(models.Model):
    """Soft delete: business records are archived, never physically deleted (Part 1.5)."""
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True, editable=False)
    archived_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name='+', editable=False)
    archive_reason = models.CharField(max_length=500, blank=True, editable=False)

    class Meta:
        abstract = True

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class ActiveQuerySet(models.QuerySet):
    def active(self):
        return self.filter(archived_at__isnull=True)

    def archived(self):
        return self.filter(archived_at__isnull=False)
