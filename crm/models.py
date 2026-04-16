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
    photo = models.ImageField(
        upload_to='crm_profiles/',
        blank=True,
        null=True,
        help_text='Shown in the CRM header next to your name',
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
        WHATSAPP_CONNECTED = 'whatsapp_connected', 'WhatsApp Connected'
        CALL_CONNECTED = 'call_connected', 'Call Connected'

        # Conversion stages
        CLOSING_ONGOING = 'closing_ongoing', 'Closing Ongoing'
        CLOSED = 'closed', 'Closed'
        FAILED_RETRY = 'failed_retry', 'Failed to Close & Retry'
        LOST = 'lost', 'Lost'

        # Proposal stages
        PROPOSAL_SENT = 'proposal_sent', 'Proposal Sent'
        NEGOTIATION_AFTER_PROPOSAL = 'negotiation_after_proposal', 'Negotiation After Proposal'
        LOST_AFTER_PROPOSAL = 'lost_after_proposal', 'Lost After Proposal'

        # Payment & delivery stages
        ADVANCE_RECEIVED_PROJECT_STARTED = 'advance_received_project_started', 'Advance Received & Project Started'
        PROJECT_HANDED = 'project_handed', 'Project Handed'
        TRAINING_COMPLETED = 'training_completed', 'Training Completed'
        BALANCE_PAID_PROJECT_COMPLETED = 'balance_paid_project_completed', 'Balance Paid & Project Completed'
        ISSUE_PAYMENT_COLLECTION = 'issue_payment_collection', 'Issue in Payment Collection'

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
        # Must fit the longest Status value string (e.g.:
        # advance_received_project_started = 32 chars).
        # Keep generous headroom for future status value additions.
        max_length=64,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )
    # Simple flag for “priority / strong probability” leads.
    high_hope = models.BooleanField(default=False, db_index=True)
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
    # Set when the 5-minute reminder was pushed (one per follow-up).
    reminder_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)

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


class Achievement(models.Model):
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crm_achievements',
    )
    lead = models.ForeignKey(
        Lead,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='achievements',
    )
    package = models.ForeignKey(
        Package,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='achievements',
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    achieved_date = models.DateField(db_index=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_achievements',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-achieved_date', '-id']
        indexes = [
            models.Index(fields=['employee', 'achieved_date']),
            models.Index(fields=['employee', 'package', 'achieved_date']),
        ]

    def __str__(self):
        return f'{self.employee} — {self.amount} on {self.achieved_date}'


class MonthlyTarget(models.Model):
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crm_monthly_targets',
    )
    month = models.DateField(
        help_text='First day of the month this target applies to.',
    )
    target_amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        verbose_name = 'Monthly target'
        verbose_name_plural = 'Monthly targets'
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'month'],
                name='uniq_employee_month_target',
            ),
        ]
        indexes = [
            models.Index(fields=['employee', 'month']),
        ]

    def __str__(self):
        return f'{self.employee} — {self.month.strftime("%Y-%m")} target {self.target_amount}'

    def clean(self):
        super().clean()
        if self.month and self.month.day != 1:
            # Normalize to first day so month-based lookups remain consistent.
            self.month = self.month.replace(day=1)

    def save(self, *args, **kwargs):
        if self.month and self.month.day != 1:
            self.month = self.month.replace(day=1)
        return super().save(*args, **kwargs)
