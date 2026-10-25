"""Organisation, access control and cross-cutting infrastructure models (Part 3, 13, 14.1 core)."""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.utils import timezone

from crm.core.india import STATE_CHOICES

from .base import USER, MoneyField, RateField, TimeStamped, Versioned


_PRIVATE_STORAGE = None


def private_storage():
    """Uploads live outside MEDIA_ROOT and are only served through signed, permission-checked URLs.

    A callable, so migrations store a reference instead of a machine-specific path."""
    global _PRIVATE_STORAGE
    if _PRIVATE_STORAGE is None:
        _PRIVATE_STORAGE = FileSystemStorage(location=str(settings.CRM_PRIVATE_MEDIA_ROOT), base_url=None)
    return _PRIVATE_STORAGE


# ---------------------------------------------------------------- Organisation

class CompanySettings(Versioned):
    """Singleton: company identity, tax registration, SLAs and policy limits (Part 13.6)."""
    legal_name = models.CharField(max_length=200, default='BThinkX LLP')
    display_name = models.CharField(max_length=120, default='BThinkX')
    gstin = models.CharField('GSTIN', max_length=15, blank=True)
    pan = models.CharField('PAN', max_length=10, blank=True)
    gst_registered = models.BooleanField(default=True)
    lut_enabled = models.BooleanField('LUT for exports/SEZ', default=False,
                                      help_text='When on, supplies to SEZ or overseas are zero-rated (CA-approved).')
    state_code = models.CharField('State', max_length=2, choices=STATE_CHOICES, default='32')
    address_line1 = models.CharField(max_length=200, default='2nd Floor, Suffis Arcade')
    address_line2 = models.CharField(max_length=200, blank=True, default='Pattom')
    city = models.CharField(max_length=80, default='Trivandrum')
    pincode = models.CharField(max_length=10, default='695004')
    phone = models.CharField(max_length=20, default='+919544196763')
    email = models.EmailField(default='hr@bthinkx.com')
    website = models.URLField(blank=True, default='https://bthinkx.com')
    bank_name = models.CharField(max_length=120, blank=True)
    bank_account_name = models.CharField(max_length=120, blank=True)
    bank_account_number = models.CharField(max_length=30, blank=True)
    bank_ifsc = models.CharField('IFSC', max_length=11, blank=True)
    bank_branch = models.CharField(max_length=120, blank=True)
    upi_id = models.CharField('UPI ID', max_length=80, blank=True)
    signatory_name = models.CharField(max_length=120, blank=True, default='Authorised Signatory')
    signatory_designation = models.CharField(max_length=120, blank=True, default='Partner')

    # Documents
    round_off_quotation = models.BooleanField('Round off quotations to the rupee', default=False)
    round_off_invoice = models.BooleanField('Round off invoices to the rupee', default=False)
    quotation_validity_days = models.PositiveSmallIntegerField(default=15)
    invoice_due_days = models.PositiveSmallIntegerField(default=15)
    quotation_terms = models.TextField(blank=True, default=(
        '1. Prices are valid until the validity date shown above.\n'
        '2. Work starts after the advance payment is received.\n'
        '3. Additional features requested after approval are quoted separately.'))
    invoice_terms = models.TextField(blank=True, default='Payment is due by the due date shown above.')
    refund_policy = models.TextField(blank=True, default='Refunds are processed to the original payment method within 7 working days of approval.')
    advance_allocation_rule = models.CharField(
        max_length=20, default='proportional',
        choices=[('proportional', 'Proportional (tax share inside every payment)'), ('tax_first', 'Tax first')])
    tax_on_advance = models.BooleanField('GST due on receipt of advance', default=True,
                                         help_text='CA-approved rule: whether receipt vouchers for advances carry tax.')

    # Lead engine SLAs (Part 4.9)
    first_response_minutes = models.PositiveIntegerField(default=15)
    unassigned_manager_alert_minutes = models.PositiveIntegerField(default=5)
    unassigned_owner_alert_minutes = models.PositiveIntegerField(default=30)
    overdue_manager_hours = models.PositiveIntegerField(default=4)
    overdue_owner_hours = models.PositiveIntegerField(default=24)
    stale_days = models.PositiveIntegerField(default=3)
    stale_escalate_days = models.PositiveIntegerField(default=7)
    aging_qualified_days = models.PositiveIntegerField(default=7)
    aging_quotation_sent_days = models.PositiveIntegerField(default=5)
    aging_negotiation_days = models.PositiveIntegerField(default=10)
    postpone_review_count = models.PositiveIntegerField(default=3)
    work_start_hour = models.PositiveSmallIntegerField(default=9)
    work_end_hour = models.PositiveSmallIntegerField(default=19)
    default_open_lead_cap = models.PositiveIntegerField(default=200)
    lost_approval_threshold = MoneyField(default=Decimal('100000.00'))
    win_back_days = models.PositiveIntegerField(default=90)

    # Policy limits (Part 3.4)
    exec_discount_limit = RateField(default=Decimal('5.00'))
    manager_discount_limit = RateField(default=Decimal('15.00'))
    special_terms_days = models.PositiveIntegerField(default=30)
    exec_payment_link_limit = MoneyField(default=Decimal('100000.00'))
    payment_link_default_expiry_days = models.PositiveIntegerField(default=7)
    payment_link_max_expiry_days = models.PositiveIntegerField(default=30)
    min_partial_payment = MoneyField(default=Decimal('1000.00'))
    lead_export_approval_rows = models.PositiveIntegerField(default=100)
    manual_payment_proof_above = MoneyField(default=Decimal('10000.00'))
    expense_proof_above = MoneyField(default=Decimal('2000.00'))
    expense_manager_limit = MoneyField(default=Decimal('25000.00'))
    credit_note_approval_above = MoneyField(default=Decimal('0.00'))
    journal_approval_above = MoneyField(default=Decimal('100000.00'))
    margin_floor_percent = RateField(default=Decimal('20.00'))
    idle_timeout_minutes = models.PositiveIntegerField(default=30)

    # Payment gateway (Part 6.8); secrets are encrypted and write-only.
    gateway_provider = models.CharField(max_length=20, default='none',
                                        choices=[('none', 'Not configured'), ('cashfree', 'Cashfree'),
                                                 ('sandbox', 'Built-in sandbox (testing only)')])
    gateway_live = models.BooleanField('Live mode', default=False)
    gateway_app_id = models.CharField('App ID / key ID', max_length=120, blank=True)
    gateway_secret_enc = models.TextField(blank=True, editable=False)
    gateway_webhook_secret_enc = models.TextField(blank=True, editable=False)
    gateway_methods = models.JSONField(default=list, blank=True)
    gateway_secret_rotated_at = models.DateTimeField(null=True, blank=True, editable=False)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        verbose_name = 'company settings'
        verbose_name_plural = 'company settings'

    def __str__(self):
        return self.legal_name

    @classmethod
    def get(cls) -> 'CompanySettings':
        obj = cls.objects.order_by('pk').first()
        if obj is None:
            obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def address_text(self) -> str:
        from crm.core.india import state_name
        parts = [self.address_line1, self.address_line2, f'{self.city} {self.pincode}'.strip(),
                 state_name(self.state_code)]
        return ', '.join(p for p in parts if p)


