import logging

from django.contrib import admin, messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import path

from ai_assistant.deepseek_api import DeepSeekError

from .forms import save_test_cases
from .luogu import LuoguFetchError, fetch_luogu_problem, normalize_pid
from .models import Problem, TestCase, Solution
from .tag_complete import (
    MAX_BATCH,
    collect_vocabulary,
    complete_one_problem,
    count_incomplete_problems,
    incomplete_problems,
    load_saved_prompts,
    save_prompts,
)
from .tag_labels import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT
from .tag_complete import (
    MAX_BATCH,
    collect_vocabulary,
    complete_problems_in_parallel,   # 新增
    count_incomplete_problems,
    incomplete_problems,
    load_saved_prompts,
    save_prompts,
)

TAG_STEP_BATCH = 10

logger = logging.getLogger(__name__)


class TestCaseInline(admin.TabularInline):
    model = TestCase
    extra = 3


@admin.register(Problem)
class ProblemAdmin(admin.ModelAdmin):
    list_display = ['id', 'title', 'luogu_pid', 'problem_type', 'difficulty', 'created_by', 'is_public', 'created_at']
    list_filter = ['problem_type', 'difficulty', 'is_public', 'created_at']
    search_fields = ['title', 'description', 'tags', 'luogu_pid']
    readonly_fields = ['luogu_pid', 'created_at', 'updated_at']
    fieldsets = (
        (None, {
            'fields': ('title', 'description', 'input_format', 'output_format',
                       'sample_input', 'sample_output', 'hint', 'tags',
                       'luogu_pid'),
        }),
        ('题目类型', {
            'fields': ('problem_type', 'function_files', 'interactive_config'),
            'description': '函数题（IOI 风格）请把 problem_type 设为 function，并在 function_files '
                           '填 JSON 数组，例如 [{"name": "problem.h", "content": "..."}, '
                           '{"name": "grader.cpp", "content": "..."}]。'
                           '交互题（IOI 风格）把 problem_type 设为 interactive，function_files 放 '
                           'manager/stub 等文件，interactive_config 填 JSON 对象，例如 '
                           '{"num_processes": 1, "user_io": "fifo_io"}。标准题忽略这两个字段。',
        }),
        ('评测限制', {
            'fields': ('difficulty', 'time_limit', 'memory_limit', 'is_public'),
        }),
        ('元数据', {
            'fields': ('created_by', 'created_at', 'updated_at'),
        }),
    )
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
            path(
                'complete-tags/',
                self.admin_site.admin_view(self.complete_tags_view),
                name='problems_problem_complete_tags',
            ),
            path(
                'complete-tags/start/',
                self.admin_site.admin_view(self.complete_tags_start),
                name='problems_problem_complete_tags_start',
            ),
            path(
                'complete-tags/step/',
                self.admin_site.admin_view(self.complete_tags_step),
                name='problems_problem_complete_tags_step',
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

    def complete_tags_view(self, request):
        incomplete = count_incomplete_problems()
        vocab = collect_vocabulary()
        prompts = load_saved_prompts()
        context = {
            **self.admin_site.each_context(request),
            'title': 'AI 完善题目标签',
            'opts': self.model._meta,
            'incomplete_count': incomplete,
            'vocab_size': len(vocab),
            'vocab_preview': vocab[:40],
            'max_batch': MAX_BATCH,
            'default_count': min(20, incomplete or 1),
            'system_prompt': prompts['system'],
            'user_prompt': prompts['user'],
            'default_system_prompt': DEFAULT_SYSTEM_PROMPT,
            'default_user_prompt': DEFAULT_USER_PROMPT,
        }
        return render(request, 'admin/problems/complete_tags.html', context)

    def complete_tags_start(self, request):
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'error': '请使用 POST。'}, status=405)
        api_key = (request.POST.get('api_key') or '').strip()
        raw_count = (request.POST.get('count') or '').strip()
        if not api_key:
            return JsonResponse({'ok': False, 'error': '请填写 DeepSeek API Key。'}, status=400)
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'error': '完善数量必须是正整数。'}, status=400)
        if count < 1 or count > MAX_BATCH:
            return JsonResponse(
                {'ok': False, 'error': f'完善数量须在 1～{MAX_BATCH} 之间。'},
                status=400,
            )
        vocab = collect_vocabulary()
        prompts = save_prompts(
            request.POST.get('system_prompt') or '',
            request.POST.get('user_prompt') or '',
        )
        selected = incomplete_problems(limit=count)
        if not selected:
            return JsonResponse({'ok': False, 'error': '没有需要完善标签的题目。'}, status=400)
        request.session['tag_complete_api_key'] = api_key
        request.session['tag_complete_ids'] = [p.id for p in selected]
        request.session['tag_complete_vocab'] = vocab
        request.session['tag_complete_system_prompt'] = prompts['system']
        request.session['tag_complete_user_prompt'] = prompts['user']
        request.session['tag_complete_total'] = len(selected)
        request.session.modified = True
        return JsonResponse({
            'ok': True,
            'total': len(selected),
            'vocab_size': len(vocab),
            'ids': [p.id for p in selected],
        })

    def complete_tags_step(self, request):
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'error': '请使用 POST。'}, status=405)

        api_key = request.session.get('tag_complete_api_key') or ''
        ids = list(request.session.get('tag_complete_ids') or [])

        if not api_key:
            return JsonResponse(
                {'ok': False, 'error': '会话已过期，请重新填写 API Key。'},
                status=400,
            )
        if not ids:
            request.session.pop('tag_complete_api_key', None)
            request.session.modified = True
            return JsonResponse(
                {'ok': True, 'done': True, 'remaining': 0, 'results': []}
            )

        batch_ids = ids[:TAG_STEP_BATCH]
        rest_ids = ids[TAG_STEP_BATCH:]

        # 保持原有顺序，同时容忍中途被删掉的题
        problem_map = Problem.objects.in_bulk(batch_ids)
        problems: list[Problem] = []
        missing: list[int] = []
        for pid in batch_ids:
            problem = problem_map.get(pid)
            if problem is None:
                missing.append(pid)
            else:
                problems.append(problem)

        vocab = request.session.get('tag_complete_vocab') or collect_vocabulary()
        system_prompt = request.session.get('tag_complete_system_prompt') or ''
        user_prompt = request.session.get('tag_complete_user_prompt') or ''

        results: list[dict] = []
        if problems:
            # 单题内部已用 try/finally 关闭线程本地连接；
            # 这里的 concurrency 与 batch_size 保持一致，避免空闲线程。
            results = complete_problems_in_parallel(
                problems,
                vocab,
                api_key=api_key,
                system_prompt=system_prompt,
                user_prompt_template=user_prompt,
                concurrency=min(TAG_STEP_BATCH, len(problems)),
            )

        for pid in missing:
            results.append({
                'ok': False,
                'problem_id': pid,
                'title': '',
                'error': f'题目 P{pid} 不存在。',
                'before': '',
                'after': '',
                'added': [],
            })

        # 并行返回乱序，按提交顺序排回去，方便前端打印
        order = {pid: i for i, pid in enumerate(batch_ids)}
        results.sort(key=lambda r: order.get(r.get('problem_id'), 0))

        request.session['tag_complete_ids'] = rest_ids
        request.session.modified = True

        done = not rest_ids
        if done:
            request.session.pop('tag_complete_api_key', None)
            request.session.pop('tag_complete_vocab', None)
            request.session.modified = True

        return JsonResponse({
            'ok': True,
            'done': done,
            'remaining': len(rest_ids),
            'results': results,
        })


@admin.register(TestCase)
class TestCaseAdmin(admin.ModelAdmin):
    list_display = ['id', 'problem', 'order', 'is_sample']
    list_filter = ['problem']
    search_fields = ['id', 'problem__title']

    # The big text columns are not shown anywhere in the admin list/autocomplete;
    # loading them for the whole table costs ~1.2 GB per page render.
    def get_queryset(self, request):
        return super().get_queryset(request).defer('input_data', 'expected_output')


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
