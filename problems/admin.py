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

logger = logging.getLogger(__name__)


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
            return JsonResponse({'ok': True, 'done': True, 'remaining': 0})
        problem_id = ids[0]
        problem = Problem.objects.filter(pk=problem_id).first()
        if problem is None:
            ids.pop(0)
            request.session['tag_complete_ids'] = ids
            request.session.modified = True
            return JsonResponse({
                'ok': False,
                'done': not ids,
                'remaining': len(ids),
                'error': f'题目 P{problem_id} 不存在。',
                'problem_id': problem_id,
            })
        vocab = request.session.get('tag_complete_vocab') or collect_vocabulary()
        system_prompt = request.session.get('tag_complete_system_prompt') or ''
        user_prompt = request.session.get('tag_complete_user_prompt') or ''
        try:
            result = complete_one_problem(
                problem, vocab, api_key=api_key,
                system_prompt=system_prompt,
                user_prompt_template=user_prompt,
            )
        except DeepSeekError as exc:
            logger.warning('DeepSeek tag complete failed for P%s', problem_id)
            return JsonResponse({
                'ok': False,
                'done': False,
                'remaining': len(ids),
                'problem_id': problem_id,
                'title': problem.title,
                'error': exc.user_message,
            })
        ids.pop(0)
        request.session['tag_complete_ids'] = ids
        request.session.modified = True
        result['done'] = not ids
        result['remaining'] = len(ids)
        if result['done']:
            request.session.pop('tag_complete_api_key', None)
            request.session.pop('tag_complete_vocab', None)
            request.session.modified = True
        return JsonResponse(result)


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
