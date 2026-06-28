from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from problems.markdown_utils import render_markdown

from . import github, scanner
from .models import DevLogEntry, FileChange, ServiceComponent


def _refresh_auto_components():
    """Update statuses of components flagged for automatic health checking."""
    auto = list(ServiceComponent.objects.filter(auto_check=True))
    if not auto:
        return
    checks = {}
    try:
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        checks['database'] = True
    except Exception:
        checks['database'] = False
    try:
        from django.core.cache import cache
        cache.set('devlog_health_probe', '1', 10)
        checks['cache'] = cache.get('devlog_health_probe') == '1'
        checks['redis'] = checks['cache']
    except Exception:
        checks['cache'] = False
        checks['redis'] = False

    for comp in auto:
        key = comp.health_key or comp.name_en.lower() or comp.name.lower()
        ok = checks.get(key)
        if ok is None:
            continue
        new_status = (
            ServiceComponent.STATUS_OPERATIONAL if ok
            else ServiceComponent.STATUS_MAJOR
        )
        if comp.status != new_status:
            comp.status = new_status
            comp.save(update_fields=['status', 'updated_at'])


def status_page(request):
    _refresh_auto_components()

    components = list(ServiceComponent.objects.all())
    overall = max((c.severity for c in components), default=0)
    overall_status = {
        0: ('all_operational', '所有系统正常', 'success'),
        1: ('maintenance', '维护进行中', 'info'),
        2: ('degraded', '部分系统性能下降', 'warning'),
        3: ('partial', '部分系统中断', 'warning'),
        4: ('major', '重大服务中断', 'danger'),
    }[overall]

    entries = []
    for entry in DevLogEntry.objects.select_related('author')[:30]:
        entries.append({'obj': entry, 'html': render_markdown(entry.body)})

    commits = github.get_commits(limit=15)

    file_changes = FileChange.objects.select_related('annotated_by')[:40]

    context = {
        'components': components,
        'overall_label': overall_status[1],
        'overall_class': overall_status[2],
        'overall_key': overall_status[0],
        'entries': entries,
        'commits': commits,
        'commits_page_url': github.COMMITS_PAGE_URL,
        'file_changes': file_changes,
        'last_scan': FileChange.objects.order_by('-detected_at').values_list('detected_at', flat=True).first(),
        'now': timezone.now(),
        'can_manage': request.user.is_authenticated and request.user.is_staff,
    }
    return render(request, 'devlog/status.html', context)


def _is_staff(user):
    return user.is_authenticated and user.is_staff


@require_POST
@user_passes_test(_is_staff)
def add_entry(request):
    title = (request.POST.get('title') or '').strip()
    if not title:
        messages.error(request, '标题不能为空')
        return redirect('devlog:status')
    DevLogEntry.objects.create(
        title=title,
        version=(request.POST.get('version') or '').strip(),
        body=(request.POST.get('body') or '').strip(),
        pinned=bool(request.POST.get('pinned')),
        author=request.user,
    )
    messages.success(request, '已添加开发者日志')
    return redirect('devlog:status')


@require_POST
@user_passes_test(_is_staff)
def rescan(request):
    result = scanner.scan()
    if result['first_run']:
        messages.success(
            request,
            f"已建立基线快照，共 {result['total_tracked']} 个文件（首次扫描不记录变更）。",
        )
    else:
        messages.success(
            request,
            f"扫描完成：新增 {result['added']} · 修改 {result['modified']} · 删除 {result['deleted']}。",
        )
    return redirect('devlog:status')


@require_POST
@login_required
def annotate_change(request, pk):
    if not request.user.is_staff:
        messages.error(request, '没有权限')
        return redirect('devlog:status')
    change = get_object_or_404(FileChange, pk=pk)
    change.remarks = (request.POST.get('remarks') or '').strip()[:255]
    change.description = (request.POST.get('description') or '').strip()
    change.annotated_by = request.user
    change.save(update_fields=['remarks', 'description', 'annotated_by'])
    messages.success(request, f'已更新备注：{change.path}')
    return redirect('devlog:status')
