from datetime import timedelta

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from contests.models import Contest, ContestProblem
from problems.models import Problem, TestCase as ProblemTestCase
from submissions.models import Submission, SubmissionTestResult
from users.models import User


class ContestFeatureTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user('creator', 'creator@example.com', 'password')
        self.player = User.objects.create_user('player', 'player@example.com', 'password')
        self.problem = Problem.objects.create(
            title='Contest question', description='Description', input_format='Input',
            output_format='Output', sample_input='1', sample_output='1',
            created_by=self.creator, is_public=False,
        )
        for index in range(3):
            ProblemTestCase.objects.create(
                problem=self.problem, input_data=str(index), expected_output=str(index), order=index,
            )

    def create_contest(self, start_delta=-timedelta(minutes=5), end_delta=timedelta(minutes=30), limit=2):
        contest = Contest.objects.create(
            name=f'Contest {Contest.objects.count()}', creator=self.creator,
            start_at=timezone.now() + start_delta, end_at=timezone.now() + end_delta,
            max_submissions_per_problem=limit,
        )
        item = ContestProblem.objects.create(contest=contest, problem=self.problem)
        return contest, item

    def test_question_is_hidden_before_start_and_available_when_live(self):
        contest, item = self.create_contest(start_delta=timedelta(minutes=5))
        self.assertTrue(self.client.login(username='player', password='password'))
        response = self.client.get(reverse('contest_question', args=[contest.id, item.id]))
        self.assertContains(response, '题目暂不可访问')
        contest.start_at = timezone.now() - timedelta(minutes=1)
        contest.save(update_fields=['start_at'])
        response = self.client.get(reverse('contest_question', args=[contest.id, item.id]))
        self.assertContains(response, self.problem.title)

    def test_finished_contest_publishes_problem_and_origin_tag_once(self):
        contest, _ = self.create_contest(end_delta=-timedelta(minutes=1))
        contest.publish_finished_problems()
        self.problem.refresh_from_db()
        contest.refresh_from_db()
        self.assertTrue(self.problem.is_public)
        self.assertIn('contest:contest-0', self.problem.tags.split())
        first_published_at = contest.published_at
        self.assertFalse(contest.publish_finished_problems())
        contest.refresh_from_db()
        self.assertEqual(contest.published_at, first_published_at)

    def test_attempt_cap_rejects_extra_contest_submission(self):
        contest, item = self.create_contest(limit=1)
        self.assertTrue(self.client.login(username='player', password='password'))
        url = reverse('submit_contest_solution', args=[contest.id, item.id])
        response = self.client.post(url, {'language': 'Ruby', 'code': 'puts 1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Submission.objects.filter(contest_problem=item, user=self.player).count(), 1)
        response = self.client.post(url, {'language': 'Ruby', 'code': 'puts 2'}, follow=True)
        self.assertContains(response, '本题提交次数已用完，请移步下一题')
        self.assertEqual(Submission.objects.filter(contest_problem=item, user=self.player).count(), 1)

    def test_standings_uses_case_result_formula(self):
        contest, item = self.create_contest()
        submission = Submission.objects.create(
            problem=self.problem, user=self.player, contest_problem=item, code='puts 1', language='Ruby',
        )
        SubmissionTestResult.objects.create(submission=submission, case_index=0, status='Accepted')
        SubmissionTestResult.objects.create(submission=submission, case_index=1, status='Wrong Answer')
        response = self.client.get(reverse('contest_standings', args=[contest.id]))
        self.assertContains(response, self.player.username)
        self.assertContains(response, '>1<')

    def test_creation_requires_native_add_contest_permission(self):
        self.assertTrue(self.client.login(username='player', password='password'))
        url = reverse('create_contest')
        self.assertEqual(self.client.get(url).status_code, 403)
        self.player.user_permissions.add(Permission.objects.get(codename='add_contest'))
        self.assertEqual(self.client.get(url).status_code, 200)
