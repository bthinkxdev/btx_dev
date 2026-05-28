import uuid
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
    crm_role = models.CharField(
        max_length=20,
        choices=(
            ('admin', 'Admin'),
            ('dev', 'Developer'),
            ('support', 'Support'),
            ('sales', 'Sales'),
        ),
        default='sales',
        db_index=True,
        help_text='CRM RBAC: controls provisioning, credentials, and secrets access.',
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


class Client(models.Model):
    lead = models.OneToOneField(
        Lead,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='client',
    )
    business_name = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=200)
    phone = models.CharField(max_length=40)
    email = models.EmailField(blank=True)
    gst_number = models.CharField(max_length=20, blank=True)
    pan_number = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_clients',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.business_name


class Project(models.Model):
    class Status(models.TextChoices):
        ONBOARDING_PENDING = 'onboarding_pending', 'Onboarding Pending'
        ONBOARDING_SUBMITTED = 'onboarding_submitted', 'Onboarding Submitted'
        IN_DEVELOPMENT = 'in_development', 'In Development'
        REVIEW = 'review', 'Client Review'
        COMPLETED = 'completed', 'Completed'
        SUSPENDED = 'suspended', 'Suspended'

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name='projects',
    )
    package = models.ForeignKey(
        Package,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='projects',
    )
    deal_value = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0'),
    )
    advance_received = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0'),
    )
    balance_due = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0'),
    )
    status = models.CharField(
        max_length=40,
        choices=Status.choices,
        default=Status.ONBOARDING_PENDING,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_projects',
    )
    onboarding_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_projects',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.client.business_name} — {self.package}'

    def save(self, *args, **kwargs):
        self.balance_due = self.deal_value - self.advance_received
        super().save(*args, **kwargs)

    @property
    def assignee_usernames(self):
        names = list(
            self.memberships.values_list('user__username', flat=True)
        )
        if names:
            return names
        if self.assigned_to_id:
            return [self.assigned_to.get_username()]
        return []


class ProjectMember(models.Model):
    """Many-to-many team assignment for delivery projects."""

    class Role(models.TextChoices):
        LEAD = 'lead', 'Lead'
        MEMBER = 'member', 'Member'

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='project_memberships',
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.MEMBER,
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-role', 'added_at']
        constraints = [
            models.UniqueConstraint(
                fields=['project', 'user'],
                name='crm_projectmember_project_user_uniq',
            ),
        ]

    def __str__(self):
        return f'{self.project_id} · {self.user_id}'


class ProjectTicket(models.Model):
    """Simple Jira-style task board scoped to a project."""

    class Status(models.TextChoices):
        BACKLOG = 'backlog', 'Backlog'
        TODO = 'todo', 'To Do'
        IN_PROGRESS = 'in_progress', 'In Progress'
        REVIEW = 'review', 'Review'
        DONE = 'done', 'Done'

    class Priority(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='tickets',
    )
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.BACKLOG,
        db_index=True,
    )
    priority = models.CharField(
        max_length=10,
        choices=Priority.choices,
        default=Priority.MEDIUM,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='project_tickets_assigned',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='project_tickets_created',
    )
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['status', 'position', '-updated_at']

    def __str__(self):
        return self.title


