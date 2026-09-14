import re

from django.conf import settings
from django.db.models.functions import Cast, RowNumber
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q, Count, When, Case, Value, F, FloatField, Window
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from django.core.cache import cache
from .models import Problem, Solution
from .forms import ProblemForm, parse_test_cases_from_post, validate_test_cases, save_test_cases
from .tag_labels import search_aliases
from users.models import User
from submissions.models import Submission

PROBLEMS_PER_PAGE = 20

# Tags are stored as free text, separated by spaces and/or commas
# (ASCII "," and Chinese "，"); user input may also use "、" or ";".
_TERM_SPLIT_RE = re.compile(r'[\s,，、;；]+')


def _split_search_terms(text):
    """Split a search/filter string into fuzzy-match terms."""
    return [term for term in _TERM_SPLIT_RE.split(text or '') if term]


def home(request):
    # Cache recent problems
    recent_problems_cache_key = 'home_recent_problems'
    cached_recent = cache.get(recent_problems_cache_key)
    if cached_recent is not None:
        recent_problems = cached_recent
    else:
        recent_problems = Problem.objects.filter(is_public=True)[:10]
        recent_problems = list(recent_problems)  # Force evaluation
        cache.set(recent_problems_cache_key, recent_problems, 60 * 5)  # Cache for 5 minutes

    # Cache stats
    stats_cache_key = 'home_stats'
    cached_stats = cache.get(stats_cache_key)
    if cached_stats is not None:
        stats = cached_stats
    else:
        public_problems = Problem.objects.filter(is_public=True)
        # AI 判题验证专用账号不计入公开统计（与排行榜口径一致）。
        bot_username = getattr(settings, 'AI_JUDGE_BOT_USERNAME', '__ai_judge_bot__')
        stats = {
            'problem_count': public_problems.count(),
            'submission_count': Submission.objects.exclude(user__username=bot_username).count(),
            'user_count': User.objects.exclude(username=bot_username).count(),
        }
        cache.set(stats_cache_key, stats, 60 * 5)  # Cache for 5 minutes

    return render(request, 'home.html', {
        'recent_problems': recent_problems,
        'stats': stats,
    })


@never_cache  # base.html navbar is user-specific — the rendered page must never be shared
def problem_list(request):
    # NOTE: only the user-independent total COUNT is cached. The full
    # rendered HttpResponse must never be cached: the page extends base.html,
    # whose navbar embeds the current visitor's username / profile link /
    # logout CSRF / messages. A shared response cache would serve one
    # visitor's account chrome to everyone else.
    #
    # Never materialize/cache the whole result set: with 20k+ rows, each
    # Problem carries multi-KB statement TextFields (description, I/O format,
    # samples, hint). list()ing all rows and pickling them into Redis spiked
    # every web worker by hundreds of MB (and an even bigger blob sat in
    # Redis per filter combination). Pagination stays in SQL via LIMIT/OFFSET
    # and only the 20 lightweight rows of the current page are fetched.
    cache_version = cache.get('problem_list_version', 1)

    difficulty = (request.GET.get('difficulty') or '').strip()
    search = (request.GET.get('search') or '').strip()
    tags_query = (request.GET.get('tags') or '').strip()
    page_number = request.GET.get('page') or 1

    # The page number only affects slicing, not the filtered result set, so
    # the count cache key is derived from filters only.
    filter_key = f'd={difficulty}&s={search}&t={tags_query}'

    problems = Problem.objects.filter(is_public=True).only(
        'id', 'title', 'difficulty', 'tags', 'created_at',
    )

    # Filter by difficulty
    if difficulty:
        problems = problems.filter(difficulty=difficulty)

    # Fuzzy search: every whitespace/comma-separated term must appear in
    # the title OR the tags (AND across terms, OR across fields). An
    # all-numeric query also matches the exact problem ID. The ORM
    # parameterizes every term; matching tags fuzzily means "csp 2024"
    # finds problems tagged "CSP-S 2024" and "dp" finds "DP / 动态规划".
    if search:
        filters = Q()
        for term in _split_search_terms(search):
            term_q = Q()
            for alias in search_aliases(term):
                term_q |= Q(title__icontains=alias) | Q(tags__icontains=alias)
            filters &= term_q
        if search.isdecimal():
            filters |= Q(id=int(search))
        problems = problems.filter(filters)

    # Dedicated tag filter: every term must match a tag (AND across
    # terms), so "动态规划 贪心" narrows to problems carrying both tags.
    for term in _split_search_terms(tags_query):
        tag_q = Q()
        for alias in search_aliases(term):
            tag_q |= Q(tags__icontains=alias)
        problems = problems.filter(tag_q)

    # Aggregate submission stats in the same page query instead of firing a
    # COUNT per table row (and bypassing Problem.pass_rate's per-row cache
    # round-trips).
    problems = problems.annotate(
        total_subs=Count('submissions'),
        accepted_subs=Count('submissions', filter=Q(submissions__status='Accepted')),
    ).order_by('-created_at')

    # Cache only the integer total per filter (tiny, cheap to pickle).
    count_cache_key = f'problem_list_count:v{cache_version}:{filter_key}'
    total_count = cache.get(count_cache_key)
    if total_count is None:
        total_count = problems.count()
        cache.set(count_cache_key, total_count, 60 * 5)

    paginator = Paginator(problems, PROBLEMS_PER_PAGE)
    # Paginator.count is a cached_property backed by queryset.count(); reuse
    # the cached integer so a page request doesn't run COUNT twice.
    paginator.__dict__['count'] = total_count
    page_obj = paginator.get_page(page_number)

    # Only the <=20 rows on this page are in memory; derive the display rate
    # here (Problem.pass_rate is a data-descriptor property, so it can't be
    # shadowed by an ORM annotation of the same name).
    for problem in page_obj:
        total = problem.total_subs
        problem.list_pass_rate = round(
            problem.accepted_subs * 100.0 / total, 1
        ) if total else 0.0

    # Preserve filter query string across pagination links
    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    return render(request, 'problems/problem_list.html', {
        'problems': page_obj,  # Iterable over the current page's problems
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'query_string': query_string,
        'total_count': paginator.count,
    })