class Department(TimeStamped):
    name = models.CharField(max_length=120, unique=True)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='children')
    head = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_headed_departments')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Team(TimeStamped):
    name = models.CharField(max_length=120, unique=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT, related_name='teams')
    manager = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_managed_teams')
    is_sales = models.BooleanField('Sales team (receives leads)', default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Role(models.Model):
    """A bundle of permissions an administrator can edit (Part 3.1). No code refers to a job title."""
    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80, unique=True)
    description = models.CharField(max_length=300, blank=True)
    grants = models.JSONField(default=dict, blank=True, help_text='{module: {action: scope}}')
    field_groups = models.JSONField(default=list, blank=True, help_text='Sensitive field groups this role may see.')
    denied_field_groups = models.JSONField(default=list, blank=True, help_text='Explicit deny overrides allow.')
    home = models.CharField(max_length=60, default='crm:my_day')
    is_system = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class UserProfile(Versioned):
    user = models.OneToOneField(USER, on_delete=models.PROTECT, related_name='crm_profile')
    roles = models.ManyToManyField(Role, blank=True, related_name='profiles')
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.PROTECT, related_name='members')
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT, related_name='members')
    manager = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_reports')
    phone = models.CharField(max_length=20, blank=True)
    designation = models.CharField(max_length=120, blank=True)
    receives_leads = models.BooleanField('Receives auto-assigned leads', default=False)
    is_available = models.BooleanField(default=True, help_text='Off while on leave; assignment skips unavailable users.')
    unavailable_until = models.DateField(null=True, blank=True)
    open_lead_cap = models.PositiveIntegerField(null=True, blank=True)
    assignment_weight = models.PositiveSmallIntegerField(default=1)
    last_assigned_at = models.DateTimeField(null=True, blank=True)
    failed_logins = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_enabled = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    delegate_to = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+',
                                    help_text='Approval delegate')
    delegate_from = models.DateField(null=True, blank=True)
    delegate_until = models.DateField(null=True, blank=True)
    notify_email = models.BooleanField(default=True)
    quiet_hours = models.BooleanField(default=True)

    def __str__(self):
        return f'Profile of {self.user}'