class ProjectTicketLink(models.Model):
    """Reference links shown with the ticket description."""

    ticket = models.ForeignKey(
        ProjectTicket,
        on_delete=models.CASCADE,
        related_name='links',
    )
    label = models.CharField(max_length=200)
    url = models.URLField(max_length=500)
    note = models.TextField(blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.label or self.url


class ProjectTicketAttachment(models.Model):
    class Visibility(models.TextChoices):
        TEAM = 'team', 'Project team'
        ASSIGNEE = 'assignee', 'Assignee only'
        LEAD = 'lead', 'Team lead only'

    ticket = models.ForeignKey(
        ProjectTicket,
        on_delete=models.CASCADE,
        related_name='attachments',
    )
    file = models.FileField(upload_to='crm/ticket_attachments/%Y/%m/')
    original_name = models.CharField(max_length=255, blank=True)
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.TEAM,
    )
    save_to_desk = models.BooleanField(
        default=False,
        help_text='Highlight as a desk download for the team.',
    )
    is_reference_screenshot = models.BooleanField(
        default=False,
        help_text='Shown in the reference screenshots gallery on the ticket.',
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='project_ticket_uploads',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return self.original_name or str(self.file)


class OnboardingSubmission(models.Model):
    class SectionStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PARTIAL = 'partial', 'Partial'
        SUBMITTED = 'submitted', 'Submitted'
        VERIFIED = 'verified', 'Verified'
        REJECTED = 'rejected', 'Rejected'

    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name='onboarding',
    )

    business_name = models.CharField(max_length=200, blank=True)
    tagline = models.CharField(max_length=200, blank=True)
    business_description = models.TextField(blank=True)
    years_in_business = models.CharField(max_length=20, blank=True)
    industry = models.CharField(max_length=100, blank=True)
    target_audience = models.TextField(blank=True)
    competitors = models.TextField(blank=True)
    usp = models.TextField(blank=True)

    contact_phone = models.CharField(max_length=40, blank=True)
    whatsapp_number = models.CharField(max_length=40, blank=True)
    contact_email = models.EmailField(blank=True)
    office_address = models.TextField(blank=True)
    google_maps_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    facebook_url = models.URLField(blank=True)
    youtube_url = models.URLField(blank=True)
    website_url = models.URLField(blank=True)

    brand_colors = models.CharField(max_length=200, blank=True)
    brand_fonts = models.CharField(max_length=200, blank=True)
    brand_notes = models.TextField(blank=True)

    reference_websites = models.TextField(blank=True)
    website_requirements = models.JSONField(default=dict, blank=True)

    about_us = models.TextField(blank=True)
    privacy_policy = models.TextField(blank=True)
    refund_policy = models.TextField(blank=True)
    shipping_policy = models.TextField(blank=True)
    terms_and_conditions = models.TextField(blank=True)
    faq = models.TextField(blank=True)

    logo_file = models.FileField(
        upload_to='onboarding/logos/',
        blank=True,
        null=True,
    )
    gst_certificate = models.FileField(
        upload_to='onboarding/docs/',
        blank=True,
        null=True,
    )
    pan_document = models.FileField(
        upload_to='onboarding/docs/',
        blank=True,
        null=True,
    )
    business_registration = models.FileField(
        upload_to='onboarding/docs/',
        blank=True,
        null=True,
    )
    brand_guidelines = models.FileField(
        upload_to='onboarding/docs/',
        blank=True,
        null=True,
    )

    # Payment gateway KYC (owner + bank + registered phone numbers).
    owner_aadhaar_front = models.FileField(
        upload_to='onboarding/kyc/',
        blank=True,
        null=True,
    )
    owner_aadhaar_back = models.FileField(
        upload_to='onboarding/kyc/',
        blank=True,
        null=True,
    )
    bank_name = models.CharField(max_length=120, blank=True)
    bank_account_number = models.CharField(max_length=34, blank=True)
    bank_ifsc = models.CharField(max_length=11, blank=True)
    bank_branch_name = models.CharField(max_length=200, blank=True)
    phone_registered_bank = models.CharField(max_length=40, blank=True)
    phone_registered_aadhaar = models.CharField(max_length=40, blank=True)
    phone_registered_pan = models.CharField(max_length=40, blank=True)
    kyc_privacy_acknowledged = models.BooleanField(
        default=False,
        help_text='Client confirmed purpose + undertaking for payment KYC.',
    )

    terms_accepted = models.BooleanField(default=False)
    terms_version = models.CharField(max_length=20, blank=True, default='1.0')
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    terms_accepted_ip = models.GenericIPAddressField(null=True, blank=True)

    business_info_status = models.CharField(
        max_length=20,
        choices=SectionStatus.choices,
        default=SectionStatus.PENDING,
    )
    contact_status = models.CharField(
        max_length=20,
        choices=SectionStatus.choices,
        default=SectionStatus.PENDING,
    )
    branding_status = models.CharField(
        max_length=20,
        choices=SectionStatus.choices,
        default=SectionStatus.PENDING,
    )
    documents_status = models.CharField(
        max_length=20,
        choices=SectionStatus.choices,
        default=SectionStatus.PENDING,
    )
    payment_kyc_status = models.CharField(
        max_length=20,
        choices=SectionStatus.choices,
        default=SectionStatus.PENDING,
    )
    requirements_status = models.CharField(
        max_length=20,
        choices=SectionStatus.choices,
        default=SectionStatus.PENDING,
    )
    content_status = models.CharField(
        max_length=20,
        choices=SectionStatus.choices,
        default=SectionStatus.PENDING,
    )
    agreement_status = models.CharField(
        max_length=20,
        choices=SectionStatus.choices,
        default=SectionStatus.PENDING,
    )

    internal_notes = models.TextField(blank=True)
    client_notes = models.TextField(
        blank=True,
        help_text='Notes visible to the client / tenant portal (never mix with internal_notes).',
    )

    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'Onboarding — {self.project}'

    def _filled_str(self, value) -> bool:
        if value is None:
            return False
        return bool(str(value).strip())

    def _filled_file(self, field_name: str) -> bool:
        f = getattr(self, field_name, None)
        return bool(f and getattr(f, 'name', ''))

    def _section_business_pct(self) -> float:
        fields = (
            'business_name',
            'tagline',
            'business_description',
            'years_in_business',
            'industry',
            'target_audience',
            'competitors',
            'usp',
        )
        total = len(fields)
        filled = sum(1 for n in fields if self._filled_str(getattr(self, n)))
        return (filled / total) * 100.0 if total else 0.0

    def _section_contact_pct(self) -> float:
        fields = (
            'contact_phone',
            'whatsapp_number',
            'contact_email',
            'office_address',
            'google_maps_url',
            'instagram_url',
            'facebook_url',
            'youtube_url',
            'website_url',
        )
        total = len(fields)
        filled = sum(1 for n in fields if self._filled_str(getattr(self, n)))
        return (filled / total) * 100.0 if total else 0.0

    def _section_branding_pct(self) -> float:
        fields = ('brand_colors', 'brand_fonts', 'brand_notes')
        total = len(fields)
        filled = sum(1 for n in fields if self._filled_str(getattr(self, n)))
        return (filled / total) * 100.0 if total else 0.0

    def _section_documents_pct(self) -> float:
        files = (
            'logo_file',
            'gst_certificate',
            'pan_document',
            'business_registration',
            'brand_guidelines',
        )
        total = len(files)
        filled = sum(1 for n in files if self._filled_file(n))
        return (filled / total) * 100.0 if total else 0.0

    def _section_payment_kyc_pct(self) -> float:
        files = ('owner_aadhaar_front', 'owner_aadhaar_back')
        str_fields = (
            'bank_name',
            'bank_account_number',
            'bank_ifsc',
            'bank_branch_name',
            'phone_registered_bank',
            'phone_registered_aadhaar',
            'phone_registered_pan',
        )
        parts = []
        parts.extend(1.0 if self._filled_file(n) else 0.0 for n in files)
        parts.extend(1.0 if self._filled_str(getattr(self, n)) else 0.0 for n in str_fields)
        parts.append(1.0 if self.kyc_privacy_acknowledged else 0.0)
        total = len(parts)
        return (sum(parts) / total) * 100.0 if total else 0.0

    def is_payment_kyc_complete(self) -> bool:
        """Required before final onboarding submit (payment gateway registration)."""
        if not self.kyc_privacy_acknowledged:
            return False
        if not self._filled_file('owner_aadhaar_front') or not self._filled_file(
            'owner_aadhaar_back'
        ):
            return False
        for n in (
            'bank_name',
            'bank_account_number',
            'bank_ifsc',
            'bank_branch_name',
            'phone_registered_bank',
            'phone_registered_aadhaar',
            'phone_registered_pan',
        ):
            if not self._filled_str(getattr(self, n)):
                return False
        return True

    def payment_kyc_incomplete_reasons(self) -> list[str]:
        """Human-readable gaps for final submit validation and messaging."""
        reasons = []
        if not self.kyc_privacy_acknowledged:
            reasons.append(
                'Tick “I confirm I have read the notice and undertaking” at the top of '
                'Payment & owner KYC, then save that section again.'
            )
        if not self._filled_file('owner_aadhaar_front'):
            reasons.append('Upload owner’s Aadhaar (front).')
        if not self._filled_file('owner_aadhaar_back'):
            reasons.append('Upload owner’s Aadhaar (back).')
        if not self._filled_str(self.bank_name):
            reasons.append('Enter the bank name.')
        if not self._filled_str(self.bank_account_number):
            reasons.append('Enter the bank account number.')
        if not self._filled_str(self.bank_ifsc):
            reasons.append('Enter the 11-character bank IFSC code.')
        if not self._filled_str(self.bank_branch_name):
            reasons.append('Enter the branch name.')
        if not self._filled_str(self.phone_registered_bank):
            reasons.append('Enter the phone number registered with your bank.')
        if not self._filled_str(self.phone_registered_aadhaar):
            reasons.append('Enter the phone number registered with Aadhaar.')
        if not self._filled_str(self.phone_registered_pan):
            reasons.append('Enter the phone number registered with PAN.')
        return reasons

    def _section_requirements_pct(self) -> float:
        ref_ok = self._filled_str(self.reference_websites)
        keys = (
            'login',
            'cart',
            'wishlist',
            'payment_gateway',
            'booking',
            'blog',
            'chat',
            'admin_dashboard',
            'reports',
            'subscription',
        )
        data = self.website_requirements or {}
        if not isinstance(data, dict):
            data = {}
        truths = sum(1 for k in keys if bool(data.get(k)))
        json_pct = (truths / len(keys)) * 100.0 if keys else 0.0
        ref_pct = 100.0 if ref_ok else 0.0
        return (ref_pct + json_pct) / 2.0

    def _section_content_pct(self) -> float:
        fields = (
            'about_us',
            'privacy_policy',
            'refund_policy',
            'shipping_policy',
            'terms_and_conditions',
            'faq',
        )
        total = len(fields)
        filled = sum(1 for n in fields if self._filled_str(getattr(self, n)))
        return (filled / total) * 100.0 if total else 0.0

    def _section_agreement_pct(self) -> float:
        return 100.0 if self.terms_accepted else 0.0

    def overall_completion_percent(self) -> int:
        """
        Sections weighted equally. Eight groups: business, contact, branding,
        documents, payment KYC, requirements, content, agreement.
        """
        parts = (
            self._section_business_pct(),
            self._section_contact_pct(),
            self._section_branding_pct(),
            self._section_documents_pct(),
            self._section_payment_kyc_pct(),
            self._section_requirements_pct(),
            self._section_content_pct(),
            self._section_agreement_pct(),
        )
        return int(round(sum(parts) / len(parts))) if parts else 0

    def section_completion_percent(self, section_name: str) -> int:
        funcs = {
            'business_info': self._section_business_pct,
            'contact': self._section_contact_pct,
            'branding': self._section_branding_pct,
            'documents': self._section_documents_pct,
            'payment_kyc': self._section_payment_kyc_pct,
            'requirements': self._section_requirements_pct,
            'content': self._section_content_pct,
            'agreement': self._section_agreement_pct,
        }
        fn = funcs.get(section_name)
        if not fn:
            return 0
        return int(round(fn()))

    def is_fully_submitted(self) -> bool:
        return bool(self.terms_accepted and self.submitted_at is not None)


