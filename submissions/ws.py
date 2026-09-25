"""Raw ASGI WebSocket endpoint for live submission judging status.

Served by a standalone uvicorn process (``oj_project.asgi``) so the Granian
WSGI service that runs the site is untouched. Authentication reuses the
standard Django session cookie — the socket is same-origin.

Delivery model
--------------
1. On connect: authenticate, authorize (owner or staff), subscribe to the
   per-submission channel on the web cache Redis AND every enabled judge
   Redis (remote judges publish on the instance co-located with their
   broker), then send the current database snapshot.
2. Every Redis notification triggers a fresh snapshot push.
3. A slow database watchdog (every ``WATCHDOG_INTERVAL`` s) self-heals any
   missed/unreachable pub/sub message and closes the socket at terminal
   status.
4. Server-originated pings keep CDN/proxy idle timers quiet.

The endpoint never streams hidden test data: snapshots come from the same
builder as the public HTTP status API.
"""

import asyncio
import json
import logging

from asgiref.sync import sync_to_async
from django.conf import settings

from .models import Submission
from .realtime import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    async_cache_client,
    async_judge_clients,
    build_submission_status_payload,
    submission_channel,
)

logger = logging.getLogger(__name__)

WATCHDOG_INTERVAL = 5.0
HEARTBEAT_INTERVAL = 30.0
# Coalesce bursts: the same event may arrive from several endpoints, and the
# worker saves one row per test point. Minimum gap between DB refetches.
FETCH_COALESCE_SEC = 0.25
LISTENER_RECONNECT_SEC = 2.0

# Shared Redis clients for this event loop, built once.
_endpoints = None


def _parse_cookies(scope):
    for name, value in scope.get('headers', []):
        if name == b'cookie':
            cookies = {}
            for part in value.decode('latin-1').split(';'):
                if '=' in part:
                    key, val = part.strip().split('=', 1)
                    cookies[key] = val
            return cookies
    return {}


@sync_to_async
def _load_user(scope):
    """Resolve the session cookie to a Django user, mirroring
    AuthenticationMiddleware (backend validation + session hash)."""
    from django.contrib.auth.middleware import get_user
    from django.contrib.sessions.backends.db import SessionStore

    cookies = _parse_cookies(scope)
    session_key = cookies.get(settings.SESSION_COOKIE_NAME)
    if not session_key:
        return None

    session = SessionStore(session_key)
    try:
        session.load()
    except Exception:
        return None

    class _Request:
        pass

    request = _Request()
    request.session = session
    try:
        user = get_user(request)
    except Exception:
        return None
    return user if user.is_authenticated else None


@sync_to_async
def _load_submission_for_user(submission_id, user):
    try:
        submission = Submission.objects.select_related(
            'problem', 'contest_problem'
        ).prefetch_related('test_results').get(id=submission_id)
    except Submission.DoesNotExist:
        return 'missing', None
    if submission.user_id != user.id and not user.is_staff:
        return 'forbidden', None
    return 'ok', submission


@sync_to_async
def _fresh_snapshot(submission_id):
    submission = Submission.objects.select_related(
        'problem', 'contest_problem'
    ).prefetch_related('test_results').get(id=submission_id)
    return build_submission_status_payload(submission)


def _get_endpoints():
    """Return [(label, redis_client), ...] shared by all connections."""
    global _endpoints
    if _endpoints is not None:
        return _endpoints

    endpoints = []
    cache = async_cache_client()
    if cache is not None:
        endpoints.append(('cache', cache))
    endpoints.extend(async_judge_clients())
    _endpoints = endpoints
    return endpoints


