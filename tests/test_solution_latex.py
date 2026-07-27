from django.test import TestCase
from django.urls import reverse

from problems.markdown_utils import render_markdown
from problems.models import Problem, Solution
from users.models import User


class SolutionLatexTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='solution-author',
            email='solution-author@example.com',
            password='password',
        )
        self.problem = Problem.objects.create(
            title='LaTeX test problem',
            description='Description',
            input_format='Input',
            output_format='Output',
            created_by=self.author,
        )

    def test_solution_detail_loads_mathjax_for_markdown_content(self):
        solution = Solution.objects.create(
            problem=self.problem,
            author=self.author,
            title='Formula solution',
            content='**Square:** $x^2$\n\n$$\\sum_{i=1}^n i$$',
            is_approved=True,
        )

        response = self.client.get(
            reverse('solution_detail', args=[self.problem.id, solution.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<strong>Square:</strong>', html=True)
        self.assertContains(response, '$x^2$')
        self.assertContains(response, '$$\\sum_{i=1}^n i$$')
        self.assertContains(response, 'problem-mathjax.js')
        self.assertContains(response, 'tex-chtml-full.js')

    def test_markdown_renderer_preserves_latex_delimiters(self):
        html = render_markdown('**Area:** $a^2$\n\n$$\\frac{a}{b}$$')

        self.assertIn('<strong>Area:</strong>', html)
        self.assertIn('$a^2$', html)
        self.assertIn('$$\\frac{a}{b}$$', html)