class ProjectProvisioning(models.Model):
    """One operational provisioning bucket per project (steps are child rows)."""

    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name='provisioning',
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='provisionings_assigned',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'Provisioning — {self.project_id}'


class ProvisioningStep(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        BLOCKED = 'blocked', 'Blocked'

    provisioning = models.ForeignKey(
        ProjectProvisioning,
        on_delete=models.CASCADE,
        related_name='steps',
    )
    step_key = models.CharField(max_length=80, db_index=True)
    domain = models.CharField(
        max_length=40,
        db_index=True,
        help_text='Logical area: tenant, payment_gateway, delivery, email, infrastructure.',
    )
    provider_type = models.CharField(
        max_length=80,
        blank=True,
        help_text='Integration class, e.g. payment_gateway, smtp, dns — not a vendor lock-in name.',
    )
    provider_name = models.CharField(
        max_length=120,
        blank=True,
        help_text='Human/provider label stored for display; integrations stay metadata-driven.',
    )
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='provisioning_steps_assigned',
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='provisioning_steps_completed',
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['domain', 'step_key']
        constraints = [
            models.UniqueConstraint(
                fields=['provisioning', 'step_key'],
                name='uniq_provisioning_step_key',
            ),
        ]

    def __str__(self):
        return f'{self.step_key} ({self.get_status_display()})'


class ProjectCredential(models.Model):
    """Vault entry: secrets stored only encrypted at rest."""

    class Category(models.TextChoices):
        PAYMENT_GATEWAY = 'payment_gateway', 'Payment gateway'
        DELIVERY = 'delivery', 'Delivery'
        SMTP = 'smtp', 'SMTP'
        ADMIN_PANEL = 'admin_panel', 'Admin panel'
        EMAIL = 'email', 'Email'
        DOMAIN = 'domain', 'Domain'
        ANALYTICS = 'analytics', 'Analytics'
        META = 'meta', 'Meta'
        HOSTING_INTERNAL = 'hosting_internal', 'Hosting (internal)'
        OTHER = 'other', 'Other'

    class CredentialType(models.TextChoices):
        ADMIN_LOGIN = 'admin_login', 'Admin login'
        DELIVERY_LOGIN = 'delivery_login', 'Delivery panel login'
        MAILBOX_LOGIN = 'mailbox_login', 'Mailbox login'
        API_KEY = 'api_key', 'API key'
        WEBHOOK_SECRET = 'webhook_secret', 'Webhook secret'
        SMTP_SECRET = 'smtp_secret', 'SMTP secret'
        INFRA = 'infra', 'Infrastructure'
        OTHER = 'other', 'Other'

    class VisibilityLevel(models.TextChoices):
        INTERNAL = 'internal', 'Internal'
        SHARED = 'shared', 'Shared (tenant portal)'
        ADMIN = 'admin', 'Admin'
        DEV = 'dev', 'Developer'
        SUPPORT = 'support', 'Support'
        SALES = 'sales', 'Sales'
        CLIENT = 'client', 'Client'

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='credentials',
    )
    category = models.CharField(max_length=40, choices=Category.choices, default=Category.OTHER)
    provider_type = models.CharField(max_length=80, blank=True)
    provider_name = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    credential_type = models.CharField(
        max_length=40,
        choices=CredentialType.choices,
        default=CredentialType.OTHER,
        db_index=True,
    )
    label = models.CharField(max_length=200)
    username = models.CharField(max_length=200, blank=True)
    password_encrypted = models.TextField(blank=True)
    secret_key_encrypted = models.TextField(blank=True)
    login_url = models.URLField(blank=True)
    is_client_visible = models.BooleanField(default=False)
    visibility_level = models.CharField(
        max_length=20,
        choices=VisibilityLevel.choices,
        default=VisibilityLevel.ADMIN,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='credentials_created',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='credentials_updated',
    )
    rotated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.label} ({self.project_id})'


