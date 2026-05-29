from django.contrib import admin

from .models import (
    Achievement,
    ActivityLog,
    AuditEntry,
    Bill,
    BillLineItem,
    BillPayment,
    BillSequence,
    ChangeRequest,
    Client,
    CredentialAuditLog,
    EmployeeProfile,
    FollowUp,
    HandoverPortalAccess,
    LedgerEntry,
    Lead,
    MonthlyTarget,
    OnboardingSubmission,
    Package,
    PackageScope,
    Project,
    ProjectCredential,
    ProjectMember,
    ProjectTicket,
    ProjectTicketAttachment,
    ProjectTicketLink,
    ProjectHandover,
    ProjectProvisioning,
    ProvisioningStep,
    RenewalReminderLog,
    RenewalTracker,
    Task,
    WhatsAppBotExcludePhone,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppNumber,
)


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = (
        'business_name',
        'contact_person',
        'phone',
        'email',
        'lead',
        'created_at',
        'created_by',
    )
    list_filter = ('created_at', 'created_by')
    search_fields = ('business_name', 'contact_person', 'phone', 'email', 'gst_number', 'pan_number')


class ProjectMemberInline(admin.TabularInline):
    model = ProjectMember
    extra = 0
    autocomplete_fields = ('user',)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    inlines = [ProjectMemberInline]
    list_display = (
        'id',
        'client',
        'package',
        'deal_value',
        'advance_received',
        'balance_due',
        'status',
        'assigned_to',
        'created_at',
        'created_by',
    )
    list_filter = ('status', 'package', 'created_at', 'assigned_to')
    search_fields = (
        'client__business_name',
        'client__phone',
        'client__email',
        'notes',
    )


class ProjectTicketLinkInline(admin.TabularInline):
    model = ProjectTicketLink
    extra = 0


@admin.register(ProjectTicket)
class ProjectTicketAdmin(admin.ModelAdmin):
    list_display = ('title', 'project', 'status', 'priority', 'assigned_to', 'updated_at')
    list_filter = ('status', 'priority', 'project')
    search_fields = ('title', 'description', 'project__client__business_name')
    inlines = [ProjectTicketLinkInline]


@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'crm_role', 'whatsapp_bot_enabled', 'target_amount', 'has_profile_photo')
    search_fields = ('user__username', 'user__email')
    fields = ('user', 'crm_role', 'whatsapp_bot_enabled', 'target_amount', 'photo')

    @admin.display(description='Photo', boolean=True)
    def has_profile_photo(self, obj):
        return bool(obj.photo)


class PackageScopeInline(admin.StackedInline):
    model = PackageScope
    extra = 0


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ('name', 'price', 'employee')
    list_filter = ('employee',)
    inlines = [PackageScopeInline]


class ActivityInline(admin.TabularInline):
    model = ActivityLog
    extra = 0
    readonly_fields = ('action', 'note', 'created_at')
    can_delete = False


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ('name', 'employee', 'status', 'phone', 'next_followup')
    list_filter = ('status', 'employee')
    search_fields = ('name', 'phone', 'email')
    inlines = [ActivityInline]


@admin.register(WhatsAppNumber)
class WhatsAppNumberAdmin(admin.ModelAdmin):
    list_display = (
        'display_phone_number',
        'phone_number_id',
        'executive',
        'is_active',
        'updated_at',
    )
    list_filter = ('is_active', 'executive')
    search_fields = ('display_phone_number', 'phone_number_id', 'executive__username', 'executive__email')


@admin.register(WhatsAppBotExcludePhone)
class WhatsAppBotExcludePhoneAdmin(admin.ModelAdmin):
    list_display = ('phone', 'label', 'executive', 'created_at')
    list_filter = ('executive',)
    search_fields = ('phone', 'label', 'executive__username')


@admin.register(WhatsAppConversation)
class WhatsAppConversationAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'wa_number',
        'executive',
        'customer_phone',
        'lead',
        'bot_enabled',
        'human_takeover_at',
        'updated_at',
    )
    list_filter = ('bot_enabled', 'executive', 'wa_number')
    search_fields = ('customer_phone', 'lead__phone', 'lead__name')


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'created_at',
        'direction',
        'status',
        'source',
        'wa_number',
        'executive',
        'customer_phone',
        'message_id',
    )
    list_filter = ('direction', 'status', 'source', 'wa_number', 'executive')
    search_fields = ('message_id', 'customer_phone', 'text', 'lead__name', 'lead__phone')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(FollowUp)
class FollowUpAdmin(admin.ModelAdmin):
    list_display = ('lead', 'employee', 'datetime', 'is_done')
    list_filter = ('is_done', 'employee')


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'lead', 'employee', 'due_date', 'is_completed')


@admin.register(Achievement)
class AchievementAdmin(admin.ModelAdmin):
    list_display = (
        'employee',
        'amount',
        'achieved_date',
        'package',
        'lead',
        'created_by',
        'created_at',
    )
    list_filter = ('employee', 'package', 'achieved_date')
    search_fields = ('lead__name', 'employee__username', 'employee__email')


