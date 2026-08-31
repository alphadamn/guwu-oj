from datetime import timedelta
from unittest.mock import patch

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from devlog.models import (
    ServiceComponent,
    TrafficBrowserLocationMetric,
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

    def test_dashboard_prefers_browser_locations(self):
        today = timezone.localdate()
        TrafficCountryMetric.objects.create(
            day=today, country_code='FR', country_name='France',
            latitude=46.2, longitude=2.2, requests=9,
        )
        TrafficBrowserLocationMetric.objects.create(
            day=today, latitude='31.2', longitude='121.5', requests=3,
        )
        self.client.force_login(self.staff)

        data = self.client.get(reverse('admin:dashboard_metrics')).json()

        self.assertEqual(data['location_source'], 'browser')
        self.assertEqual(data['locations'][0]['country_code'], 'CN')
        self.assertEqual(data['locations'][0]['country_name'], 'China')
        self.assertEqual(data['location_list'], [{
            'country_code': 'CN', 'country_name': 'China', 'requests': 3,
        }])

    def test_browser_country_resolution_is_available(self):
        from devlog.geoip import country_for_coordinates

        self.assertEqual(country_for_coordinates(31.2, 121.5)['country_code'], 'CN')

    def test_dashboard_uses_ip_locations_when_browser_mode_is_disabled(self):
        from devlog.models import SiteConfig

        today = timezone.localdate()
        SiteConfig.objects.create(browser_geolocation_enabled=False)
        TrafficCountryMetric.objects.create(
            day=today, country_code='FR', country_name='France',
            latitude=46.2, longitude=2.2, requests=9,
        )
        TrafficBrowserLocationMetric.objects.create(
            day=today, latitude='31.2', longitude='121.5', requests=3,
        )
        self.client.force_login(self.staff)

        data = self.client.get(reverse('admin:dashboard_metrics')).json()

        self.assertEqual(data['location_source'], 'ip')
        self.assertEqual(data['location_mode'], 'ip')
        self.assertEqual(data['locations'][0]['country_code'], 'FR')

    def test_dashboard_location_mode_toggle_updates_site_config(self):
        from devlog.models import SiteConfig

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('admin:dashboard_location_mode'),
            data=json.dumps({'mode': 'ip'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['mode'], 'ip')
        self.assertFalse(SiteConfig.objects.get(pk=1).browser_geolocation_enabled)

    def test_dashboard_location_mode_updates_existing_singleton_row(self):
        from devlog.models import SiteConfig

        config = SiteConfig.objects.create(pk=9, browser_geolocation_enabled=True)
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('admin:dashboard_location_mode'),
            data=json.dumps({'mode': 'ip'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        config.refresh_from_db()
        self.assertFalse(config.browser_geolocation_enabled)
        self.assertFalse(SiteConfig.objects.filter(pk=1).exists())
        self.assertFalse(SiteConfig.browser_geolocation_is_enabled())

    def test_admin_homepage_includes_location_mode_toggle(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse('admin:index'))

        self.assertContains(response, 'oj-dashboard-location-toggle')
        self.assertContains(response, 'oj-dashboard-location-error')
        self.assertContains(response, 'oj-dashboard.js')

    def test_dashboard_location_mode_rejects_unknown_mode(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse('admin:dashboard_location_mode'),
            data=json.dumps({'mode': 'unknown'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)

    def test_dashboard_location_mode_accepts_admin_csrf_token(self):
        from devlog.models import SiteConfig

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        homepage = client.get(reverse('admin:index'))
        csrf_token = homepage.cookies['csrftoken'].value

        response = client.post(
            reverse('admin:dashboard_location_mode'),
            data=json.dumps({'mode': 'ip'}),
            content_type='application/json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['mode'], 'ip')
        self.assertFalse(SiteConfig.objects.get(pk=1).browser_geolocation_enabled)


class TrafficMetricsMiddlewareTests(TestCase):
    @patch('devlog.views._check_judging_system', return_value=3)
    @patch('django_redis.get_redis_connection')
    def test_redis_health_probe_uses_direct_ping(self, get_connection, _judge_check):
        from devlog.views import _do_refresh_auto_components

        component = ServiceComponent.objects.create(
            name='Redis', auto_check=True, health_key='redis'
        )
        client = get_connection.return_value
        client.get.return_value = b'1'

        _do_refresh_auto_components(force_refresh=True)

        component.refresh_from_db()
        client.ping.assert_called_once()
        self.assertEqual(component.status, ServiceComponent.STATUS_OPERATIONAL)

    @patch('django_redis.get_redis_connection')
    def test_failed_redis_health_probe_is_major(self, get_connection):
        from devlog.views import _do_refresh_auto_components

        component = ServiceComponent.objects.create(
            name='Redis', auto_check=True, health_key='redis'
        )
        get_connection.return_value.ping.side_effect = ConnectionError('unavailable')

        with patch('devlog.views._check_judging_system', return_value=3):
            _do_refresh_auto_components(force_refresh=True)

        component.refresh_from_db()
        self.assertEqual(component.status, ServiceComponent.STATUS_MAJOR)


class TrafficMetricsMiddlewareTests(TestCase):
    def setUp(self):
        self.author = get_user_model().objects.create_user(
            username='traffic-author', email='traffic@example.com', password='pass'
        )
        self.problem = Problem.objects.create(
            title='Traffic problem', description='d', input_format='i', output_format='o',
            created_by=self.author,
        )

    def test_browser_location_is_preferred_over_ip(self):
        with patch('devlog.geoip._country_for_ip') as country_for_ip:
            country_for_ip.return_value = {
                'country_code': 'FR', 'country_name': 'France',
                'latitude': 46.2, 'longitude': 2.2,
            }
            self.client.cookies['oj_analytics_consent'] = 'accepted'
            session = self.client.session
            session['oj_browser_location'] = '31.2,121.5'
            session.save()
            self.client.get('/')

        metric = TrafficBrowserLocationMetric.objects.get(
            day=timezone.localdate(), latitude='31.2', longitude='121.5'
        )
        self.assertEqual(metric.requests, 1)
        self.assertFalse(TrafficCountryMetric.objects.exists())
        country_for_ip.assert_not_called()

    def test_invalid_browser_location_falls_back_to_ip(self):
        with patch('devlog.geoip._country_for_ip') as country_for_ip:
            country_for_ip.return_value = {
                'country_code': 'FR', 'country_name': 'France',
                'latitude': 46.2, 'longitude': 2.2,
            }
            # An out-of-range session value (e.g. a corrupt or hand-edited
            # session store) must be ignored so the country fallback takes over.
            session = self.client.session
            session['oj_browser_location'] = '91,181'
            session.save()
            self.client.get('/')

        self.assertTrue(TrafficCountryMetric.objects.filter(country_code='FR').exists())
        self.assertFalse(TrafficBrowserLocationMetric.objects.exists())
        country_for_ip.assert_called_once()

    def test_record_browser_location_requires_consent_and_rounds_coordinates(self):
        url = reverse('devlog:record_browser_location')
        response = self.client.post(
            url, data=json.dumps({'latitude': 31.234, 'longitude': 121.567}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

        self.client.cookies['oj_analytics_consent'] = 'accepted'
        response = self.client.post(
            url, data=json.dumps({'latitude': 31.234, 'longitude': 121.567}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        metric = TrafficBrowserLocationMetric.objects.get(day=timezone.localdate())
        self.assertEqual((float(metric.latitude), float(metric.longitude)), (31.2, 121.6))
        # The location is stored server-side in the session, not in a cookie.
        self.assertEqual(response.json()['location'], '31.2,121.6')
        self.assertNotIn('oj_browser_location', response.cookies)
        session = self.client.session
        self.assertEqual(session['oj_browser_location'], '31.2,121.6')

    def test_record_browser_location_rejects_invalid_coordinates(self):
        self.client.cookies['oj_analytics_consent'] = 'accepted'
        response = self.client.post(
            reverse('devlog:record_browser_location'),
            data=json.dumps({'latitude': 91, 'longitude': 181}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)


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