class CredentialAuditLog(models.Model):
    credential = models.ForeignKey(
        ProjectCredential,
        on_delete=models.CASCADE,
        related_name='audit_logs',
    )
    action = models.CharField(max_length=64, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='credential_audit_events',
    )
    ip = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'{self.action} @ {self.timestamp}'


class ProjectHandover(models.Model):
    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name='handover',
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='handovers_completed',
    )
    handover_notes = models.TextField(blank=True)
    client_notified = models.BooleanField(default=False)
    handover_pdf = models.FileField(
        upload_to='handovers/',
        blank=True,
        null=True,
    )
    tenant_visibility_enabled = models.BooleanField(default=False)
    live_site_url = models.URLField(blank=True)
    admin_site_url = models.URLField(blank=True)
    support_contact = models.CharField(max_length=200, blank=True)
    sla_summary = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'Handover — {self.project_id}'


class RenewalTracker(models.Model):
    class SubjectType(models.TextChoices):
        DOMAIN = 'domain', 'Domain'
        SMTP = 'smtp', 'SMTP'
        SUBSCRIPTION = 'subscription', 'Subscription'
        SSL = 'ssl', 'SSL / TLS'
        DELIVERY = 'delivery', 'Delivery plan'
        OTHER = 'other', 'Other'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        EXPIRED = 'expired', 'Expired'
        SUSPENDED = 'suspended', 'Suspended'

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='renewals',
    )
    subject_type = models.CharField(max_length=30, choices=SubjectType.choices, db_index=True)
    title = models.CharField(max_length=200)
    expires_at = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    renewal_url = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    notified_internal_at = models.DateTimeField(null=True, blank=True)
    notified_client_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['expires_at']

    def __str__(self):
        return f'{self.title} ({self.project_id})'


