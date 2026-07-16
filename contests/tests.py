from datetime import timedelta

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from contests.models import Contest, ContestProblem, ContestTestCase
from problems.models import Problem
from submissions.models import Submission, SubmissionTestResult
from users.models import User


class ContestFeatureTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user('creator', 'creator@example.com', 'password')
        self.player = User.objects.create_user('player', 'player@example.com', 'password')

    def create_contest_problem(self, start_delta=-timedelta(minutes=5), end_delta=timedelta(minutes=30), limit=2):
        contest = Contest.objects.create(
            name=f'Contest {Contest.objects.count()}', creator=self.creator,
            start_at=timezone.now() + start_delta, end_at=timezone.now() + end_delta,
            max_submissions_per_problem=limit,
        )
        item = ContestProblem.objects.create(
            contest=contest, title='Contest question', description='**Bold** and $x^2$',
            input_format='Input', output_format='Output', sample_input='1',
            sample_output='1', created_by=self.creator,
        )
        ContestTestCase.objects.bulk_create([
            ContestTestCase(contest_problem=item, input_data=str(index), expected_output=str(index), order=index)
            for index in range(3)
        ])
        return contest, item

    def test_question_is_hidden_before_start_and_available_when_live(self):
        contest, item = self.create_contest_problem(start_delta=timedelta(minutes=5))
        self.assertTrue(self.client.login(username='player', password='password'))
        response = self.client.get(reverse('contest_question', args=[contest.id, item.id]))
        self.assertContains(response, '题目暂不可访问')
        contest.start_at = timezone.now() - timedelta(minutes=1)
        contest.save(update_fields=['start_at'])
        response = self.client.get(reverse('contest_question', args=[contest.id, item.id]))
        self.assertContains(response, item.title)
        self.assertContains(response, '<strong>Bold</strong>', html=True)
        self.assertContains(response, '$x^2$')
        self.assertContains(response, 'problem-mathjax.js')
        self.assertContains(response, 'monaco-editor')
        self.assertContains(response, 'code-editor.js')

    def test_finished_contest_copies_normal_problem_and_origin_tag_once(self):
        contest, item = self.create_contest_problem(end_delta=-timedelta(minutes=1))
        contest.publish_finished_problems()
        item.refresh_from_db()
        contest.refresh_from_db()
        self.assertIsNotNone(item.published_problem)
        self.assertTrue(item.published_problem.is_public)
        self.assertIn('contest:contest-0', item.published_problem.tags.split())
        self.assertEqual(item.published_problem.test_cases.count(), 3)
        first_published_problem = item.published_problem_id
        self.assertFalse(contest.publish_finished_problems())
        item.refresh_from_db()
        self.assertEqual(item.published_problem_id, first_published_problem)

    def test_attempt_cap_rejects_extra_contest_submission(self):
        contest, item = self.create_contest_problem(limit=1)
        self.assertTrue(self.client.login(username='player', password='password'))
        url = reverse('submit_contest_solution', args=[contest.id, item.id])
        response = self.client.post(url, {'language': 'Ruby', 'code': 'puts 1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Submission.objects.filter(contest_problem=item, user=self.player).count(), 1)
        submission = Submission.objects.get(contest_problem=item)
        self.assertIsNone(submission.problem_id)
        self.assertEqual(submission.effective_problem, item)
        response = self.client.get(reverse('submission_detail', args=[submission.id]))
        self.assertContains(response, reverse('contest_question', args=[contest.id, item.id]))
        response = self.client.post(url, {'language': 'Ruby', 'code': 'puts 2'}, follow=True)
        self.assertContains(response, '本题提交次数已用完，请移步下一题')
        self.assertEqual(Submission.objects.filter(contest_problem=item, user=self.player).count(), 1)

    def test_standings_uses_case_result_formula(self):
        contest, item = self.create_contest_problem()
        submission = Submission.objects.create(user=self.player, contest_problem=item, code='puts 1', language='Ruby')
        first_case, second_case = item.test_cases.all()[:2]
        SubmissionTestResult.objects.create(submission=submission, contest_test_case=first_case, case_index=0, status='Accepted')
        SubmissionTestResult.objects.create(submission=submission, contest_test_case=second_case, case_index=1, status='Wrong Answer')
        response = self.client.get(reverse('contest_standings', args=[contest.id]))
        self.assertContains(response, self.player.username)
        self.assertContains(response, '>1<')

    def test_creation_requires_native_add_contest_permission(self):
        self.assertTrue(self.client.login(username='player', password='password'))
        url = reverse('create_contest')
        self.assertEqual(self.client.get(url).status_code, 403)
        self.player.user_permissions.add(Permission.objects.get(codename='add_contest'))
        self.assertEqual(self.client.get(url).status_code, 200)