@admin.register(MonthlyTarget)
class MonthlyTargetAdmin(admin.ModelAdmin):
    list_display = ('employee', 'month', 'target_amount')
    list_filter = ('employee', 'month')


class ProvisioningStepInline(admin.TabularInline):
    model = ProvisioningStep
    extra = 0
    readonly_fields = ('step_key', 'domain', 'created_at', 'updated_at')


@admin.register(ProjectProvisioning)
class ProjectProvisioningAdmin(admin.ModelAdmin):
    list_display = ('project', 'assigned_to', 'updated_at')
    search_fields = ('project__client__business_name',)
    inlines = [ProvisioningStepInline]


@admin.register(ProjectCredential)
class ProjectCredentialAdmin(admin.ModelAdmin):
    list_display = ('label', 'project', 'category', 'credential_type', 'visibility_level', 'is_client_visible', 'updated_at')
    list_filter = ('category', 'credential_type', 'visibility_level', 'is_client_visible')
    search_fields = ('label', 'project__client__business_name', 'username')
    readonly_fields = ('password_encrypted', 'secret_key_encrypted', 'created_at', 'updated_at')


@admin.register(CredentialAuditLog)
class CredentialAuditLogAdmin(admin.ModelAdmin):
    list_display = ('action', 'credential', 'user', 'timestamp', 'ip')
    list_filter = ('action',)
    search_fields = ('credential__label', 'user__username')


@admin.register(ProjectHandover)
class ProjectHandoverAdmin(admin.ModelAdmin):
    list_display = ('project', 'completed_at', 'client_notified', 'tenant_visibility_enabled')
    search_fields = ('project__client__business_name',)


@admin.register(RenewalTracker)
class RenewalTrackerAdmin(admin.ModelAdmin):
    list_display = ('title', 'project', 'subject_type', 'expires_at', 'status')
    list_filter = ('subject_type', 'status')
    search_fields = ('title', 'project__client__business_name')


@admin.register(HandoverPortalAccess)
class HandoverPortalAccessAdmin(admin.ModelAdmin):
    list_display = (
        'project',
        'is_active',
        'activated_at',
        'activated_by',
        'last_accessed_at',
        'access_count',
    )
    list_filter = ('is_active',)
    readonly_fields = ('access_token', 'activated_at', 'last_accessed_at', 'access_count')
    search_fields = ('project__client__business_name',)


@admin.register(RenewalReminderLog)
class RenewalReminderLogAdmin(admin.ModelAdmin):
    list_display = ('renewal', 'reminder_type', 'sent_at', 'sent_to', 'success')
    list_filter = ('reminder_type', 'success')
    readonly_fields = ('sent_at',)


@admin.register(OnboardingSubmission)
class OnboardingSubmissionAdmin(admin.ModelAdmin):
    list_display = [
        'project',
        'submitted_at',
        'terms_accepted',
        'business_info_status',
        'documents_status',
        'agreement_status',
    ]
    list_filter = ['terms_accepted', 'business_info_status', 'agreement_status']
    search_fields = ['project__client__business_name']
    readonly_fields = ['submitted_at', 'terms_accepted_at', 'terms_accepted_ip']


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = [
        'created_at',
        'category',
        'action',
        'actor_label',
        'object_repr',
        'project',
    ]
    list_filter = ['category', 'action']
    search_fields = ['actor_label', 'object_repr', 'note']
    readonly_fields = [
        'created_at',
        'actor_label',
        'before_state',
        'after_state',
        'category',
        'action',
        'object_type',
        'object_id',
        'object_repr',
        'actor',
        'project',
        'ip_address',
        'note',
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class BillLineItemInline(admin.TabularInline):
    model = BillLineItem
    extra = 0


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    list_display = (
        'bill_number',
        'client',
        'project',
        'bill_date',
        'total_amount',
        'balance_due',
        'status',
        'email_sent_at',
    )
    list_filter = ('status', 'bill_date')
    search_fields = ('bill_number', 'client__business_name')
    readonly_fields = ('bill_number', 'amount_paid', 'balance_due', 'subtotal', 'gst_amount', 'total_amount')
    inlines = [BillLineItemInline]


@admin.register(BillPayment)
class BillPaymentAdmin(admin.ModelAdmin):
    list_display = (
        'project',
        'bill',
        'amount',
        'payment_date',
        'transaction_id',
        'status',
        'recorded_by',
    )
    list_filter = ('status', 'payment_method')
    search_fields = ('transaction_id', 'project__client__business_name')


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        'entry_date',
        'project',
        'entry_type',
        'amount',
        'balance_after',
        'reference',
    )
    list_filter = ('entry_type',)
    search_fields = ('reference', 'description', 'client__business_name')


@admin.register(BillSequence)
class BillSequenceAdmin(admin.ModelAdmin):
    list_display = ('fiscal_year', 'last_number')


@admin.register(ChangeRequest)
class ChangeRequestAdmin(admin.ModelAdmin):
    list_display = [
        'project',
        'title',
        'request_type',
        'status',
        'scope_verdict',
        'quoted_amount',
        'created_at',
    ]
    list_filter = ['status', 'request_type', 'scope_verdict']
    search_fields = ['title', 'project__client__business_name']