class HandoverPortalAccess(models.Model):
    """
    Controls client access to their handover portal.
    Separate from onboarding_token — intentionally a different token
    so access can be revoked independently.
    """

    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name='portal_access',
    )
    access_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    is_active = models.BooleanField(default=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activated_portals',
    )
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    access_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f'Portal — {self.project} — active={self.is_active}'


class RenewalReminderLog(models.Model):
    """
    Tracks every reminder email sent so the management command
    is idempotent — it checks this before sending.
    """

    class ReminderType(models.TextChoices):
        DAYS_30 = '30d', '30 Days Before'
        DAYS_7 = '7d', '7 Days Before'
        DAYS_1 = '1d', '1 Day Before'
        EXPIRED = 'expired', 'Expired'
        INTERNAL = 'internal', 'Internal Team Alert'

    renewal = models.ForeignKey(
        RenewalTracker,
        on_delete=models.CASCADE,
        related_name='reminder_logs',
    )
    reminder_type = models.CharField(max_length=10, choices=ReminderType.choices)
    sent_at = models.DateTimeField(auto_now_add=True)
    sent_to = models.EmailField()
    success = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-sent_at']
        constraints = [
            models.UniqueConstraint(
                fields=['renewal', 'reminder_type'],
                name='uniq_renewal_reminder_type',
            )
        ]

    def __str__(self):
        return f'{self.renewal} — {self.reminder_type} — {self.sent_at}'