async def submission_status_consumer(scope, receive, send, submission_id):
    user = await _load_user(scope)
    if user is None:
        # Rejecting before accept makes uvicorn answer the handshake with 403.
        await send({'type': 'websocket.close', 'code': CLOSE_UNAUTHORIZED})
        return

    outcome, submission = await _load_submission_for_user(submission_id, user)
    if outcome in ('missing', 'forbidden'):
        await send({'type': 'websocket.close', 'code': CLOSE_FORBIDDEN})
        return

    await send({'type': 'websocket.accept'})

    channel = submission_channel(submission_id)
    endpoints = _get_endpoints()
    # Listeners own their (re)subscriptions and run in the background, so an
    # unreachable judge Redis can never delay the initial snapshot. A
    # notification that arrives during that tiny setup window is caught by
    # the watchdog.
    pubsubs = {}

    done = asyncio.Event()
    state = {'last_text': None, 'last_fetch': 0.0, 'refetch_task': None}
    tasks = []

    async def refetch():
        # Trailing fetch for updates coalesced away: guarantees the newest
        # committed state is pushed instead of waiting for the watchdog.
        await asyncio.sleep(FETCH_COALESCE_SEC)
        if not done.is_set():
            await push_snapshot(force=True)

    async def push_snapshot(force=False):
        loop = asyncio.get_running_loop()
        now = loop.time()
        if not force and now - state['last_fetch'] < FETCH_COALESCE_SEC:
            if state['refetch_task'] is None or state['refetch_task'].done():
                state['refetch_task'] = asyncio.create_task(refetch())
            return
        state['last_fetch'] = now

        try:
            payload = await _fresh_snapshot(submission_id)
        except Submission.DoesNotExist:
            await shutdown(CLOSE_FORBIDDEN)
            return
        except Exception:
            logger.warning(
                'WebSocket snapshot failed for submission %s',
                submission_id,
                exc_info=True,
            )
            return

        text = json.dumps(payload, ensure_ascii=False)
        if text != state['last_text']:
            await send({'type': 'websocket.send', 'text': text})
            state['last_text'] = text
        if payload['done']:
            await shutdown()

    async def receiver():
        while not done.is_set():
            event = await receive()
            if event['type'] == 'websocket.disconnect':
                done.set()
                return
            # Client frames are only heartbeats ("ping"); nothing to answer.

    def make_listener(label, client):
        async def listen():
            while not done.is_set():
                pubsub = pubsubs.get(label)
                try:
                    if pubsub is None:
                        pubsub = client.pubsub()
                        await pubsub.subscribe(channel)
                        pubsubs[label] = pubsub
                    async for message in pubsub.listen():
                        if done.is_set():
                            return
                        if message.get('type') == 'message':
                            await push_snapshot()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.info(
                        'WebSocket listener %s lost for submission %s, '
                        'reconnecting in %ss',
                        label, submission_id, LISTENER_RECONNECT_SEC,
                        exc_info=True,
                    )
                    pubsubs.pop(label, None)
                    try:
                        await asyncio.wait_for(
                            done.wait(), timeout=LISTENER_RECONNECT_SEC
                        )
                        return
                    except asyncio.TimeoutError:
                        pass
        return listen

    async def watchdog():
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=WATCHDOG_INTERVAL)
                return
            except asyncio.TimeoutError:
                await push_snapshot(force=True)

    async def heartbeat():
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=HEARTBEAT_INTERVAL)
                return
            except asyncio.TimeoutError:
                await send({
                    'type': 'websocket.send',
                    'text': json.dumps({'type': 'ping'}),
                })

    async def shutdown(close_code=1000):
        if done.is_set():
            return
        done.set()
        trailing = state.get('refetch_task')
        if trailing is not None and not trailing.done():
            trailing.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await send({'type': 'websocket.close', 'code': close_code})
        except Exception:
            pass

    # Subscribe-before-snapshot ordering prevents lost-update races; the
    # first push closes immediately if judging has already finished.
    tasks.append(asyncio.create_task(receiver()))
    for label, client in endpoints:
        tasks.append(asyncio.create_task(make_listener(label, client)()))
    tasks.append(asyncio.create_task(watchdog()))
    tasks.append(asyncio.create_task(heartbeat()))

    try:
        await push_snapshot(force=True)
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for pubsub in pubsubs.values():
            try:
                await pubsub.aclose()
            except Exception:
                pass
