from datetime import timedelta
from unittest.mock import patch

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from devlog.models import (
    ServiceComponent,
    TrafficCountryMetric,
    TrafficDailyMetric,
    TrafficPageMetric,
)
from problems.models import Problem
from submissions.models import Submission


class DashboardMetricsTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_superuser(
            username='dashboard-admin', email='dashboard@example.com', password='pass'
        )
        self.author = get_user_model().objects.create_user(
            username='author', email='author@example.com', password='pass'
        )
        self.problem = Problem.objects.create(
            title='Dashboard problem', description='d', input_format='i', output_format='o',
            created_by=self.author,
        )

    def test_dashboard_metrics_requires_admin_access(self):
        response = self.client.get(reverse('admin:dashboard_metrics'))
        self.assertEqual(response.status_code, 302)

    def test_dashboard_metrics_returns_zero_filled_series_and_totals(self):
        Submission.objects.create(
            problem=self.problem, user=self.author, code='int main() {}',
            language='C++', status='Accepted',
        )
        ServiceComponent.objects.create(name='数据库', status='operational')
        day = timezone.localdate() - timedelta(days=2)
        TrafficDailyMetric.objects.create(day=day, page_views=7)
        self.client.force_login(self.staff)

        response = self.client.get(reverse('admin:dashboard_metrics'))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['labels']), 14)
        self.assertEqual(len(data['traffic']), 14)
        self.assertEqual(len(data['submissions']), 14)
        self.assertIn(7, data['traffic'])
        self.assertTrue(data['traffic_has_data'])
        self.assertEqual(data['traffic_started_at'], day.isoformat())
        self.assertFalse(data['page_ranking_has_data'])
        self.assertEqual(data['top_pages'], [])
        self.assertEqual(data['top_problems'], [
            {'submissions': 1, 'id': self.problem.id, 'title': self.problem.title},
        ])
        self.assertEqual(data['summary']['users'], 2)
        self.assertEqual(data['summary']['problems'], 1)
        self.assertEqual(data['summary']['submissions'], 1)
        self.assertEqual(data['summary']['acceptance_rate'], 100.0)
        self.assertEqual(data['summary']['health'], '1/1 正常')

    def test_dashboard_metrics_marks_traffic_as_unavailable_before_first_view(self):
        self.client.force_login(self.staff)
        data = self.client.get(reverse('admin:dashboard_metrics')).json()

        self.assertFalse(data['traffic_has_data'])
        self.assertIsNone(data['traffic_started_at'])
        self.assertEqual(data['traffic'], [0] * 14)

    def test_dashboard_metrics_includes_country_aggregates(self):
        today = timezone.localdate()
        TrafficCountryMetric.objects.create(
            day=today, country_code='SG', country_name='Singapore',
            latitude=1.35, longitude=103.82, requests=4,
        )
        self.client.force_login(self.staff)

        data = self.client.get(reverse('admin:dashboard_metrics')).json()

        self.assertEqual(data['locations'], [{
            'country_code': 'SG', 'country_name': 'Singapore',
            'latitude': 1.35, 'longitude': 103.82, 'requests': 4,
        }])
        self.assertTrue(data['location_has_data'])


class TrafficMetricsMiddlewareTests(TestCase):
    def setUp(self):
        self.author = get_user_model().objects.create_user(
            username='traffic-author', email='traffic@example.com', password='pass'
        )
        self.problem = Problem.objects.create(
            title='Traffic problem', description='d', input_format='i', output_format='o',
            created_by=self.author,
        )

    def test_public_page_is_counted_but_admin_and_health_are_excluded(self):
        self.client.get('/')
        today = timezone.localdate()
        self.assertEqual(TrafficDailyMetric.objects.get(day=today).page_views, 1)
        self.assertEqual(
            TrafficPageMetric.objects.get(day=today, path='/').page_views, 1
        )

        self.client.get(f'/problem/{self.problem.id}/?page=2')
        self.assertEqual(
            TrafficPageMetric.objects.get(day=today, path='/problem/:id/').page_views, 1
        )

        self.client.get('/admin/login/')
        self.client.get('/health/')
        self.assertEqual(TrafficDailyMetric.objects.get(day=today).page_views, 2)

    @patch('devlog.geoip._country_for_ip')
    def test_forwarded_public_ip_is_recorded_as_country(self, country_for_ip):
        country_for_ip.return_value = {
            'country_code': 'SG', 'country_name': 'Singapore',
            'latitude': 1.35, 'longitude': 103.82,
        }

        self.client.get('/', HTTP_X_FORWARDED_FOR='43.160.219.206')

        metric = TrafficCountryMetric.objects.get(
            day=timezone.localdate(), country_code='SG'
        )
        self.assertEqual(metric.requests, 1)
        country_for_ip.assert_called_once_with('43.160.219.206')


class GeoIpVisibilityTests(TestCase):
    def test_all_geojson_iso_codes_have_centroids(self):
        centroids = json.loads(Path('devlog/country_centroids.json').read_text())
        world = json.loads(Path('static/admin/data/world-countries.geojson').read_text())
        codes = {
            (feature.get('properties') or {}).get('ISO_A2_EH')
            or (feature.get('properties') or {}).get('ISO_A2')
            for feature in world['features']
        }
        self.assertTrue({code for code in codes if isinstance(code, str) and len(code) == 2} <= centroids.keys())
        self.assertIn('DE', centroids)
        self.assertIn('FR', centroids)
        self.assertIn('NO', centroids)

    @patch('devlog.geoip._reader')
    def test_country_without_centroid_is_still_visible(self, reader):
        from devlog.geoip import _country_for_ip, clear_reader_cache

        class Country:
            iso_code = 'DE'
            name = 'Germany'

        class Response:
            country = Country()

        reader.return_value.country.return_value = Response()
        clear_reader_cache()
        try:
            result = _country_for_ip('45.135.194.31')
        finally:
            clear_reader_cache()

        self.assertEqual(result['country_code'], 'DE')
        self.assertGreater(result['latitude'], 0)
        self.assertGreater(result['longitude'], 0)
        reader.return_value.country.assert_called_once_with('45.135.194.31')

