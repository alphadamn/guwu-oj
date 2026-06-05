from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from problems.models import Problem

from .judge import JUDGED_LANGUAGES
from .judge_queue import enqueue_judge
from .models import Submission


@login_required
def submit_solution(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id, is_public=True)
    
    if request.method == 'POST':
        code = request.POST.get('code')
        language = request.POST.get('language')
        
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
            if language in JUDGED_LANGUAGES and problem.test_cases.exists():
                enqueue_judge(submission.id)
            return redirect('submission_detail', submission_id=submission.id)
    
    return render(request, 'submissions/submit.html', {'problem': problem})


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
    submissions = Submission.objects.all()
    
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
    
    submissions = submissions.order_by('-created_at')[:100]
    return render(request, 'submissions/all_list.html', {'submissions': submissions})