class AuditEntry(models.Model):
    """
    Central audit log for all significant CRM state changes.
    Separate from CredentialAuditLog (which is credential-specific)
    and ActivityLog (which is lead-specific).
    This covers Project, Client, Onboarding, Provisioning,
    Subscription, Portal, and ChangeRequest events.
    """

    class EventCategory(models.TextChoices):
        PROJECT = 'project', 'Project'
        CLIENT = 'client', 'Client'
        ONBOARDING = 'onboarding', 'Onboarding'
        PROVISIONING = 'provisioning', 'Provisioning'
        CREDENTIAL = 'credential', 'Credential'
        PORTAL = 'portal', 'Portal'
        RENEWAL = 'renewal', 'Renewal'
        CHANGE_REQUEST = 'change_request', 'Change Request'
        SCOPE = 'scope', 'Scope'

    category = models.CharField(
        max_length=20, choices=EventCategory.choices, db_index=True
    )
    action = models.CharField(max_length=100, db_index=True)

    object_type = models.CharField(max_length=60)
    object_id = models.CharField(max_length=40)
    object_repr = models.CharField(max_length=200)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_entries',
    )
    actor_label = models.CharField(max_length=100, blank=True)

    project = models.ForeignKey(
        'Project',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_entries',
        db_index=True,
    )

    before_state = models.JSONField(null=True, blank=True)
    after_state = models.JSONField(null=True, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['category', 'created_at']),
            models.Index(fields=['project', 'created_at']),
            models.Index(fields=['actor', 'created_at']),
            models.Index(fields=['object_type', 'object_id']),
        ]

    def __str__(self):
        return f'{self.category}:{self.action} — {self.object_repr}'


