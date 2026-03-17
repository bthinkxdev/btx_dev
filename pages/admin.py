from django.contrib import admin
from django.utils.html import format_html

from .models import (
    BlogPageSettings,
    BlogPost,
    ContactSubmission,
    NewsletterSubscriber,
    Project,
    TeamMember,
    TeamSection,
    TechTag,
)


@admin.register(ContactSubmission)
class ContactSubmissionAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'project_type', 'created_at')
    list_filter = ('project_type', 'created_at')
    search_fields = ('name', 'email', 'company', 'message')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'


@admin.register(TechTag)
class TechTagAdmin(admin.ModelAdmin):
    search_fields = ('name',)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'image_thumb',
        'category',
        'year',
        'is_featured',
        'show_on_homepage',
        'sort_order',
    )
    list_filter = ('category', 'is_featured', 'show_on_homepage', 'year')
    search_fields = ('title', 'short_description')
    list_editable = ('is_featured', 'show_on_homepage', 'sort_order')
    readonly_fields = ('image_preview',)
    prepopulated_fields = {'slug': ('title',)}
    filter_horizontal = ('stack',)

    @admin.display(description='Image')
    def image_thumb(self, obj):
        if obj.pk and obj.image:
            return format_html(
                '<img src="{}" alt="" style="max-height:48px;border-radius:6px"/>',
                obj.image.url,
            )
        return '—'

    @admin.display(description='Current image')
    def image_preview(self, obj):
        if obj.pk and obj.image:
            return format_html(
                '<img src="{}" alt="" style="max-width:400px;border-radius:8px;border:1px solid #333"/>',
                obj.image.url,
            )
        return 'Upload an image below.'


@admin.register(TeamSection)
class TeamSectionAdmin(admin.ModelAdmin):
    list_display = ('heading', 'badge')

    def has_add_permission(self, request):
        return TeamSection.objects.count() == 0

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TeamMember)
class TeamMemberAdmin(admin.ModelAdmin):
    list_display = ('title', 'role', 'is_active', 'sort_order', 'photo_thumb')
    list_filter = ('is_active',)
    list_editable = ('is_active', 'sort_order')
    search_fields = ('title', 'role', 'bio')
    readonly_fields = ('photo_preview',)

    @admin.display(description='Photo')
    def photo_thumb(self, obj):
        if obj.pk and obj.photo:
            return format_html(
                '<img src="{}" alt="" style="max-height:40px;border-radius:6px"/>',
                obj.photo.url,
            )
        return '—'

    @admin.display(description='Current photo')
    def photo_preview(self, obj):
        if obj.pk and obj.photo:
            return format_html(
                '<img src="{}" alt="" style="max-width:280px;border-radius:8px;border:1px solid #333"/>',
                obj.photo.url,
            )
        return 'Upload a photo below.'


@admin.register(BlogPageSettings)
class BlogPageSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return BlogPageSettings.objects.count() == 0

    def has_delete_permission(self, request, obj=None):
        return False


@admin.action(description='Reset newsletter state (unpublish & publish again to resend)')
def reset_blog_newsletter_notifications(modeladmin, request, queryset):
    queryset.update(
        notification_job_started_at=None,
        subscriber_notification_completed_at=None,
    )


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'category',
        'published_at',
        'is_featured',
        'is_published',
        'newsletter_status',
        'image_thumb',
    )
    list_filter = ('is_published', 'is_featured', 'category', 'published_at')
    list_editable = ('is_featured', 'is_published')
    search_fields = ('title', 'excerpt', 'body')
    prepopulated_fields = {'slug': ('title',)}
    date_hierarchy = 'published_at'
    actions = [reset_blog_newsletter_notifications]

    fieldsets = (
        (None, {'fields': ('title', 'slug', 'category', 'category_slug')}),
        ('Content', {'fields': ('excerpt', 'body', 'featured_image', 'image_preview')}),
        ('Publishing', {'fields': ('published_at', 'read_time_minutes', 'is_featured', 'is_published')}),
        (
            'Newsletter',
            {
                'fields': (
                    'notification_job_started_at',
                    'subscriber_notification_completed_at',
                ),
                'classes': ('collapse',),
                'description': 'Subscribers are emailed when a post is first published. Reset via admin action to allow resend after unpublish/republish.',
            },
        ),
        ('SEO', {'fields': ('meta_description',), 'classes': ('collapse',)}),
        ('Meta', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )
    readonly_fields = (
        'category_slug',
        'image_preview',
        'created_at',
        'updated_at',
        'notification_job_started_at',
        'subscriber_notification_completed_at',
    )

    @admin.display(description='Newsletter')
    def newsletter_status(self, obj):
        if not obj.pk:
            return '—'
        if obj.subscriber_notification_completed_at:
            return format_html('<span style="color:green">Sent</span>')
        if obj.notification_job_started_at:
            return format_html('<span style="color:orange">Sending…</span>')
        if obj.is_published:
            return 'Pending'
        return '—'

    @admin.display(description='Image')
    def image_thumb(self, obj):
        if obj.pk and obj.featured_image:
            return format_html(
                '<img src="{}" alt="" style="max-height:40px;border-radius:6px"/>',
                obj.featured_image.url,
            )
        return '—'

    @admin.display(description='Featured image preview')
    def image_preview(self, obj):
        if obj.pk and obj.featured_image:
            return format_html(
                '<img src="{}" alt="" style="max-width:400px;border-radius:8px;border:1px solid #333"/>',
                obj.featured_image.url,
            )
        return 'Upload below.'


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ('email', 'is_active', 'source', 'subscribed_at')
    list_filter = ('is_active', 'source')
    search_fields = ('email',)
    readonly_fields = ('unsubscribe_token', 'subscribed_at')
    ordering = ('-subscribed_at',)
