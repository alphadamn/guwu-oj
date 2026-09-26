from django.contrib import admin

from .models import Ticket


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ('id', 'subject', 'category', 'status', 'submitter',
                    'email_sent', 'created_at')
    list_display_links = ('id', 'subject')
    list_filter = ('status', 'category')
    search_fields = ('subject', 'body', 'submitter__username', 'contact_email')
    list_select_related = ('submitter',)
    date_hierarchy = 'created_at'
    readonly_fields = ('submitter', 'contact_email', 'email_sent', 'email_error',
                       'created_at', 'updated_at')
    actions = ('mark_processing', 'mark_resolved', 'mark_closed')

    fieldsets = (
        ('工单内容', {'fields': ('subject', 'category', 'status', 'body')}),
        ('提交信息', {'fields': ('submitter', 'contact_email', 'created_at', 'updated_at')}),
        ('处理', {'fields': ('admin_note',)}),
        ('邮件通知', {'fields': ('email_sent', 'email_error')}),
    )

    @admin.action(description='标记为处理中')
    def mark_processing(self, request, queryset):
        queryset.update(status=Ticket.Status.PROCESSING)

    @admin.action(description='标记为已解决')
    def mark_resolved(self, request, queryset):
        queryset.update(status=Ticket.Status.RESOLVED)

    @admin.action(description='标记为已关闭')
    def mark_closed(self, request, queryset):
        queryset.update(status=Ticket.Status.CLOSED)