# ---------------------------------------------------------------- Audit and security

class AuditLog(models.Model):
    """Append-only (Part 13.5). Update/delete are blocked in code and by a PostgreSQL trigger."""
    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor_type = models.CharField(max_length=20, default='user')  # user, system, job, webhook, customer
    actor_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    actor_name = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=80, db_index=True)
    object_type = models.CharField(max_length=80, blank=True, db_index=True)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    object_repr = models.CharField(max_length=200, blank=True)
    result = models.CharField(max_length=20, default='success')
    changes = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    request_id = models.CharField(max_length=40, blank=True)
    job_id = models.CharField(max_length=64, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    channel = models.CharField(max_length=20, default='web')

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['object_type', 'object_id', '-id'])]

    def __str__(self):
        return f'{self.action} {self.object_type}:{self.object_id}'

    def save(self, *args, **kwargs):
        if self.pk and AuditLog.objects.filter(pk=self.pk).exists():
            raise ValidationError('Audit log rows cannot be changed.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Audit log rows cannot be deleted.')


class AuditCheckpoint(models.Model):
    """Periodic hash-chain checkpoint so tampering is detectable (Part 13.5)."""
    created_at = models.DateTimeField(default=timezone.now)
    first_id = models.BigIntegerField()
    last_id = models.BigIntegerField()
    row_count = models.PositiveIntegerField()
    previous_hash = models.CharField(max_length=64, blank=True)
    chain_hash = models.CharField(max_length=64)

    class Meta:
        ordering = ['-last_id']


class SecurityEvent(models.Model):
    KINDS = [
        ('login_success', 'Login'), ('login_failed', 'Failed login'), ('lockout', 'Account locked'),
        ('logout', 'Logout'), ('forbidden', 'Forbidden attempt'), ('invalid_signature', 'Invalid signature'),
        ('password_changed', 'Password changed'), ('sessions_revoked', 'Signed out all devices'),
        ('two_factor', 'Two-factor event'), ('rate_limited', 'Rate limited'),
    ]
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    kind = models.CharField(max_length=30, choices=KINDS, db_index=True)
    user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    username = models.CharField(max_length=150, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    path = models.CharField(max_length=300, blank=True)
    detail = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-id']


class UserSession(models.Model):
    """Tracks sessions so a user can see devices and sign out everywhere (Part 3.6)."""
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='crm_sessions')
    session_key = models.CharField(max_length=40, unique=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-last_seen_at']


# ---------------------------------------------------------------- Notifications

class Notification(models.Model):
    LEVELS = [('info', 'Info'), ('success', 'Success'), ('warning', 'Warning'), ('critical', 'Critical')]
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='crm_notifications')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    event_type = models.CharField(max_length=60, db_index=True)
    level = models.CharField(max_length=10, choices=LEVELS, default='info')
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=500, blank=True)
    url = models.CharField(max_length=300, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['user', 'read_at', '-id'])]
        constraints = [
            models.UniqueConstraint(fields=['user', 'dedupe_key'], condition=~models.Q(dedupe_key=''),
                                    name='crm_notification_dedupe'),
        ]