# Intentionally NOT cached — the page exposes user-specific state
# (whether the logged-in user has the `submit` feature disabled).
def problem_detail(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id, is_public=True)
    submit_disabled = False
    if request.user.is_authenticated:
        fn = getattr(request.user, 'feature_disabled', None)
        if callable(fn):
            try:
                submit_disabled = bool(fn('submit'))
            except Exception:
                submit_disabled = False
    return render(request, 'problems/problem_detail.html', {
        'problem': problem,
        'count': problem.submissions.filter(status='Accepted').count(),
        'submit_disabled': submit_disabled,
    })


@login_required
def create_problem(request):
    # Feature-ban: if the user has `create_problem` disabled, don't let them through.
    create_disabled = False
    if request.user.is_authenticated:
        fn = getattr(request.user, 'feature_disabled', None)
        if callable(fn):
            try:
                create_disabled = bool(fn('create_problem'))
            except Exception:
                create_disabled = False
    if create_disabled:
        try:
            import json as _json
            from django.utils import timezone as _tz
            ends_at = getattr(request.user, 'disabled_features_until', None)
            payload = {
                'kind': 'feature_ban',
                'title': '上传题目功能已被禁用',
                'reason': '你当前无法在谷物 OJ 上传新题目，若认为这是误判可联系管理员申诉。',
                'features': ['create_problem'],
                'feature_labels': ['禁止上传新题目'],
                'username': getattr(request.user, 'username', ''),
            }
            if ends_at:
                try:
                    payload['ends_at'] = _tz.localtime(ends_at).strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    pass
            request.session['punishment_notice'] = _json.dumps(payload, ensure_ascii=False)
        except Exception:
            messages.error(request, '当前账号的上传题目功能已被管理员禁用。')
        return redirect('home')

    if request.method == 'POST':
        form = ProblemForm(request.POST)
        test_cases = parse_test_cases_from_post(request.POST)
        test_error = validate_test_cases(test_cases)

        if form.is_valid() and not test_error:
            problem = form.save(commit=False)
            problem.created_by = request.user
            problem.is_public = False
            problem.save()
            save_test_cases(problem, test_cases)
            messages.success(request, f'题目 P{problem.id} 上传成功，已添加 {len(test_cases)} 个测试用例。')
            return redirect('home')

        if test_error:
            messages.error(request, test_error)
    else:
        form = ProblemForm()

    return render(request, 'problems/create_problem.html', {'form': form})


