from django.db.models.functions import Cast, RowNumber
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q, Count, ExpressionWrapper, When, Case, Value, F, FloatField, Window
from django.views.decorators.http import require_POST
from django.views.decorators.cache import cache_page
from django.core.cache import cache
from .models import Problem, Solution
from .forms import ProblemForm, parse_test_cases_from_post, validate_test_cases, save_test_cases
from users.models import User
from submissions.models import Submission


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
        stats = {
            'problem_count': public_problems.count(),
            'submission_count': Submission.objects.count(),
            'user_count': User.objects.count(),
        }
        cache.set(stats_cache_key, stats, 60 * 5)  # Cache for 5 minutes

    return render(request, 'home.html', {
        'recent_problems': recent_problems,
        'stats': stats,
    })


def problem_list(request):
    # Generate cache key based on query parameters
    cache_key = f'problem_list_{request.GET.urlencode()}'
    cached_response = cache.get(cache_key)

    if cached_response:
        return cached_response

    # Cache the query result separately
    query_cache_key = f'problem_list_query_{request.GET.urlencode()}'
    cached_problems = cache.get(query_cache_key)

    if cached_problems is not None:
        problems = cached_problems
    else:
        problems = Problem.objects.filter(is_public=True)

        # Filter by difficulty
        difficulty = request.GET.get('difficulty')
        if difficulty:
            problems = problems.filter(difficulty=difficulty)

        # Search by title or ID
        search = request.GET.get('search')
        if search:
            problems = problems.filter(
                Q(title__icontains=search) | Q(id__icontains=search)
            )

        # Filter by tags
        tags = request.GET.get('tags')
        if tags:
            problems = problems.filter(tags__icontains=tags)

        problems = problems.order_by('-created_at')
        # Cache the queryset evaluation for 5 minutes
        problems = list(problems)  # Force evaluation
        cache.set(query_cache_key, problems, 60 * 5)

    response = render(request, 'problems/problem_list.html', {'problems': problems})
    cache.set(cache_key, response, 60 * 10)  # Cache for 10 minutes
    return response


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


@cache_page(60 * 30)  # Cache for 30 minutes (complex query)
def leaderboard(request):
    # Cache the complex query result separately
    query_cache_key = 'leaderboard_users'
    cached_users = cache.get(query_cache_key)

    if cached_users is not None:
        users = cached_users
    else:
        users = User.objects.annotate(
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
        ).order_by(F('ratio').desc(nulls_last=True))  # highest first, nulls at bottom
        # Cache the queryset evaluation for 10 minutes
        users = list(users)  # Force evaluation
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
