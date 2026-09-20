"""ASGI entry point.

Run by the standalone WebSocket service (Granian ``--interface asgi``,
see ``guwu-oj-ws.service``): HTTP requests fall through to Django's
standard ASGI handler; ``/ws/submissions/<id>/status/`` is served by the
raw ASGI WebSocket consumer in ``submissions.ws``. The main site keeps
running on Granian WSGI — this module is never loaded there.
"""

import os
import re

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')
django.setup()

from django.core.asgi import get_asgi_application

from submissions.ws import submission_status_consumer

django_asgi_app = get_asgi_application()

_WS_ROUTES = [
    (
        re.compile(r'^/ws/submissions/(?P<submission_id>\d+)/status/?$'),
        submission_status_consumer,
    ),
]


async def application(scope, receive, send):
    if scope['type'] == 'http':
        await django_asgi_app(scope, receive, send)
        return

    if scope['type'] == 'websocket':
        path = scope.get('path', '')
        for pattern, consumer in _WS_ROUTES:
            match = pattern.match(path)
            if match:
                await consumer(
                    scope,
                    receive,
                    send,
                    submission_id=int(match.group('submission_id')),
                )
                return
        await send({'type': 'websocket.close', 'code': 4404})
        return
