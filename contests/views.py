from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from submissions.models import Submission
from .models import Contest, ContestEnrollment, ContestProblem


def _publish_finished():
    for contest in Contest.objects.filter(end_at__lte=timezone.now()):
        contest.publish_finished_problems()


def contest_list(request):
    _publish_finished()
    return render(request, 'contests/list.html', {'contests': Contest.objects.all()})


def _contest_or_404(contest_id):
    _publish_finished()
    return get_object_or_404(Contest.objects.prefetch_related('problems__test_cases'), pk=contest_id)


def contest_detail(request, contest_id):
    contest = _contest_or_404(contest_id)
    enrolled = (
        request.user.is_authenticated
        and ContestEnrollment.objects.filter(contest=contest, user=request.user).exists()
    )
    return render(request, 'contests/detail.html', {
        'contest': contest,
        'finished': contest.is_finished,
        'enrolled': enrolled,
    })


@login_required
@require_POST
def join_contest(request, contest_id):
    contest = _contest_or_404(contest_id)
    if contest.is_finished:
        messages.error(request, '竞赛已结束，无法参加。')
        return redirect('contest_detail', contest_id=contest.id)

    with transaction.atomic():
        contest = Contest.objects.select_for_update().get(pk=contest.id)
        if ContestEnrollment.objects.filter(contest=contest, user=request.user).exists():
            messages.info(request, '你已经参加了本场竞赛。')
            return redirect('contest_detail', contest_id=contest.id)
        from points.services import InsufficientPoints, apply_points
        try:
            apply_points(
                user_id=request.user.id,
                amount=-contest.entry_points_cost,
                event_type='contest_entry',
                event_key=str(contest.id),
                description=f'参加竞赛：{contest.name}',
            )
        except InsufficientPoints:
            messages.error(request, f'积分不足，参加本场竞赛需要 {contest.entry_points_cost} 积分。')
            return redirect('contest_detail', contest_id=contest.id)
        ContestEnrollment.objects.create(
            contest=contest, user=request.user, points_cost=contest.entry_points_cost,
        )
    messages.success(request, f'已参加竞赛，消耗 {contest.entry_points_cost} 积分。')
    return redirect('contest_detail', contest_id=contest.id)


def _require_enrollment(request, contest):
    if not ContestEnrollment.objects.filter(contest=contest, user=request.user).exists():
        messages.error(request, '请先参加竞赛后再查看题目和提交代码。')
        return False
    return True


@login_required
def contest_question(request, contest_id, question_id):
    contest = _contest_or_404(contest_id)
    item = get_object_or_404(ContestProblem.objects.select_related('contest'), pk=question_id, contest=contest)
    if not contest.is_live:
        return render(request, 'contests/unavailable.html', {'contest': contest})
    if not _require_enrollment(request, contest):
        return redirect('contest_detail', contest_id=contest.id)
    submissions = Submission.objects.filter(user=request.user, contest_problem=item) if request.user.is_authenticated else Submission.objects.none()
    used = submissions.count()
    solved = submissions.filter(status='Accepted').exists()
    return render(request, 'contests/question.html', {
        'contest': contest,
        'item': item,
        'problem': item,
        'used': used,
        'solved': solved,
        'remaining': max(0, contest.max_submissions_per_problem - used),
    })


@login_required
@ratelimit(key='user', rate='60/m', method='POST', block=False)
@require_POST
def submit_contest_solution(request, contest_id, question_id):
    contest = _contest_or_404(contest_id)
    item = get_object_or_404(ContestProblem.objects.select_related('contest'), pk=question_id, contest=contest)
    if not contest.is_live:
        messages.error(request, '当前不在竞赛进行时间内。')
        return redirect('contest_detail', contest_id=contest.id)
    if not _require_enrollment(request, contest):
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
        locked = ContestProblem.objects.select_for_update().select_related('contest').get(pk=item.id)
        existing_submissions = Submission.objects.filter(user=request.user, contest_problem=locked)
        if existing_submissions.filter(status='Accepted').exists():
            messages.success(request, '本题已全部通过，无需再次提交，请继续下一题。')
            return redirect('contest_question', contest_id=contest.id, question_id=item.id)
        used = existing_submissions.count()
        if used >= locked.contest.max_submissions_per_problem:
            messages.error(request, '本题提交次数已用完，请移步下一题。')
            return redirect('contest_question', contest_id=contest.id, question_id=item.id)
        submission = Submission.objects.create(user=request.user, code=code, language=language, contest_problem=locked)
    try:
        from users.captcha import record_submission_attempt
        record_submission_attempt(request.user.id, success=True)
    except Exception:
        pass
    from submissions.judge import JUDGED_LANGUAGES
    from submissions.judge_queue import enqueue_judge
    if language in JUDGED_LANGUAGES and locked.test_cases.exists():
        enqueue_judge(submission.id)
    return redirect('submission_detail', submission_id=submission.id)


def contest_standings(request, contest_id):
    contest = _contest_or_404(contest_id)
    submissions = Submission.objects.filter(
        contest_problem__contest=contest,
    ).select_related('user').prefetch_related('test_results').order_by(
        'user_id', 'contest_problem_id', '-created_at', '-id',
    )

    latest_submissions = {}
    for submission in submissions:
        latest_submissions.setdefault((submission.user_id, submission.contest_problem_id), submission)

    grouped = {}
    for submission in latest_submissions.values():
        row = grouped.setdefault(
            submission.user_id,
            {'user': submission.user, 'accepted': 0, 'non_accepted': 0, 'submissions': 0},
        )
        row['submissions'] += 1
        for result in submission.test_results.all():
            if result.status == 'Accepted':
                row['accepted'] += 1
            else:
                row['non_accepted'] += 1

    for row in grouped.values():
        row['score'] = row['accepted'] * 1.5 - row['non_accepted']
    rows = sorted(grouped.values(), key=lambda value: (-value['score'], -value['accepted'], value['user'].id))
    return render(request, 'contests/standings.html', {'contest': contest, 'standings': rows})
