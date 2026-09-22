"""HTTP client used by DB-less judge workers (Phase 3).

The worker claims submissions and renews leases through the Django
internal API instead of touching PostgreSQL. Only the stdlib is used so
this module stays importable in an environment stripped of all DB
configuration. Claims survive short API outages with bounded retries;
a hard ``409`` means another worker owns the job and is never retried.
"""

from __future__ import annotations

import gzip
import http.client
import json
import logging
import threading
import time
import urllib.parse

from django.conf import settings

logger = logging.getLogger(__name__)


class ClaimLostError(Exception):
    """Heartbeat says our token no longer owns the submission."""


class ClaimEndpointUnavailable(Exception):
    """The claim API could not be reached within the retry budget."""


class _ConnectionPool:
    """Keeps one idle HTTP connection per (thread, base URL).

    Judge machines can sit a WAN hop away from the web host, where opening a
    fresh TCP connection costs a full round trip (~55ms measured) *before* the
    request is even sent, on top of the request itself. ``urllib`` cannot
    reuse a connection, so this client talks ``http.client`` directly. Each
    thread gets its own sockets (they are not thread-safe); a connection the
    peer closed while it sat idle is dropped and retried once on a new one.
    """

    def __init__(self):
        self._local = threading.local()

    def _conns(self):
        conns = getattr(self._local, 'conns', None)
        if conns is None:
            conns = self._local.conns = {}
        return conns

    def _connect(self, base, timeout):
        parts = urllib.parse.urlsplit(base)
        if parts.scheme == 'https':
            return http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=timeout,
            )
        return http.client.HTTPConnection(
            parts.hostname, parts.port, timeout=timeout,
        )

    def _discard(self, base):
        conn = self._conns().pop(base, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def post(self, base, path, body, headers, timeout):
        """POST ``body`` to ``base`` + ``path``; return (status, payload)."""
        conns = self._conns()
        for _attempt in (1, 2):
            conn = conns.get(base)
            reused = conn is not None
            if conn is None:
                conn = conns[base] = self._connect(base, timeout)
            try:
                conn.request('POST', path, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read()
                if (response.getheader('Content-Encoding') or '').lower() == 'gzip':
                    # Test data is bulky, repetitive text and the judge uplink
                    # is the bottleneck, so the web edge gzips these payloads.
                    # Inflate here so every caller still sees plain JSON.
                    data = gzip.decompress(data)
                if response.will_close:
                    # The peer is done with this socket, so keeping it would
                    # only bill the next call for a failed attempt.
                    self._discard(base)
                return response.status, data
            except (http.client.HTTPException, OSError) as exc:
                # A reused socket may have been closed by the peer while it
                # sat idle: drop it and retry once on a fresh connection.
                self._discard(base)
                if not reused:
                    raise
                logger.debug('Recycled %s connection failed: %r', base, exc)
        raise AssertionError('unreachable')


_POOL = _ConnectionPool()


class JudgeApiClient:
    """Talks to the internal judge API over one or two base URLs.

    ``base_url`` is the fast path (the direct link, an IP literal).
    ``fallback_base_url`` is the always-reachable CDN origin: it is used
    first by :meth:`report_ip` (the direct link may be dead precisely
    because our NAT address changed) and second by everything else.
    """

    def __init__(self, base_url=None, fallback_base_url=None, token=None,
                 timeout=15):
        self.base_url = (
            base_url or getattr(settings, 'JUDGE_API_BASE', '')
        ).rstrip('/')
        fallback = (
            fallback_base_url if fallback_base_url is not None
            else getattr(settings, 'JUDGE_API_FALLBACK_BASE', '')
        )
        fallback = (fallback or '').rstrip('/')
        self.fallback_base_url = '' if fallback == self.base_url else fallback
        self.token = token or getattr(settings, 'JUDGE_INTERNAL_TOKEN', '')
        self.timeout = timeout

    def _bases(self, prefer_fallback=False):
        if prefer_fallback and self.fallback_base_url:
            ordered = [self.fallback_base_url, self.base_url]
        else:
            ordered = [self.base_url, self.fallback_base_url]
        return [base for base in ordered if base]

    def _post(self, path, payload, retry_on_network=False, max_wait=60,
              prefer_fallback=False):
        bases = self._bases(prefer_fallback)
        if not bases or not self.token:
            raise ClaimEndpointUnavailable(
                'JUDGE_API_BASE / JUDGE_INTERNAL_TOKEN not configured'
            )
        # Split the retry budget across bases so an unreachable primary
        # cannot starve the fallback.
        last_error = None
        for index, base in enumerate(bases):
            try:
                return self._post_to(
                    base, path, payload, retry_on_network,
                    max_wait / len(bases),
                )
            except ClaimEndpointUnavailable as exc:
                last_error = exc
                if index + 1 < len(bases):
                    logger.warning(
                        'Judge API base %s failed (%s); trying %s',
                        base, exc, bases[index + 1],
                    )
        raise last_error

    def _post_to(self, base, path, payload, retry_on_network, max_wait):
        target = urllib.parse.urlsplit(f'{base}/internal/judge/{path}')
        wire_path = target.path or '/'
        if target.query:
            wire_path = f'{wire_path}?{target.query}'
        body = json.dumps(payload).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'X-Judge-Token': self.token,
            # Some CDNs/WAFs challenge the default Python-urllib UA.
            'User-Agent': 'GuwuOJ-JudgeWorker/1.0',
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'Content-Length': str(len(body)),
            # Pin the socket in the pool: a judge machine is a WAN hop away,
            # so every dropped connection costs another full handshake.
            'Connection': 'keep-alive',
        }
        deadline = time.monotonic() + max_wait
        attempt = 0
        while True:
            attempt += 1
            try:
                status, data = _POOL.post(
                    base, wire_path, body, headers, self.timeout,
                )
                if status == 409:
                    # 409 is an authoritative "you don't own this" answer.
                    try:
                        return json.loads(data.decode('utf-8'))
                    except Exception:
                        return {'claimable': False, 'alive': False}
                if status in (400, 403, 404):
                    # A refusal, not an outage: hand it to _post so the other
                    # base gets a chance before the error reaches the caller.
                    raise ClaimEndpointUnavailable(
                        f'POST {path} to {base} was refused with HTTP {status}'
                    )
                if status >= 400:
                    logger.warning('Judge API %s returned HTTP %s', path, status)
                else:
                    return json.loads(data.decode('utf-8'))
            except (http.client.HTTPException, OSError) as exc:
                logger.warning('Judge API %s unreachable: %r', path, exc)

            if not retry_on_network or time.monotonic() >= deadline:
                raise ClaimEndpointUnavailable(
                    f'POST {path} to {base} failed after {attempt} attempt(s)'
                )
            delay = min(1.0 * (2 ** min(attempt - 1, 5)), 10)
            time.sleep(delay)

    def claim(self, submission_id, worker_id):
        return self._post(
            'claim/',
            {
                'submission_id': submission_id,
                'worker_id': worker_id,
                # Ask the web side to leave test data out of the response.
                # It is pulled lazily through :meth:`fetch_cases`, so judging
                # starts immediately instead of waiting for a payload that
                # can reach hundreds of megabytes.
                'batched_cases': True,
            },
            retry_on_network=True,
            max_wait=120,
        )

    def fetch_cases(self, submission_id, claim_token, offset, limit):
        """Pull one slice of test data for a claim we still own.

        Raises :class:`ClaimLostError` when the web side answers ``409``:
        the lease was revoked (or the row already reached a terminal state),
        and the caller must abort rather than keep judging.
        """
        data = self._post(
            'cases/',
            {
                'submission_id': submission_id,
                'claim_token': claim_token,
                'offset': offset,
                'limit': limit,
            },
            retry_on_network=True,
            max_wait=120,
        )
        if data.get('claimable') is False:
            raise ClaimLostError(
                f'test data for submission {submission_id} is no longer ours'
            )
        return data.get('cases') or []

    def heartbeat(self, submission_id, claim_token):
        data = self._post(
            'heartbeat/',
            {'submission_id': submission_id, 'claim_token': claim_token},
            retry_on_network=False,
        )
        return bool(data.get('alive'))

    def report_ip(self, worker_id):
        """Tell the web side which source address to whitelist.

        Best effort: a failure only matters once the NAT address actually
        changes, and the caller retries then. Prefers the CDN base because
        the direct base is likely the thing that just stopped working.
        """
        try:
            return self._post(
                'report_ip/', {'worker_id': worker_id},
                retry_on_network=True, max_wait=30,
                prefer_fallback=True,
            )
        except Exception:
            logger.warning('Could not report worker IP', exc_info=True)
            return None


class HttpHeartbeat:
    """Background lease renewal against the heartbeat API."""

    def __init__(self, client, submission_id, claim_token, interval_secs=None):
        self.client = client
        self.submission_id = submission_id
        self.claim_token = claim_token
        self.interval_secs = (
            interval_secs
            if interval_secs is not None
            else getattr(settings, 'OJ_JUDGE_HEARTBEAT_SECS', 15)
        )
        self._stop = None
        self._lost = None
        self._thread = None

    def start(self):
        import threading
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f'judge-hb-http-{self.submission_id}',
            daemon=True,
        )
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(self.interval_secs):
            try:
                if not self.client.heartbeat(
                    self.submission_id, self.claim_token,
                ):
                    logger.warning(
                        'HTTP heartbeat lost claim on submission %s',
                        self.submission_id,
                    )
                    self._lost.set()
                    return
            except Exception:
                # Transient API blip: keep renewing; the DB-side reaper is
                # the authoritative lease backstop.
                logger.debug(
                    'heartbeat call failed for %s',
                    self.submission_id, exc_info=True,
                )

    def ensure_alive(self):
        if self._lost is not None and self._lost.is_set():
            raise ClaimLostError(
                f'claim for submission {self.submission_id} was revoked'
            )

    def stop(self):
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