class MessageTemplate(models.Model):
    """Email/WhatsApp templates with variables, versioned (Part 13.3)."""
    code = models.SlugField(max_length=60)
    channel = models.CharField(max_length=20, default='email', choices=[('email', 'Email'), ('whatsapp', 'WhatsApp'), ('sms', 'SMS')])
    name = models.CharField(max_length=120)
    subject = models.CharField(max_length=200, blank=True)
    body = models.TextField()
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    customer_facing = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['code', '-version']
        constraints = [models.UniqueConstraint(fields=['code', 'channel', 'version'], name='crm_template_version')]

    def __str__(self):
        return f'{self.name} v{self.version}'


class DeliveryLog(models.Model):
    """Every outbound message (Part 13.3)."""
    STATUSES = [('queued', 'Queued'), ('sent', 'Sent'), ('delivered', 'Delivered'), ('failed', 'Failed'), ('bounced', 'Bounced')]
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    channel = models.CharField(max_length=20, default='email')
    recipient = models.CharField(max_length=254)
    cc = models.CharField(max_length=500, blank=True)
    subject = models.CharField(max_length=250, blank=True)
    template_code = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default='queued')
    provider_id = models.CharField(max_length=120, blank=True)
    error = models.CharField(max_length=500, blank=True)
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    sent_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    attempts = models.PositiveSmallIntegerField(default=0)
    body = models.TextField(blank=True)
    attachment_name = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-id']


# ---------------------------------------------------------------- Approvals

class ApprovalRequest(models.Model):
    """Configurable maker-checker approvals (Part 3.4)."""
    KINDS = [
        ('quotation', 'Quotation discount or terms'),
        ('payment_link', 'Payment link'),
        ('payment_link_extension', 'Payment link extension'),
        ('gst_override', 'GST override'),
        ('refund', 'Refund'),
        ('expense', 'Expense'),
        ('leave', 'Leave'),
        ('payroll', 'Payroll run'),
        ('period_close', 'Period close'),
        ('period_reopen', 'Period reopen'),
        ('lead_merge', 'Lead merge'),
        ('lead_lost', 'Lost lead above threshold'),
        ('lead_export', 'Lead export'),
        ('credit_note', 'Credit note'),
        ('invoice_cancel', 'Invoice cancellation'),
        ('write_off', 'Bad-debt write-off'),
        ('journal', 'Manual journal'),
        ('journal_reversal', 'Journal reversal'),
        ('contract', 'Contract'),
        ('change_request', 'Change request'),
        ('commission', 'Commission'),
    ]
    STATUSES = [('pending', 'Awaiting approval'), ('approved', 'Approved'), ('rejected', 'Rejected'),
                ('cancelled', 'Cancelled'), ('expired', 'Expired')]
    kind = models.CharField(max_length=30, choices=KINDS, db_index=True)
    status = models.CharField(max_length=12, choices=STATUSES, default='pending', db_index=True)
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.CharField(max_length=64)
    target = GenericForeignKey('content_type', 'object_id')
    title = models.CharField(max_length=200)
    module = models.CharField(max_length=30)
    amount = MoneyField(null=True, blank=True, default=None)
    reasons = models.JSONField(default=list, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    message = models.TextField(blank=True)
    required_level = models.CharField(max_length=20, default='manager',
                                      help_text='manager, finance, accountant, owner, hr')
    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='crm_approval_requests')
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    approver = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_approvals_assigned')
    decided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='crm_approvals_decided')
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['content_type', 'object_id', 'status'])]

    def __str__(self):
        return self.title


# ---------------------------------------------------------------- Infrastructure

class IdempotencyKey(models.Model):
    """One-time form tokens: the first submission wins, repeats get the same result (Part 2.2)."""
    key = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    path = models.CharField(max_length=300)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=12, default='processing')
    response_location = models.CharField(max_length=500, blank=True)


