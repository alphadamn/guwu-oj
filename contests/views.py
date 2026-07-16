from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from problems.forms import ProblemForm, parse_test_cases_from_post, save_test_cases, validate_test_cases
from problems.models import Problem
from submissions.models import Submission, SubmissionTestResult
from .forms import ContestForm
from .models import Contest, ContestProblem


def _publish_finished():
    for contest in Contest.objects.filter(published_at__isnull=True, end_at__lte=timezone.now()):
        contest.publish_finished_problems()


def contest_list(request):
    _publish_finished()
    return render(request, 'contests/list.html', {'contests': Contest.objects.all()})


def _contest_or_404(contest_id):
    _publish_finished()
    return get_object_or_404(Contest.objects.prefetch_related('problems__problem'), pk=contest_id)


def contest_detail(request, contest_id):
    contest = _contest_or_404(contest_id)
    if contest.is_finished:
        return render(request, 'contests/detail.html', {'contest': contest, 'finished': True})
    return render(request, 'contests/detail.html', {'contest': contest, 'finished': False})


@login_required
def contest_question(request, contest_id, question_id):
    contest = _contest_or_404(contest_id)
    item = get_object_or_404(ContestProblem.objects.select_related('problem', 'contest'), pk=question_id, contest=contest)
    if not contest.is_live:
        return render(request, 'contests/unavailable.html', {'contest': contest})
    used = Submission.objects.filter(user=request.user, contest_problem=item).count() if request.user.is_authenticated else 0
    return render(request, 'contests/question.html', {'contest': contest, 'item': item, 'problem': item.problem, 'used': used, 'remaining': max(0, contest.max_submissions_per_problem - used)})


@login_required
@permission_required('contests.add_contest', raise_exception=True)
def create_contest(request):
    if request.method == 'POST':
        contest_form = ContestForm(request.POST)
        question_form = ProblemForm(request.POST)
        cases = parse_test_cases_from_post(request.POST)
        error = validate_test_cases(cases)
        if contest_form.is_valid() and question_form.is_valid() and not error:
            with transaction.atomic():
                contest = Contest.objects.create(creator=request.user, **contest_form.cleaned_data)
                problem = question_form.save(commit=False)
                problem.created_by = request.user
                problem.is_public = False
                problem.save()
                save_test_cases(problem, cases)
                ContestProblem.objects.create(contest=contest, problem=problem, order=0)
            messages.success(request, '竞赛创建成功。')
            return redirect('contest_detail', contest_id=contest.id)
        if error:
            messages.error(request, error)
    else:
        contest_form = ContestForm()
        question_form = ProblemForm()
    return render(request, 'contests/create.html', {'contest_form': contest_form, 'question_form': question_form})


@login_required
@permission_required('contests.change_contest', raise_exception=True)
def add_contest_question(request, contest_id):
    contest = get_object_or_404(Contest, pk=contest_id)
    if contest.is_finished:
        raise Http404('Contest has already finished')
    if request.method == 'POST':
        question_form = ProblemForm(request.POST)
        cases = parse_test_cases_from_post(request.POST)
        error = validate_test_cases(cases)
        if question_form.is_valid() and not error:
            with transaction.atomic():
                problem = question_form.save(commit=False)
                problem.created_by = request.user
                problem.is_public = False
                problem.save()
                save_test_cases(problem, cases)
                ContestProblem.objects.create(
                    contest=contest,
                    problem=problem,
                    order=contest.problems.count(),
                )
            messages.success(request, '竞赛题目已添加。')
            return redirect('contest_detail', contest_id=contest.id)
        if error:
            messages.error(request, error)
    else:
        question_form = ProblemForm()
    return render(request, 'contests/add_question.html', {
        'contest': contest,
        'question_form': question_form,
    })


@login_required
@ratelimit(key='user', rate='60/m', method='POST', block=False)
@require_POST
def submit_contest_solution(request, contest_id, question_id):
    contest = _contest_or_404(contest_id)
    item = get_object_or_404(ContestProblem.objects.select_related('problem'), pk=question_id, contest=contest)
    if not contest.is_live:
        messages.error(request, '当前不在竞赛进行时间内。')
        return redirect('contest_detail', contest_id=contest.id)
    if request.user.feature_disabled('submit'):
        messages.error(request, '当前账号的提交功能已被管理员禁用。')
        return redirect('contest_question', contest_id=contest.id, question_id=item.id)
    code = request.POST.get('code', '')
    language = request.POST.get('language', '')
    if code and len(code.encode('utf-8')) > settings.OJ_MAX_SUBMISSION_CODE_BYTES:
        messages.error(request, f'代码过大（上限 {settings.OJ_MAX_SUBMISSION_CODE_BYTES // 1024} KB），请缩短后重试。')
        return redirect('contest_question', contest_id=contest.id, question_id=item.id)
    if not code or not language:
        messages.error(request, '代码和编程语言不能为空。')
        return redirect('contest_question', contest_id=contest.id, question_id=item.id)
    with transaction.atomic():
        locked = ContestProblem.objects.select_for_update().select_related('contest', 'problem').get(pk=item.id)
        used = Submission.objects.filter(user=request.user, contest_problem=locked).count()
        if used >= locked.contest.max_submissions_per_problem:
            messages.error(request, '本题提交次数已用完，请移步下一题。')
            return redirect('contest_question', contest_id=contest.id, question_id=item.id)
        submission = Submission.objects.create(problem=locked.problem, user=request.user, code=code, language=language, contest_problem=locked)
    try:
        from users.captcha import record_submission_attempt
        record_submission_attempt(request.user.id, success=True)
    except Exception:
        pass
    from submissions.judge import JUDGED_LANGUAGES
    from submissions.judge_queue import enqueue_judge
    if language in JUDGED_LANGUAGES and locked.problem.test_cases.exists():
        enqueue_judge(submission.id)
    return redirect('submission_detail', submission_id=submission.id)


def contest_standings(request, contest_id):
    contest = _contest_or_404(contest_id)
    rows = []
    submissions = Submission.objects.filter(contest_problem__contest=contest).select_related('user').prefetch_related('test_results')
    grouped = {}
    for submission in submissions:
        row = grouped.setdefault(submission.user_id, {'user': submission.user, 'accepted': 0, 'non_accepted': 0, 'submissions': 0})
        row['submissions'] += 1
        for result in submission.test_results.all():
            if result.status == 'Accepted':
                row['accepted'] += 1
            else:
                row['non_accepted'] += 1
    for row in grouped.values():
        row['score'] = row['accepted'] * 2 - row['non_accepted']
    rows = sorted(grouped.values(), key=lambda value: (-value['score'], -value['accepted'], value['user'].id))
    return render(request, 'contests/standings.html', {'contest': contest, 'standings': rows})
