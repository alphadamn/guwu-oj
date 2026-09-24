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
from .models import Problem, Solution, split_stored_tags
from .forms import (
    ProblemForm,
    parse_function_files_from_post,
    parse_test_cases_from_post,
    save_test_cases,
    validate_function_files,
    validate_test_cases,
)
from .tag_labels import (
    TAG_GROUPS,
    SOURCE_TAGS,
    canonical_tag_name,
    filter_aliases,
    is_provenance_tag,
    search_aliases,
    to_zh_algorithm_tag,
)
from users.models import User
from submissions.models import Submission

PROBLEMS_PER_PAGE = 20

# Tags are stored as free text, separated by spaces and/or commas
# (ASCII "," and Chinese "，"); user input may also use "、" or ";".
_TERM_SPLIT_RE = re.compile(r'[\s,，、;；]+')

# Whole-token boundaries for exact tag regex matching.
_TAG_TOKEN_SEP = r' ,、;；，\s'

# Tags below this per-tag problem count are not added to the picker's
# auto-discovered "其他" group (the curated groups always stay visible).
_EXTRA_TAG_MIN_COUNT = 5

TAG_CATALOG_CACHE_KEY = 'problem_tag_catalog'
TAG_CATALOG_CACHE_SECONDS = 60 * 10


def _split_search_terms(text):
    """Split a search/filter string into fuzzy-match terms."""
    return [term for term in _TERM_SPLIT_RE.split(text or '') if term]


def _tag_token_q(alias):
    """Q matching ``alias`` as one whole stored tag token (case-insensitive).

    Boundaries are the same separators ``split_stored_tags`` understands.
    Case-insensitivity is required for legacy rows storing uppercase aliases
    (e.g. ``DP``); CJK labels are unaffected.
    """
    pattern = (
        r'(?:^|[' + _TAG_TOKEN_SEP + r'])'
        + re.escape(alias)
        + r'(?:[' + _TAG_TOKEN_SEP + r']|$)'
    )
    return Q(tags__iregex=pattern)


def _build_tag_catalog():
    """All selectable tags grouped, with public-problem counts.

    One scan over the tags column (only field pulled); each problem counts
    at most once per tag. Algorithm labels are normalized through the same
    Chinese mapping used for badge display; source tags are counted on
    their exact stored token.
    """
    algorithm_counts = {}
    source_counts = {name: 0 for name in SOURCE_TAGS}
    source_names = set(SOURCE_TAGS)

    for raw in Problem.objects.filter(is_public=True).values_list('tags', flat=True).iterator():
        alg_hits = set()
        source_hits = set()
        for token in split_stored_tags(raw):
            if token in source_names:
                source_hits.add(token)
            elif not is_provenance_tag(token):
                zh = to_zh_algorithm_tag(token)
                if zh:
                    alg_hits.add(zh)
        for name in alg_hits:
            algorithm_counts[name] = algorithm_counts.get(name, 0) + 1
        for name in source_hits:
            source_counts[name] += 1

    grouped_names = set()
    groups = []
    other_group = None
    for group_name, tag_names in TAG_GROUPS.items():
        group = {
            'name': group_name,
            'tags': [
                {'name': name, 'count': algorithm_counts.get(name, 0)}
                for name in tag_names
            ],
        }
        grouped_names.update(tag_names)
        groups.append(group)
        if group_name == '其他':
            other_group = group

    # Auto-discovered Chinese tags not curated above (e.g. future labels)
    # join the "其他" group, most frequent first.
    extras = sorted(
        ((name, count) for name, count in algorithm_counts.items()
         if name not in grouped_names and count >= _EXTRA_TAG_MIN_COUNT),
        key=lambda item: (-item[1], item[0]),
    )
    if extras:
        if other_group is None:
            other_group = {'name': '其他', 'tags': []}
            groups.append(other_group)
        other_group['tags'].extend(
            {'name': name, 'count': count} for name, count in extras
        )

    groups.append({
        'name': '题源',
        'tags': [
            {'name': name, 'count': source_counts.get(name, 0)}
            for name in SOURCE_TAGS
        ],
    })

    selectable = set(grouped_names) | {name for name, _ in extras} | source_names
    return {'groups': groups, 'names': selectable}


