from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from contests.models import Contest, ContestEnrollment
from points.models import PointConfig, PointLedgerEntry
from problems.models import Problem, TestCase as ProblemTestCase
from submissions.judge import finalize_submission
from submissions.models import Submission, SubmissionTestResult
from users.models import User


class PointFeatureTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user('creator', 'creator@example.com', 'password')
        self.player = User.objects.create_user('player', 'player@example.com', 'password')
        self.config = PointConfig.get_solo()

    def test_referral_link_binds_users_and_credits_both_sides(self):
        self.config.inviter_registration_points = 11
        self.config.invitee_registration_points = 7
        self.config.save()
        from devlog.models import RegistrationConfig
        registration_config, _ = RegistrationConfig.objects.get_or_create(pk=1)
        registration_config.email_verification_required = False
        registration_config.save(update_fields=['email_verification_required'])
        from devlog.models import CaptchaConfig
        captcha_config, _ = CaptchaConfig.objects.get_or_create(pk=1)
        captcha_config.captcha_on_register = False
        captcha_config.save(update_fields=['captcha_on_register'])

        response = self.client.post(reverse('register'), {
            'username': 'invitee', 'email': 'invitee@example.com', 'nickname': '',
            'referral_code': self.creator.referral_code,
            'password1': 'SafePassword123!', 'password2': 'SafePassword123!',
        })
        self.assertRedirects(response, reverse('home'))
        invitee = User.objects.get(username='invitee')
        self.creator.refresh_from_db()
        invitee.refresh_from_db()
        self.assertEqual(invitee.referrer, self.creator)
        self.assertEqual(self.creator.points_balance, Decimal('11.00'))
        self.assertEqual(invitee.points_balance, Decimal('7.00'))
        self.assertEqual(PointLedgerEntry.objects.filter(event_type__startswith='referral_').count(), 2)

    def test_first_accepted_normal_testcase_rewards_fractional_points_once(self):
        self.config.accepted_testcase_points = Decimal('0.1234')
        self.config.save()
        problem = Problem.objects.create(
            title='Points problem', description='d', input_format='i', output_format='o',
            created_by=self.creator, is_public=True,
        )
        testcase = ProblemTestCase.objects.create(
            problem=problem, input_data='1', expected_output='1', is_sample=False,
        )
        for code in ('first', 'second'):
            submission = Submission.objects.create(
                user=self.player, problem=problem, code=code, language='Python',
            )
            SubmissionTestResult.objects.create(
                submission=submission, test_case=testcase, case_index=1, status='Accepted',
            )
            finalize_submission(submission, ['Accepted'], 1, 1, problem)
        self.player.refresh_from_db()
        self.assertEqual(self.player.points_balance, Decimal('0.1234'))
        entry = PointLedgerEntry.objects.get(event_type='accepted_testcase')
        self.assertEqual(entry.amount, Decimal('0.1234'))
        self.assertEqual(PointLedgerEntry.objects.filter(event_type='accepted_testcase').count(), 1)

    def test_contest_join_debits_once_and_gates_problem_access(self):
        self.player.points_balance = Decimal('100.00')
        self.player.save(update_fields=['points_balance'])
        contest = Contest.objects.create(
            name='Paid contest', creator=self.creator,
            start_at=timezone.now() - timedelta(minutes=2),
            end_at=timezone.now() + timedelta(minutes=30),
            entry_points_cost=50,
        )
        self.assertTrue(self.client.login(username='player', password='password'))
        response = self.client.post(reverse('join_contest', args=[contest.id]))
        self.assertRedirects(response, reverse('contest_detail', args=[contest.id]))
        response = self.client.post(reverse('join_contest', args=[contest.id]))
        self.assertRedirects(response, reverse('contest_detail', args=[contest.id]))
        self.player.refresh_from_db()
        self.assertEqual(self.player.points_balance, Decimal('50.00'))
        self.assertEqual(ContestEnrollment.objects.filter(contest=contest, user=self.player).count(), 1)
        self.assertEqual(PointLedgerEntry.objects.filter(event_type='contest_entry').count(), 1)

    def test_contest_join_rejects_insufficient_points(self):
        contest = Contest.objects.create(
            name='Expensive contest', creator=self.creator,
            start_at=timezone.now() - timedelta(minutes=2),
            end_at=timezone.now() + timedelta(minutes=30),
        )
        self.assertEqual(contest.entry_points_cost, 50)
        self.assertTrue(self.client.login(username='player', password='password'))
        response = self.client.post(reverse('join_contest', args=[contest.id]), follow=True)
        self.assertContains(response, '积分不足')

    def test_points_center_requires_login_and_shows_only_own_ledger(self):
        url = reverse('points_center')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)

        self.player.points_balance = Decimal('23.50')
        self.player.save(update_fields=['points_balance'])
        PointLedgerEntry.objects.create(
            user=self.player, amount=Decimal('23.50'), balance_after=Decimal('23.50'),
            event_type='manual_test', event_key='player', description='玩家积分记录',
        )
        PointLedgerEntry.objects.create(
            user=self.creator, amount=Decimal('99.00'), balance_after=Decimal('99.00'),
            event_type='manual_test', event_key='creator', description='其他用户私密记录',
        )
        self.assertTrue(self.client.login(username='player', password='password'))
        response = self.client.get(url)
        self.assertContains(response, '积分中心')
        self.assertContains(response, '23')
        self.assertContains(response, '玩家积分记录')
        self.assertNotContains(response, '其他用户私密记录')
        self.assertContains(response, f'/users/register/?ref={self.player.referral_code}')
