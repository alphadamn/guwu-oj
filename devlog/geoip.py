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


@lru_cache(maxsize=1)
def _world_features():
    path = os.path.join(settings.BASE_DIR, 'static', 'admin', 'data', 'world-countries.geojson')
    try:
        with open(path, encoding='utf-8') as source:
            return json.load(source).get('features', [])
    except (OSError, ValueError, TypeError):
        return []


def _point_in_ring(longitude, latitude, ring):
    inside = False
    for index, point in enumerate(ring):
        previous = ring[index - 1]
        x1, y1 = point[0], point[1]
        x2, y2 = previous[0], previous[1]
        if (y1 > latitude) != (y2 > latitude):
            crossing = (x2 - x1) * (latitude - y1) / ((y2 - y1) or 1e-12) + x1
            if longitude < crossing:
                inside = not inside
    return inside


def _point_in_polygon(longitude, latitude, polygon):
    return bool(polygon) and _point_in_ring(longitude, latitude, polygon[0]) and not any(
        _point_in_ring(longitude, latitude, hole) for hole in polygon[1:]
    )


@lru_cache(maxsize=4096)
def _country_for_coordinates(latitude, longitude):
    """Resolve coarse browser coordinates to the bundled country's ISO code.

    Inputs are rounded to one decimal degree upstream, so the cache hit rate is
    high; without it every dashboard render re-scans the whole world GeoJSON
    once per aggregated point.
    """
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    for feature in _world_features():
        properties = feature.get('properties') or {}
        code = properties.get('ISO_A2_EH') or properties.get('ISO_A2')
        if not isinstance(code, str) or len(code) != 2 or code == '-99':
            continue
        geometry = feature.get('geometry') or {}
        polygons = [geometry.get('coordinates')] if geometry.get('type') == 'Polygon' else geometry.get('coordinates', [])
        if any(_point_in_polygon(longitude, latitude, polygon) for polygon in polygons):
            return {
                'country_code': code,
                'country_name': properties.get('NAME') or properties.get('NAME_EN') or code,
            }
    return None


def country_for_coordinates(latitude, longitude):
    """Cached point-in-country lookup for coarse browser coordinates.

    Coordinates arrive as ``Decimal`` or ``float`` rounded to one decimal
    degree; normalising to a rounded float keeps both callers on the same cache
    key.  A copy is returned so callers cannot mutate the cached value.
    """
    try:
        key = (round(float(latitude), 1), round(float(longitude), 1))
    except (TypeError, ValueError):
        return None
    result = _country_for_coordinates(*key)
    return dict(result) if result else None


def _country_coordinates(code):
    """Return a stable map coordinate for a GeoLite2 country code."""
    coordinates = _centroids().get(code)
    if coordinates:
        return coordinates
    return {
        'MO': [113.54, 22.20], 'SG': [103.82, 1.35],
        'HK': [114.17, 22.32], 'TW': [120.96, 23.70],
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
    _world_features.cache_clear()
    _country_for_coordinates.cache_clear()