def _get_tag_catalog():
    catalog = cache.get(TAG_CATALOG_CACHE_KEY)
    if catalog is None:
        catalog = _build_tag_catalog()
        cache.set(TAG_CATALOG_CACHE_KEY, catalog, TAG_CATALOG_CACHE_SECONDS)
    return catalog


def _parse_selected_tags(raw_values, selectable):
    """Normalize GET tag params to canonical selectable names (deduped).

    Accepts both repeated params (``?tags=dp&tags=greedy``) and legacy
    space/comma-separated text (``?tags=dp greedy``); English aliases are
    mapped to their canonical Chinese label.
    """
    selected = []
    seen = set()
    for raw in raw_values:
        for term in _split_search_terms(raw):
            name = canonical_tag_name(term)
            if name and name in selectable and name not in seen:
                seen.add(name)
                selected.append(name)
    return selected


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
    page_number = request.GET.get('page') or 1

    tag_catalog = _get_tag_catalog()
    selected_tags = _parse_selected_tags(
        request.GET.getlist('tags'), tag_catalog['names']
    )
    selected_tag_set = set(selected_tags)

    # The page number only affects slicing, not the filtered result set, so
    # the count cache key is derived from filters only.
    filter_key = f'd={difficulty}&s={search}&t={"|".join(selected_tags)}'

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

    # Luogu-style tag selection: every picked tag must be present as a
    # whole stored token (AND across tags). Algorithm tags also match their
    # safe English aliases (e.g. 动态规划 matches "dp"); source tags match
    # their exact stored token.
    for name in selected_tags:
        tag_q = Q()
        for alias in filter_aliases(name):
            tag_q |= _tag_token_q(alias)
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

    # Per-chip removal links: current filters minus that one tag.
    selected_tag_chips = []
    for name in selected_tags:
        chip_params = request.GET.copy()
        chip_params.pop('page', None)
        remaining = [t for t in selected_tags if t != name]
        if remaining:
            chip_params.setlist('tags', remaining)
        else:
            chip_params.pop('tags', None)
        selected_tag_chips.append({
            'name': name,
            'query_string': chip_params.urlencode(),
        })

    return render(request, 'problems/problem_list.html', {
        'problems': page_obj,  # Iterable over the current page's problems
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'query_string': query_string,
        'total_count': paginator.count,
        'tag_groups': tag_catalog['groups'],
        'selected_tag_set': selected_tag_set,
        'selected_tag_chips': selected_tag_chips,
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
        function_files = parse_function_files_from_post(request.POST)
        # problem_type comes through ProblemForm (it's in Meta.fields); pull
        # it off the cleaned form so validate_function_files matches what
        # will actually be persisted.
        p_type = (
            form.cleaned_data.get('problem_type') if form.is_valid() else
            request.POST.get('problem_type', 'standard')
        )
        files_error = validate_function_files(function_files, p_type)

        if form.is_valid() and not test_error and not files_error:
            problem = form.save(commit=False)
            problem.created_by = request.user
            problem.is_public = False
            # Persist the function-file bundle as JSON; standard problems
            # store '[]' (the field default) so the judge treats them as
            # plain main.cpp submissions.
            import json as _json
            problem.function_files = _json.dumps(function_files)
            problem.save()
            save_test_cases(problem, test_cases)
            messages.success(request, f'题目 P{problem.id} 上传成功，已添加 {len(test_cases)} 个测试用例。')
            return redirect('home')

        if test_error:
            messages.error(request, test_error)
        if files_error:
            messages.error(request, files_error)
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
