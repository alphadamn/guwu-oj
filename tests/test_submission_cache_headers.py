"""Submission views must never be cached.

A shared reverse proxy previously stored `/submissions/detail/<id>/` and the
status API for a day, so a page rendered while a submission was still
``Pending`` kept being replayed after the judge had already recorded
``Accepted``. These views now declare themselves uncacheable.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from problems.models import Problem, TestCase as ProblemTestCase
from submissions.models import Submission


class SubmissionCacheHeaderTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="cache-header-user",
            password="safe-test-password",
        )
        self.problem = Problem.objects.create(
            title="Cache headers",
            description="",
            input_format="",
            output_format="",
            time_limit=1000,
            memory_limit=256,
            created_by=self.user,
        )
        ProblemTestCase.objects.create(
            problem=self.problem,
            input_data="",
            expected_output="",
        )
        self.submission = Submission.objects.create(
            problem=self.problem,
            user=self.user,
            language="C++",
            code="int main(){}",
            status="Pending",
        )
        self.client.force_login(self.user)

    def _assert_no_store(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        cache_control = response.headers.get("Cache-Control", "")
        self.assertIn("no-store", cache_control, msg=f"{url} -> {cache_control!r}")
        self.assertIn("max-age=0", cache_control, msg=f"{url} -> {cache_control!r}")

    def test_detail_page_is_not_cacheable(self):
        self._assert_no_store(
            reverse("submission_detail", args=[self.submission.id])
        )

    def test_status_api_is_not_cacheable(self):
        self._assert_no_store(
            reverse("submission_status_api", args=[self.submission.id])
        )

    def test_submission_lists_are_not_cacheable(self):
        self._assert_no_store(reverse("my_submissions"))
        self._assert_no_store(reverse("all_submissions"))

    def test_status_api_reports_terminal_status_after_judging(self):
        """The API must report the stored verdict, not a stale Pending."""
        self.submission.status = "Accepted"
        self.submission.save(update_fields=["status"])

        response = self.client.get(
            reverse("submission_status_api", args=[self.submission.id])
        )
        payload = response.json()
        self.assertEqual(payload["status"], "Accepted")
        self.assertTrue(payload["done"])
