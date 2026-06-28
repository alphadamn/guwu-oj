from django.contrib import admin

from .models import DevLogEntry, FileChange, FileSnapshot, ServiceComponent


@admin.register(ServiceComponent)
class ServiceComponentAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'uptime', 'order', 'auto_check', 'updated_at')
    list_editable = ('status', 'uptime', 'order', 'auto_check')
    list_filter = ('status', 'auto_check')
    search_fields = ('name', 'name_en')


@admin.register(DevLogEntry)
class DevLogEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'version', 'pinned', 'author', 'created_at')
    list_filter = ('pinned', 'created_at')
    search_fields = ('title', 'version', 'body')
    autocomplete_fields = ('author',)


@admin.register(FileChange)
class FileChangeAdmin(admin.ModelAdmin):
    list_display = ('path', 'change_type', 'detected_at', 'remarks', 'annotated_by')
    list_filter = ('change_type', 'detected_at')
    search_fields = ('path', 'remarks', 'description')
    readonly_fields = ('path', 'change_type', 'file_hash', 'old_hash', 'size', 'detected_at')


@admin.register(FileSnapshot)
class FileSnapshotAdmin(admin.ModelAdmin):
    list_display = ('path', 'size', 'updated_at')
    search_fields = ('path',)
