from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class EmployeeProfile(models.Model):
    """One profile per User — target for revenue dashboard."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crm_profile',
    )
    target_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0'),
        help_text='Monthly or period revenue target',
    )

    class Meta:
        verbose_name = 'Employee profile'
        verbose_name_plural = 'Employee profiles'

    def __str__(self):
        return f'{self.user.get_username()} — target {self.target_amount}'


class Package(models.Model):
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crm_packages',
    )
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Lead(models.Model):
    class Status(models.TextChoices):
        # Early stages
        NEW = 'new', 'New'
        CONTACTED = 'contacted', 'Contacted'
        FOLLOW_UP = 'follow_up', 'Follow up'

        # Interest stages
        INTERESTED = 'interested', 'Interested'
        QUALIFIED = 'qualified', 'Qualified'

        # Deal stages
        PROPOSAL = 'proposal', 'Proposal sent'
        NEGOTIATION = 'negotiation', 'Negotiation'

        # Closing stages
        WON = 'won', 'Won'

        # 🔥 Payment stages (NEW)
        ADVANCE_PAID = 'advance_paid', 'Advance Paid'
        PARTIALLY_PAID = 'partially_paid', 'Partially Paid'
        FULLY_PAID = 'fully_paid', 'Fully Paid'

        # Other states
        ON_HOLD = 'on_hold', 'On hold'
        LOST = 'lost', 'Lost'

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crm_leads',
    )
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    source = models.CharField(max_length=120, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )
    package = models.ForeignKey(
        Package,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='leads',
    )
    deal_value = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0'),
    )
    notes = models.TextField(blank=True)
    next_followup = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.name


class FollowUp(models.Model):
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name='followups',
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crm_followups',
    )
    datetime = models.DateTimeField(db_index=True)
    note = models.TextField(blank=True)
    is_done = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['datetime']

    def __str__(self):
        return f'{self.lead.name} @ {self.datetime}'


class Task(models.Model):
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name='tasks',
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crm_tasks',
    )
    title = models.CharField(max_length=300)
    due_date = models.DateField(null=True, blank=True)
    is_completed = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['due_date', 'id']

    def __str__(self):
        return self.title


class ActivityLog(models.Model):
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name='activities',
    )
    action = models.CharField(max_length=80)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.action} — {self.lead.name}'
