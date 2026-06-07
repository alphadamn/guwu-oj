from django.test import TestCase
from problems.models import Problem
from users.models import User
from submissions.models import Submission


class ProblemModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.problem = Problem.objects.create(
            title='Test Problem',
            description='Test description',
            input_format='Test input',
            output_format='Test output',
            sample_input='1 2',
            sample_output='3',
            time_limit=1000,
            memory_limit=256,
            difficulty='入门',
            created_by=self.user
        )

    def test_problem_creation(self):
        self.assertEqual(self.problem.title, 'Test Problem')
        self.assertEqual(self.problem.difficulty, '入门')
        self.assertTrue(self.problem.is_public)

    def test_problem_str(self):
        self.assertEqual(str(self.problem), 'Test Problem')


class UserModelTest(TestCase):
    def test_user_creation(self):
        user = User.objects.create_user(
            username='testuser2',
            email='test2@example.com',
            password='testpass123'
        )
        self.assertEqual(user.username, 'testuser2')
        self.assertTrue(user.check_password('testpass123'))


class SubmissionModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.problem = Problem.objects.create(
            title='Test Problem',
            description='Test description',
            input_format='Test input',
            output_format='Test output',
            sample_input='1 2',
            sample_output='3',
            time_limit=1000,
            memory_limit=256,
            difficulty='入门',
            created_by=self.user
        )

    def test_submission_creation(self):
        submission = Submission.objects.create(
            problem=self.problem,
            user=self.user,
            code='print("hello")',
            language='Python',
            status='Accepted'
        )
        self.assertEqual(submission.status, 'Accepted')
        self.assertEqual(submission.language, 'Python')
