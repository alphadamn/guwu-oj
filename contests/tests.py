from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from contests.admin import ContestAdmin
from contests.models import Contest, ContestEnrollment, ContestProblem, ContestTestCase
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
        self.player.points_balance = 1_000
        self.player.save(update_fields=['points_balance'])
        ContestEnrollment.objects.create(contest=contest, user=self.player, points_cost=contest.entry_points_cost)
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
        self.assertContains(response, '返回竞赛')
        self.assertContains(response, reverse('contest_detail', args=[contest.id]))

    def test_finished_contest_copies_normal_problem_and_origin_tag_once(self):
        contest, item = self.create_contest_problem(end_delta=-timedelta(minutes=1))
        contest.publish_finished_problems()
        item.refresh_from_db()
        contest.refresh_from_db()
        self.assertIsNotNone(item.published_problem)
        self.assertTrue(item.published_problem.is_public)
        self.assertIn('contest:Contest-0', item.published_problem.tags.split())
        self.assertEqual(item.published_problem.test_cases.count(), 3)
        first_published_problem = item.published_problem_id
        self.assertFalse(contest.publish_finished_problems())
        item.refresh_from_db()
        self.assertEqual(item.published_problem_id, first_published_problem)

    def test_attempt_cap_rejects_extra_contest_submission(self):
        contest, item = self.create_contest_problem(limit=1)
        self.player.points_balance = 1_000
        self.player.save(update_fields=['points_balance'])
        ContestEnrollment.objects.create(contest=contest, user=self.player, points_cost=contest.entry_points_cost)
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

    def test_accepted_contest_problem_rejects_future_submissions(self):
        contest, item = self.create_contest_problem(limit=3)
        Submission.objects.create(
            user=self.player,
            contest_problem=item,
            code='puts 1',
            language='Ruby',
            status='Accepted',
        )
        self.player.points_balance = 1_000
        self.player.save(update_fields=['points_balance'])
        ContestEnrollment.objects.create(contest=contest, user=self.player, points_cost=contest.entry_points_cost)
        self.assertTrue(self.client.login(username='player', password='password'))
        question_url = reverse('contest_question', args=[contest.id, item.id])
        response = self.client.get(question_url)
        self.assertContains(response, '本题已全部通过，无需再次提交')
        self.assertNotContains(response, 'id="monaco-editor"', html=False)
        submit_url = reverse('submit_contest_solution', args=[contest.id, item.id])
        response = self.client.post(submit_url, {'language': 'Ruby', 'code': 'puts 2'}, follow=True)
        self.assertContains(response, '本题已全部通过，无需再次提交')
        self.assertEqual(Submission.objects.filter(user=self.player, contest_problem=item).count(), 1)

    def test_standings_uses_case_result_formula(self):
        contest, item = self.create_contest_problem()
        submission = Submission.objects.create(user=self.player, contest_problem=item, code='puts 1', language='Ruby')
        first_case, second_case = item.test_cases.all()[:2]
        SubmissionTestResult.objects.create(submission=submission, contest_test_case=first_case, case_index=0, status='Accepted')
        SubmissionTestResult.objects.create(submission=submission, contest_test_case=second_case, case_index=1, status='Wrong Answer')
        response = self.client.get(reverse('contest_standings', args=[contest.id]))
        self.assertContains(response, self.player.username)
        self.assertContains(response, '>1<')

    def test_standings_only_scores_latest_submission_per_problem(self):
        contest, item = self.create_contest_problem()
        first_case, second_case = item.test_cases.all()[:2]
        older = Submission.objects.create(user=self.player, contest_problem=item, code='puts 1', language='Ruby')
        SubmissionTestResult.objects.create(submission=older, contest_test_case=first_case, case_index=0, status='Accepted')
        SubmissionTestResult.objects.create(submission=older, contest_test_case=second_case, case_index=1, status='Accepted')
        latest = Submission.objects.create(user=self.player, contest_problem=item, code='puts 2', language='Ruby')
        SubmissionTestResult.objects.create(submission=latest, contest_test_case=first_case, case_index=0, status='Accepted')
        SubmissionTestResult.objects.create(submission=latest, contest_test_case=second_case, case_index=1, status='Wrong Answer')

        response = self.client.get(reverse('contest_standings', args=[contest.id]))
        self.assertContains(response, self.player.username)
        self.assertContains(response, '>1<')
        self.assertContains(response, '>0.5<')

    def test_public_contest_pages_do_not_offer_authoring_routes(self):
        contest, _item = self.create_contest_problem()
        response = self.client.get(reverse('contest_list'))
        self.assertNotContains(response, '创建竞赛')
        response = self.client.get(reverse('contest_detail', args=[contest.id]))
        self.assertNotContains(response, '添加题目')
        self.assertEqual(self.client.get('/contests/create/').status_code, 404)
        self.assertEqual(self.client.get(f'/contests/{contest.id}/questions/add/').status_code, 404)

    def test_finished_contest_preserves_standings(self):
        contest, item = self.create_contest_problem()
        test_case = item.test_cases.first()
        submission = Submission.objects.create(
            user=self.player, contest_problem=item, code='puts 1', language='Ruby',
        )
        SubmissionTestResult.objects.create(
            submission=submission, contest_test_case=test_case, case_index=0, status='Accepted',
        )
        contest.end_at = timezone.now() - timedelta(seconds=1)
        contest.save(update_fields=['end_at'])
        contest.publish_finished_problems()

        response = self.client.get(reverse('contest_standings', args=[contest.id]))
        self.assertContains(response, self.player.username)
        self.assertContains(response, '>1.5<')

    def test_partial_publication_is_repaired_without_duplicate_problem(self):
        contest, first_item = self.create_contest_problem(end_delta=-timedelta(minutes=1))
        second_item = ContestProblem.objects.create(
            contest=contest, title='Second question', description='Description',
            input_format='Input', output_format='Output', created_by=self.creator, order=1,
        )
        ContestTestCase.objects.create(contest_problem=second_item, input_data='1', expected_output='1')
        contest.publish_finished_problems()
        first_item.refresh_from_db()
        second_item.refresh_from_db()
        first_published_id = first_item.published_problem_id
        self.assertIsNotNone(first_published_id)
        self.assertIsNotNone(second_item.published_problem_id)

        second_item.published_problem = None
        second_item.save(update_fields=['published_problem'])
        contest.published_at = timezone.now()
        contest.save(update_fields=['published_at'])
        contest.publish_finished_problems()
        first_item.refresh_from_db()
        second_item.refresh_from_db()
        self.assertEqual(first_item.published_problem_id, first_published_id)
        self.assertIsNotNone(second_item.published_problem_id)
        self.assertNotEqual(second_item.published_problem_id, first_published_id)


        contest, item = self.create_contest_problem()
        request = RequestFactory().post('/admin/contests/contest/')
        request.user = self.creator
        admin_instance = ContestAdmin(Contest, AdminSite())
        admin_instance.message_user = lambda *args, **kwargs: None
        admin_instance.end_selected_contests(request, Contest.objects.filter(pk=contest.pk))
        contest.refresh_from_db()
        item.refresh_from_db()
        self.assertTrue(contest.is_finished)
        self.assertIsNotNone(contest.published_at)
        self.assertIsNotNone(item.published_problem)
