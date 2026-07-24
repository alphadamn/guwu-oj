"""Local GeoLite2 country lookup with graceful degradation."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _reader():
    configured_path = getattr(settings, 'GEOIP2_COUNTRY_DB', '')
    candidates = [configured_path] if configured_path else []
    candidates.extend([
        os.path.join(settings.BASE_DIR, 'GeoLite2-Country.mmdb'),
        os.path.join(settings.BASE_DIR, 'data', 'GeoLite2-Country.mmdb'),
    ])
    path = next((candidate for candidate in candidates if candidate and os.path.isfile(candidate)), None)
    if not path:
        logger.info('GeoLite2 country database is not installed')
        return None
    try:
        from geoip2.database import Reader
        return Reader(path)
    except Exception:
        logger.warning('Unable to open GeoLite2 database', exc_info=True)
        return None


@lru_cache(maxsize=1)
def _centroids():
    path = os.path.join(settings.BASE_DIR, 'devlog', 'country_centroids.json')
    try:
        with open(path, encoding='utf-8') as source:
            return json.load(source)
    except (OSError, ValueError):
        return {}


def _country_coordinates(code):
    """Return a stable map coordinate for a GeoLite2 country code."""
    coordinates = _centroids().get(code)
    if coordinates:
        return coordinates
    # GeoLite2 can return codes for territories not represented by the map's
    # country layer. Keep those requests visible as source markers rather than
    # dropping the aggregate solely because a polygon is unavailable.
    return {
        'MO': [113.54, 22.20],
        'SG': [103.82, 1.35],
        'HK': [114.17, 22.32],
        'TW': [120.96, 23.70],
    }.get(code)


def _country_for_ip(value):
    address = ipaddress.ip_address(value)
    if address.is_private or address.is_loopback or address.is_reserved:
        return None
    reader = _reader()
    if reader is None:
        return None
    country = reader.country(str(address)).country
    code = country.iso_code
    coordinates = _country_coordinates(code) if code else None
    if not code or not coordinates:
        logger.info('No map coordinate for GeoLite2 country code %s', code)
        return None
    return {
        'country_code': code,
        'country_name': country.name or code,
        'latitude': float(coordinates[1]),
        'longitude': float(coordinates[0]),
    }


def country_for_request(request):
    try:
        from users.captcha import _client_ip
        return _country_for_ip(_client_ip(request))
    except Exception:
        return None


def server_location():
    try:
        configured = getattr(settings, 'OJ_SERVER_IP', '')
        return _country_for_ip(configured) if configured else None
    except Exception:
        return None


def clear_reader_cache():
    _reader.cache_clear()
    _centroids.cache_clear()
