from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET
from django_ratelimit.decorators import ratelimit

from problems.models import Problem

from .judge import JUDGED_LANGUAGES
from .judge_queue import enqueue_judge
from .models import Submission


def _submit_disabled(user) -> bool:
    """True when ``user`` has the ``submit`` feature disabled and the
    deadline (if any) is still in the future."""
    try:
        fn = getattr(user, 'feature_disabled', None)
        if callable(fn):
            return bool(fn('submit'))
    except Exception:
        return False
    return False


@login_required
@ratelimit(key='user', rate='60/m', method='POST', block=False)
def submit_solution(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id, is_public=True)

    # Feature-ban: when the user has the "禁止提交题目" feature flag active,
    # refuse with a 403 to avoid spam creating rows.
    if _submit_disabled(request.user):
        if request.method == 'POST':
            messages.error(request, '当前账号的提交功能已被管理员禁用，暂时无法提交解答。')
        return render(request, 'submissions/submit.html', {
            'problem': problem,
            'submit_disabled': True,
        })

    # Submission-rate captcha: if the user has exceeded the admin-configured
    # frequency, demand a valid captcha challenge before accepting the next
    # submission.
    requires_captcha = False
    try:
        from users.captcha import (
            submission_requires_captcha as _sr_captcha,
            check_submission_captcha as _check_submission_captcha,
        )
        requires_captcha = _sr_captcha(request)
    except Exception:
        _sr_captcha = None
        _check_submission_captcha = None
        requires_captcha = False

    if request.method == 'POST':
        code = request.POST.get('code')
        language = request.POST.get('language')

        if requires_captcha and _check_submission_captcha is not None:
            ok, msg = _check_submission_captcha(request)
            if not ok:
                messages.error(request, msg or '图形验证码错误，请重新输入后再提交。')
                return render(request, 'submissions/submit.html', {
                    'problem': problem,
                    'requires_captcha': True,
                })

        max_bytes = settings.OJ_MAX_SUBMISSION_CODE_BYTES
        if code and len(code.encode('utf-8')) > max_bytes:
            messages.error(
                request,
                f'代码过大（上限 {max_bytes // 1024} KB），请缩短后重试。',
            )
        elif code and language:
            submission = Submission.objects.create(
                problem=problem,
                user=request.user,
                code=code,
                language=language,
                status='Pending'
            )
            # Increment rate-limit counter for captcha escalation.
            try:
                from users.captcha import record_submission_attempt
                record_submission_attempt(request.user.id, success=True)
            except Exception:
                pass
            if language in JUDGED_LANGUAGES and problem.test_cases.exists():
                enqueue_judge(submission.id)
            return redirect('submission_detail', submission_id=submission.id)

    return render(request, 'submissions/submit.html', {
        'problem': problem,
        'requires_captcha': requires_captcha,
    })


@login_required
def submission_detail(request, submission_id):
    submission = get_object_or_404(
        Submission.objects.prefetch_related('test_results'),
        id=submission_id,
    )
    if submission.user_id != request.user.id and not request.user.is_staff:
        raise Http404('Submission not found')
    test_results = list(submission.test_results.all())
    # print(test_results)
    for result in test_results:
        result.expected_output = ''
    passed_count = sum(1 for r in test_results if r.status == 'Accepted')
    # should_poll = (
    #     submission.status == 'Pending'
    #     and submission.language in JUDGED_LANGUAGES
    #     and submission.problem.test_cases.exists()
    # )
    should_poll = (submission.language in JUDGED_LANGUAGES and submission.problem.test_cases.exists())
    return render(request, 'submissions/detail.html', {
        'submission': submission,
        'test_results': test_results,
        'passed_count': passed_count,
        'total_cases': len(test_results),
        'should_poll': should_poll,
    })


@login_required
@require_GET
def submission_status_api(request, submission_id):
    submission = get_object_or_404(
        Submission.objects.select_related('problem').prefetch_related('test_results'),
        id=submission_id,
    )
    if submission.user_id != request.user.id and not request.user.is_staff:
        raise Http404('Submission not found')

    test_results = list(submission.test_results.order_by('case_index'))
    # print(test_results[0].runtime)
    passed_count = sum(1 for r in test_results if r.status == 'Accepted')
    total_cases = submission.problem.test_cases.count()
    judging = (
        submission.status == 'Pending'
        and submission.language in JUDGED_LANGUAGES
        and total_cases > 0
    )

    return JsonResponse({
        'status': submission.status,
        'runtime': str(submission.runtime),
        'memory': submission.memory,
        'passed_count': passed_count,
        'total_cases': max(total_cases, len(test_results)),
        'done': not judging,
        'test_results': [
            {
                'case_index': r.case_index,
                'status': r.status,
                'runtime': str(r.runtime),
            }
            for r in test_results
        ],
    })


@login_required
def submission_list(request):
    submissions = Submission.objects.filter(user=request.user)
    return render(request, 'submissions/list.html', {'submissions': submissions})


@login_required
def all_submissions(request):
    submissions = Submission.objects.select_related(
        'user', 'problem'
    ).all()

    # Filter by problem
    problem_id = request.GET.get('problem')
    if problem_id:
        submissions = submissions.filter(problem_id=problem_id)

    # Filter by user
    username = request.GET.get('user')
    if username:
        submissions = submissions.filter(user__username=username)

    # Filter by status
    status = request.GET.get('status')
    if status:
        submissions = submissions.filter(status=status)

    submissions = submissions.order_by('-created_at')

    # Pagination: 20 records per page instead of flat [:100]
    paginator = Paginator(submissions, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Preserve filter query string across pagination links
    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    return render(request, 'submissions/all_list.html', {
        'page_obj': page_obj,
        'submissions': page_obj,  # Backward-compat alias for template
        'is_paginated': page_obj.has_other_pages(),
        'query_string': query_string,
    })
