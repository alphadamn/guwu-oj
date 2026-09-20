"""HTTP client used by DB-less judge workers (Phase 3).

The worker claims submissions and renews leases through the Django
internal API instead of touching PostgreSQL. Only the stdlib is used so
this module stays importable in an environment stripped of all DB
configuration. Claims survive short API outages with bounded retries;
a hard ``409`` means another worker owns the job and is never retried.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


class ClaimLostError(Exception):
    """Heartbeat says our token no longer owns the submission."""


class ClaimEndpointUnavailable(Exception):
    """The claim API could not be reached within the retry budget."""


class JudgeApiClient:
    def __init__(self, base_url=None, token=None, timeout=15):
        self.base_url = (
            base_url or getattr(settings, 'JUDGE_API_BASE', '')
        ).rstrip('/')
        self.token = token or getattr(settings, 'JUDGE_INTERNAL_TOKEN', '')
        self.timeout = timeout

    def _post(self, path, payload, retry_on_network=False, max_wait=60):
        if not self.base_url or not self.token:
            raise ClaimEndpointUnavailable(
                'JUDGE_API_BASE / JUDGE_INTERNAL_TOKEN not configured'
            )
        url = f'{self.base_url}/internal/judge/{path}'
        body = json.dumps(payload).encode('utf-8')
        deadline = time.monotonic() + max_wait
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(
                url, data=body, method='POST',
                headers={
                    'Content-Type': 'application/json',
                    'X-Judge-Token': self.token,
                    # Some CDNs/WAFs challenge the default Python-urllib UA.
                    'User-Agent': 'GuwuOJ-JudgeWorker/1.0',
                    'Accept': 'application/json',
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode('utf-8'))
            except urllib.error.HTTPError as exc:
                # 409 is an authoritative "you don't own this" answer.
                if exc.code == 409:
                    try:
                        return json.loads(exc.read().decode('utf-8'))
                    except Exception:
                        return {'claimable': False, 'alive': False}
                if exc.code in (400, 403, 404):
                    raise
                logger.warning('Judge API %s returned HTTP %s', path, exc.code)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.warning('Judge API %s unreachable: %r', path, exc)

            if not retry_on_network or time.monotonic() >= deadline:
                raise ClaimEndpointUnavailable(
                    f'POST {path} failed after {attempt} attempt(s)'
                )
            delay = min(1.0 * (2 ** min(attempt - 1, 5)), 10)
            time.sleep(delay)

    def claim(self, submission_id, worker_id):
        return self._post(
            'claim/',
            {'submission_id': submission_id, 'worker_id': worker_id},
            retry_on_network=True,
            max_wait=120,
        )

    def heartbeat(self, submission_id, claim_token):
        data = self._post(
            'heartbeat/',
            {'submission_id': submission_id, 'claim_token': claim_token},
            retry_on_network=False,
        )
        return bool(data.get('alive'))


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
