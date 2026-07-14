"""Template tags for per-user feature-ban checks."""
from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def feature_disabled(user, feature: str) -> bool:
    """Return True when ``user`` has ``feature`` explicitly disabled and
    the deadline (if any) is still in the future.

    Usage in templates::

        {% if user|feature_disabled:"submit" %} ... {% endif %}
        {% if user|feature_disabled:"create_problem" %} ... {% endif %}
    """
    if user is None:
        return False
    if not getattr(user, 'is_authenticated', False):
        return False
    fn = getattr(user, 'feature_disabled', None)
    if not callable(fn):
        return False
    try:
        return bool(fn(str(feature) if feature is not None else ''))
    except Exception:
        return False


@register.filter
def feature_enabled(user, feature: str) -> bool:
    """Convenience: ``not (user|feature_disabled:"feature")``."""
    return not feature_disabled(user, feature)