# The expensive aggregate query is cached below, but the rendered page is
# not: it extends base.html with the visitor-specific navbar. cache_page
# keys on URL + Vary headers only, so without a per-user (Cookie) variant
# every visitor shared one cached page and could see another account's
# chrome. @never_cache also keeps the personalized HTML out of shared
# caches/CDNs.
@never_cache
def leaderboard(request):
    # Cache the complex query result separately
    query_cache_key = 'leaderboard_users'
    cached_users = cache.get(query_cache_key)

    if cached_users is not None:
        users = cached_users
    else:
        users = User.objects.exclude(
            # Dedicated service account for AI judge verifications — it only
            # runs model-authored reference programs and must not compete in
            # the human leaderboard.
            username=getattr(settings, 'AI_JUDGE_BOT_USERNAME', '__ai_judge_bot__')
        ).annotate(
            solved_count=Count('solved_problems', distinct=True),
            submission_count=Count('submissions', distinct=True)
        ).annotate(
            ratio=Case(
                When(submission_count=0, then=Value(None, output_field=FloatField())),
                default=(
                        Cast(F('solved_count'), FloatField()) * 100.0 /
                        Cast(F('submission_count'), FloatField())
                ),
                output_field=FloatField()
            )
        ).only('id', 'username', 'nickname', 'created_at').order_by(
            F('ratio').desc(nulls_last=True), 'id'
        )[:100]
        # Cache a bounded, fully evaluated result to avoid repeated aggregate
        # queries and unbounded rendering of the entire user table.
        users = list(users)
        cache.set(query_cache_key, users, 60 * 10)

    return render(request, 'leaderboard.html', {'users': users})


def solution_list(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id, is_public=True)
    solutions = problem.solutions.filter(is_approved=True)


    # Show user's own solutions even if not approved
    if request.user.is_authenticated:
        user_solutions = problem.solutions.filter(author=request.user, is_approved=False)
        solutions = solutions | user_solutions

    solutions = solutions.annotate(
        rn=Window(
            expression=RowNumber(),
            partition_by=[F('id')],
            order_by=F('likes').desc()
        )
    ).filter(rn=1).order_by('likes')

    # s = []
    #
    # for i in range(len(solutions), 0, -1):
    #     if solutions[i-1] not in s:
    #         s.append(solutions[i-1])

    # solutions = s

    # Avoid per-row queries when rendering the list: fetch the author with
    # the row (select_related) and resolve like counts with one aggregate
    # query instead of the M2M ``like_count`` property (1 query per row).
    solutions = list(solutions.select_related('author'))
    if solutions:
        like_counts = dict(
            Solution.likes.through.objects
            .filter(solution_id__in=[s.pk for s in solutions])
            .values_list('solution_id')
            .annotate(n=Count('id'))
            .values_list('solution_id', 'n')
        )
        for solution in solutions:
            solution.like_total = like_counts.get(solution.pk, 0)

    return render(request, 'problems/solution_list.html', {
        'problem': problem,
        'solutions': solutions
    })


def solution_detail(request, problem_id, solution_id):
    problem = get_object_or_404(Problem, id=problem_id, is_public=True)
    solution = get_object_or_404(Solution, id=solution_id, problem=problem)
    
    # Check if user can view this solution
    can_view = solution.is_approved or (request.user.is_authenticated and solution.author == request.user)
    
    if not can_view:
        messages.error(request, '您没有权限查看此题解')
        return redirect('solution_list', problem_id=problem_id)
    
    return render(request, 'problems/solution_detail.html', {
        'problem': problem,
        'solution': solution,
        'is_liked': request.user.is_authenticated and request.user in solution.likes.all()
    })


@login_required
def create_solution(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id, is_public=True)
    
    if request.method == 'POST':
        title = request.POST.get('title')
        content = request.POST.get('content')
        
        if title and content:
            solution = Solution.objects.create(
                problem=problem,
                author=request.user,
                title=title,
                content=content,
                is_approved=False  # 需要管理员审核
            )
            messages.success(request, '题解提交成功，等待管理员审核')
            return redirect('solution_detail', problem_id=problem_id, solution_id=solution.id)
        else:
            messages.error(request, '标题和内容不能为空')
    
    return render(request, 'problems/create_solution.html', {'problem': problem})


@login_required
@require_POST
def like_solution(request, problem_id, solution_id):
    problem = get_object_or_404(Problem, id=problem_id, is_public=True)
    solution = get_object_or_404(Solution, id=solution_id, problem=problem)
    
    if request.user in solution.likes.all():
        solution.likes.remove(request.user)
    else:
        solution.likes.add(request.user)
    
    return redirect('solution_detail', problem_id=problem_id, solution_id=solution_id)