class PackageScope(models.Model):
    """
    Defines what features and limits are included in a Package.
    One-to-one with Package. Created when Package is created via signal.
    """

    package = models.OneToOneField(
        'Package', on_delete=models.CASCADE, related_name='scope'
    )

    includes_ecommerce = models.BooleanField(default=False)
    includes_blog = models.BooleanField(default=False)
    includes_booking = models.BooleanField(default=False)
    includes_multi_vendor = models.BooleanField(default=False)
    includes_custom_domain = models.BooleanField(default=False)
    includes_payment_gateway = models.BooleanField(default=False)
    includes_delivery_integration = models.BooleanField(default=False)
    includes_smtp_setup = models.BooleanField(default=False)
    includes_seo_basic = models.BooleanField(default=False)
    includes_social_media_setup = models.BooleanField(default=False)
    includes_marketing_ads = models.BooleanField(default=False)

    max_pages = models.PositiveIntegerField(
        default=5, help_text='Maximum website pages included'
    )
    max_products = models.PositiveIntegerField(
        default=0, help_text='0 means not applicable'
    )
    max_admin_users = models.PositiveIntegerField(default=1)
    storage_gb = models.PositiveIntegerField(
        default=5, help_text='Storage limit in GB'
    )
    support_months = models.PositiveIntegerField(
        default=3, help_text='Months of included support'
    )
    revision_rounds = models.PositiveIntegerField(
        default=2, help_text='Number of revision rounds included'
    )

    scope_notes = models.TextField(blank=True)

    exclusions = models.TextField(
        blank=True,
        help_text='Comma-separated list of excluded features',
    )

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Scope — {self.package.name}'

    def feature_flags(self) -> dict:
        """Returns dict of all boolean scope fields and their values."""
        return {
            'includes_ecommerce': self.includes_ecommerce,
            'includes_blog': self.includes_blog,
            'includes_booking': self.includes_booking,
            'includes_multi_vendor': self.includes_multi_vendor,
            'includes_custom_domain': self.includes_custom_domain,
            'includes_payment_gateway': self.includes_payment_gateway,
            'includes_delivery_integration': self.includes_delivery_integration,
            'includes_smtp_setup': self.includes_smtp_setup,
            'includes_seo_basic': self.includes_seo_basic,
            'includes_social_media_setup': self.includes_social_media_setup,
            'includes_marketing_ads': self.includes_marketing_ads,
        }

    def check_request_in_scope(self, requested_features: list) -> dict:
        """
        Takes a list of feature key strings from ChangeRequest.
        Returns in_scope / out_of_scope / verdict.
        """
        from .services.scope import FEATURE_KEY_MAP

        if not isinstance(requested_features, list):
            requested_features = []
        in_scope: list[str] = []
        out_of_scope: list[str] = []
        flags = self.feature_flags()
        for key in requested_features:
            if key not in FEATURE_KEY_MAP:
                out_of_scope.append(key)
                continue
            field = FEATURE_KEY_MAP[key]
            if flags.get(field):
                in_scope.append(key)
            else:
                out_of_scope.append(key)
        if not requested_features:
            verdict = 'in_scope'
        elif out_of_scope and in_scope:
            verdict = 'partial'
        elif out_of_scope:
            verdict = 'out_of_scope'
        else:
            verdict = 'in_scope'
        return {
            'in_scope': in_scope,
            'out_of_scope': out_of_scope,
            'verdict': verdict,
        }


class ChangeRequest(models.Model):
    """
    Client-submitted request for changes after project completion.
    Can be submitted via the client portal or by internal staff on behalf of client.
    """

    class Status(models.TextChoices):
        SUBMITTED = 'submitted', 'Submitted'
        IN_TRIAGE = 'in_triage', 'In Triage'
        QUOTED = 'quoted', 'Quoted'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    class RequestType(models.TextChoices):
        CONTENT_UPDATE = 'content_update', 'Content Update'
        DESIGN_CHANGE = 'design_change', 'Design Change'
        NEW_FEATURE = 'new_feature', 'New Feature'
        BUG_FIX = 'bug_fix', 'Bug Fix'
        PACKAGE_UPGRADE = 'package_upgrade', 'Package Upgrade'
        SEO = 'seo', 'SEO'
        MARKETING = 'marketing', 'Marketing'
        OTHER = 'other', 'Other'

    project = models.ForeignKey(
        'Project', on_delete=models.CASCADE, related_name='change_requests'
    )
    request_type = models.CharField(max_length=30, choices=RequestType.choices)
    title = models.CharField(max_length=200)
    description = models.TextField()

    requested_features = models.JSONField(
        default=list,
        blank=True,
        help_text='List of feature keys from PackageScope requested',
    )
    scope_verdict = models.CharField(
        max_length=20,
        blank=True,
        help_text='in_scope | partial | out_of_scope — set during triage',
    )

    quoted_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    quote_notes = models.TextField(blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SUBMITTED,
        db_index=True,
    )

    submitted_via_portal = models.BooleanField(default=False)
    submitted_by_staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='staff_submitted_requests',
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_change_requests',
    )

    resolution_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.project} — {self.title}'


# ─── Billing & Accounts (admin-only CRM module) ─────────────────────────────


