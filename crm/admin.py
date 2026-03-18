from django.contrib import admin

from .models import ActivityLog, EmployeeProfile, FollowUp, Lead, Package, Task


@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'target_amount')
    search_fields = ('user__username', 'user__email')


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ('name', 'price', 'employee')
    list_filter = ('employee',)


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


@admin.register(FollowUp)
class FollowUpAdmin(admin.ModelAdmin):
    list_display = ('lead', 'employee', 'datetime', 'is_done')
    list_filter = ('is_done', 'employee')


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'lead', 'employee', 'due_date', 'is_completed')
