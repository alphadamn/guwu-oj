from django.contrib import admin, messages
from django.shortcuts import redirect, render
from django.urls import path

from .forms import save_test_cases
from .luogu import LuoguFetchError, fetch_luogu_problem, normalize_pid
from .models import Problem, TestCase, Solution


class TestCaseInline(admin.TabularInline):
    model = TestCase
    extra = 3


@admin.register(Problem)
class ProblemAdmin(admin.ModelAdmin):
    list_display = ['id', 'title', 'luogu_pid', 'difficulty', 'created_by', 'is_public', 'created_at']
    list_filter = ['difficulty', 'is_public', 'created_at']
    search_fields = ['title', 'description', 'tags', 'luogu_pid']
    readonly_fields = ['luogu_pid']
    inlines = [TestCaseInline]
    change_list_template = 'admin/problems/problem/change_list.html'

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                'import-luogu/',
                self.admin_site.admin_view(self.import_luogu_view),
                name='problems_problem_import_luogu',
            ),
        ]
        return custom + urls

    def import_luogu_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            'title': '从洛谷导入题目',
            'opts': self.model._meta,
        }

        if request.method == 'POST':
            raw_pid = request.POST.get('luogu_pid', '').strip()
            try:
                pid = normalize_pid(raw_pid)
                existing = Problem.objects.filter(luogu_pid=pid).first()
                if existing:
                    messages.warning(
                        request,
                        f'洛谷 {pid} 已导入为 P{existing.id}，请勿重复导入。',
                    )
                    return redirect('admin:problems_problem_change', existing.pk)

                data = fetch_luogu_problem(pid)
                problem = Problem.objects.create(
                    title=data['title'],
                    description=data['description'],
                    input_format=data['input_format'],
                    output_format=data['output_format'],
                    sample_input=data['sample_input'],
                    sample_output=data['sample_output'],
                    hint=data['hint'],
                    difficulty=data['difficulty'],
                    time_limit=data['time_limit'],
                    memory_limit=data['memory_limit'],
                    tags=data['tags'],
                    luogu_pid=data['luogu_pid'],
                    created_by=request.user,
                    is_public=False,
                )
                save_test_cases(problem, data['test_cases'])
                messages.success(
                    request,
                    f'已从洛谷导入 {data["luogu_pid"]}，本地题号为 P{problem.id}。'
                    f'测试用例 {len(data["test_cases"])} 个（样例不足时已用样例补齐）。',
                )
                return redirect('admin:problems_problem_change', problem.pk)
            except LuoguFetchError as exc:
                messages.error(request, str(exc))
            except Exception as exc:
                messages.error(request, f'导入失败: {exc}')

        return render(request, 'admin/problems/import_luogu.html', context)


@admin.register(TestCase)
class TestCaseAdmin(admin.ModelAdmin):
    list_display = ['id', 'problem', 'order', 'is_sample']
    list_filter = ['problem']


@admin.register(Solution)
class SolutionAdmin(admin.ModelAdmin):
    list_display = ['id', 'title', 'problem', 'author', 'is_approved', 'like_count', 'created_at']
    list_filter = ['is_approved', 'created_at', 'problem']
    search_fields = ['title', 'content', 'author__username', 'problem__title']
    readonly_fields = ['like_count', 'created_at', 'updated_at']
    actions = ['approve_solutions', 'unapprove_solutions']
    
    def like_count(self, obj):
        return obj.like_count
    like_count.short_description = '点赞数'
    
    def approve_solutions(self, request, queryset):
        updated = queryset.update(is_approved=True)
        self.message_user(request, f'成功审核 {updated} 个题解。')
    approve_solutions.short_description = '审核选中的题解'
    
    def unapprove_solutions(self, request, queryset):
        updated = queryset.update(is_approved=False)
        self.message_user(request, f'成功取消审核 {updated} 个题解。')
    unapprove_solutions.short_description = '取消审核选中的题解'