def attachment_upload_to(instance, filename):
    ext = ''
    if '.' in filename:
        ext = '.' + filename.rsplit('.', 1)[1].lower()[:8]
    now = timezone.now()
    return f'attachments/{now:%Y/%m}/{uuid.uuid4().hex}{ext}'


class Attachment(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.CharField(max_length=64)
    target = GenericForeignKey('content_type', 'object_id')
    file = models.FileField(upload_to=attachment_upload_to, storage=private_storage, max_length=300)
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    purpose = models.CharField(max_length=40, blank=True)
    visibility = models.CharField(max_length=12, default='internal', choices=[('internal', 'Internal'), ('shareable', 'Shareable')])
    uploaded_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    uploaded_at = models.DateTimeField(default=timezone.now)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['content_type', 'object_id'])]

    def __str__(self):
        return self.original_name


class JobRun(models.Model):
    """Every scheduled/automated run (Part 13.4). Idempotency key blocks duplicate financial results."""
    STATUSES = [('queued', 'Queued'), ('running', 'Running'), ('succeeded', 'Succeeded'), ('failed', 'Failed'),
                ('skipped', 'Skipped'), ('resolved', 'Resolved')]
    job = models.CharField(max_length=80, db_index=True)
    idempotency_key = models.CharField(max_length=160, unique=True)
    status = models.CharField(max_length=12, choices=STATUSES, default='queued', db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    resolved_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-id']


class ExportLog(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    module = models.CharField(max_length=40)
    filters = models.JSONField(default=dict, blank=True)
    columns = models.JSONField(default=list, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    file_format = models.CharField(max_length=10, default='csv')

    class Meta:
        ordering = ['-id']


class DocumentSequence(models.Model):
    """Gap-free numbering per document type, per FY (Part 1.5). Row is locked at issue time."""
    doc_type = models.CharField(max_length=20)
    prefix = models.CharField(max_length=10)
    fiscal_year = models.CharField(max_length=7)
    next_number = models.PositiveIntegerField(default=1)
    padding = models.PositiveSmallIntegerField(default=4)
    first_issued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['doc_type', 'fiscal_year'], name='crm_sequence_unique')]
        ordering = ['doc_type', '-fiscal_year']

    def __str__(self):
        return f'{self.prefix}/{self.fiscal_year}'


class SequenceSeries(models.Model):
    """Configured prefix/padding per document type; frozen after first issue (Part 13.6)."""
    doc_type = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=60)
    prefix = models.CharField(max_length=10)
    padding = models.PositiveSmallIntegerField(default=4)

    class Meta:
        ordering = ['doc_type']
        verbose_name_plural = 'sequence series'

    def __str__(self):
        return f'{self.name} ({self.prefix})'


class SavedFilter(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='crm_saved_filters')
    module = models.CharField(max_length=40)
    name = models.CharField(max_length=60)
    querystring = models.CharField(max_length=1000)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['name']
        constraints = [models.UniqueConstraint(fields=['user', 'module', 'name'], name='crm_saved_filter_unique')]


class ImportBatch(TimeStamped):
    """Lead / data import batch: stores every row result (Part 4.8)."""
    STATUSES = [('uploaded', 'Uploaded'), ('mapped', 'Mapped'), ('validated', 'Validated'),
                ('imported', 'Imported'), ('undone', 'Undone'), ('failed', 'Failed')]
    module = models.CharField(max_length=30, default='leads')
    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64)
    file = models.FileField(upload_to='imports/%Y/%m', storage=private_storage, max_length=300)
    status = models.CharField(max_length=12, choices=STATUSES, default='uploaded')
    headers = models.JSONField(default=list, blank=True)
    mapping = models.JSONField(default=dict, blank=True)
    options = models.JSONField(default=dict, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    duplicate_rows = models.PositiveIntegerField(default=0)
    invalid_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    interaction_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    row_results = models.JSONField(default=list, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    undone_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f'Import #{self.pk} {self.file_name}'

    @property
    def can_undo(self) -> bool:
        return bool(self.imported_at and not self.undone_at and
                    timezone.now() - self.imported_at < timedelta(hours=24))
