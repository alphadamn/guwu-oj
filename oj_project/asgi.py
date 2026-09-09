import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')

_django_app = get_asgi_application()


async def application(scope, receive, send):
    """ASGI entrypoint with HTTP/3 compatibility normalization.

    Two Hypercorn/aioquic (HTTP/3) quirks are papered over here:

    1. Response header names must be lower-case. Django preserves the
       WSGI-style case (e.g. "Content-Type"); this is legal over HTTP/1.1
       and normalized by the h2 library for HTTP/2, but HTTP/3 strictly
       requires lower-case header names and aioquic does not normalize
       them -- the h3 client then rejects the response with
       "header field is not lower-case".

    2. A duplicate/stray ``http.request`` message can arrive on the
       receive channel AFTER the final (more_body=False) body message --
       observed when an h3 stream is reset/aborted (client cancels, or
       Cloudflare observatory probes time out). Django's ASGI handler,
       while listening for disconnect after consuming the body, asserts
       ("Invalid ASGI message after request body") and aborts the
       request with an "Error in ASGI Framework" traceback, leaving the
       request task in a desync state. Such redundant body messages are
       dropped here.
    """
    if scope['type'] != 'http':
        await _django_app(scope, receive, send)
        return

    body_complete = False

    async def _receive():
        nonlocal body_complete
        while True:
            message = await receive()
            if message['type'] == 'http.request':
                if body_complete:
                    # Redundant end-of-stream event over h3; keep waiting
                    # for the genuine http.disconnect.
                    continue
                if not message.get('more_body', False):
                    body_complete = True
                return message
            return message

    async def _send(message):
        if message['type'] == 'http.response.start':
            message['headers'] = [
                (name.lower(), value) for name, value in message.get('headers', [])
            ]
        await send(message)

    await _django_app(scope, _receive, _send)