class BillSequence(models.Model):
    """Atomic bill number sequence per Indian financial year (Apr–Mar)."""

    fiscal_year = models.CharField(max_length=4, unique=True, db_index=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name_plural = 'Bill sequences'

    def __str__(self):
        return f'{self.fiscal_year} → {self.last_number}'


class Bill(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ISSUED = 'issued', 'Issued'
        PARTIALLY_PAID = 'partially_paid', 'Partially Paid'
        PAID = 'paid', 'Paid'
        CANCELLED = 'cancelled', 'Cancelled'

    class Kind(models.TextChoices):
        INVOICE = 'invoice', 'Tax Invoice'
        RECEIPT = 'receipt', 'Payment Receipt'

    bill_number = models.CharField(max_length=32, unique=True, db_index=True)
    kind = models.CharField(
        max_length=10,
        choices=Kind.choices,
        default=Kind.RECEIPT,
        db_index=True,
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='bills',
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name='bills',
    )
    bill_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    description = models.TextField(
        blank=True,
        help_text='Notes printed on the bill (payment terms, scope, etc.)',
    )
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    gst_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0'),
        help_text='GST rate applied to subtotal (0 for non-GST bills)',
    )
    gst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    balance_due = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    pdf_file = models.FileField(upload_to='bills/pdf/', blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_sent_to = models.EmailField(blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_bills',
    )

    class Meta:
        ordering = ['-bill_date', '-created_at']

    def __str__(self):
        return self.bill_number

    def recalculate_totals(self):
        lines = self.line_items.all()
        self.subtotal = sum((ln.amount for ln in lines), Decimal('0'))
        self.gst_amount = (self.subtotal * self.gst_percent / Decimal('100')).quantize(
            Decimal('0.01')
        )
        self.total_amount = self.subtotal + self.gst_amount
        verified_paid = self.payments.filter(
            status=BillPayment.Status.VERIFIED
        ).aggregate(total=models.Sum('amount'))['total'] or Decimal('0')
        self.amount_paid = verified_paid
        self.balance_due = self.total_amount - self.amount_paid
        if self.status not in (self.Status.CANCELLED, self.Status.DRAFT):
            if self.balance_due <= 0 and self.total_amount > 0:
                self.status = self.Status.PAID
            elif self.amount_paid > 0:
                self.status = self.Status.PARTIALLY_PAID
            elif self.amount_paid == 0 and self.status in (
                self.Status.PARTIALLY_PAID,
                self.Status.PAID,
            ):
                self.status = self.Status.ISSUED
        return self


class BillLineItem(models.Model):
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name='line_items')
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1'))
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'pk']

    def save(self, *args, **kwargs):
        self.amount = (self.quantity * self.unit_price).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)

    def __str__(self):
        return self.description[:60]


class BillPayment(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending Verification'
        VERIFIED = 'verified', 'Verified'

    class PaymentMethod(models.TextChoices):
        BANK_TRANSFER = 'bank_transfer', 'Bank Transfer'
        UPI = 'upi', 'UPI'
        CHEQUE = 'cheque', 'Cheque'
        CASH = 'cash', 'Cash'
        OTHER = 'other', 'Other'

    bill = models.ForeignKey(
        Bill,
        on_delete=models.CASCADE,
        related_name='payments',
        null=True,
        blank=True,
        help_text='Optional — payment can apply to project without a specific bill',
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='bill_payments',
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    transaction_id = models.CharField(max_length=120, blank=True)
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.BANK_TRANSFER,
    )
    payment_date = models.DateField(default=timezone.localdate)
    proof_file = models.FileField(
        upload_to='bills/payments/',
        blank=True,
        help_text='Screenshot or PDF of bank/UPI transaction',
    )
    notes = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='recorded_bill_payments',
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='verified_bill_payments',
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date', '-created_at']

    def __str__(self):
        return f'{self.amount} — {self.project_id}'


class LedgerEntry(models.Model):
    class EntryType(models.TextChoices):
        DEBIT = 'debit', 'Debit'
        CREDIT = 'credit', 'Credit'

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='ledger_entries',
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name='ledger_entries',
    )
    entry_type = models.CharField(max_length=10, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text='Running balance (debits − credits) for this project after this entry',
    )
    bill = models.ForeignKey(
        Bill,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ledger_entries',
    )
    payment = models.ForeignKey(
        BillPayment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ledger_entries',
    )
    reference = models.CharField(max_length=120, blank=True)
    description = models.CharField(max_length=300)
    entry_date = models.DateField(default=timezone.localdate, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='ledger_entries_created',
    )

    class Meta:
        ordering = ['entry_date', 'created_at', 'pk']
        verbose_name_plural = 'Ledger entries'

    def __str__(self):
        return f'{self.entry_type} {self.amount} — {self.reference}'
