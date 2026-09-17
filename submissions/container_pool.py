"""Warm per-image pools of long-lived judge containers (one pool per worker).

Why
---
Starting a hardened judge container costs ≈0.5–1.5 s of Docker overhead on
every submission. Instead, each judge worker process keeps a small pool of
``sleep infinity`` containers per judge image, created ahead of demand.
A submission checks one out, works inside its own subdirectory of the
container's bind-mounted host root, and checks the container back in after
verdict. Queue layout and the worker thread-pool concurrency are untouched;
this module only changes where the container comes from.

Layout
------
* Pool container ``<host root>/w<pid>-<uuid>`` is bind-mounted at
  ``/sandbox``. Every checkout gets an unguessable subdirectory
  ``w<pid>-<uuid>/<token>`` exposed inside the container as
  ``/sandbox/<token>`` and used as ``docker exec -w`` workdir.
* Between submissions the container is sanitised: every leftover process is
  SIGKILLed (``--init``/tini reaps zombies) and ``/tmp`` + ``/dev/shm`` are
  wiped. A container is recycled (kill + fresh ``docker run``) after
  ``OJ_CONTAINER_POOL_MAX_USES`` checkouts, ``OJ_CONTAINER_POOL_MAX_AGE_SEC``
  seconds of life, a failed sanitisation, or an infrastructure error — this
  bounds cross-submission state leakage.
* A background maintainer thread warms ``OJ_CONTAINER_POOL_MIN_IDLE``
  containers per image, reaps aged-out idle containers, and sweeps orphaned
  host directories left by crashed workers.
* If the pool is disabled, not started (e.g. web process / unit tests),
  does not know the image, or is temporarily exhausted, callers fall back
  to the one-container-per-submission path in ``submissions.sandbox``.

Pooled containers carry labels (``oj.judge.role=pool``,
``oj.judge.image=<image>``, ``oj.judge.worker=<pid>``) so
``submissions.docker_cleanup`` can leave live workers' containers alone
while reaping orphans of dead workers.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from django.conf import settings

from .sandbox import (
    POOL_IMAGE_LABEL,
    POOL_ROLE_LABEL,
    POOL_WORKER_LABEL,
    docker_exec,
    ensure_docker_ready,
    ensure_judge_image_available,
    start_judge_container,
    update_judge_container_memory,
)

logger = logging.getLogger(__name__)


class ContainerPoolUnavailable(Exception):
    """Raised when no pooled container can be obtained in time."""


# ── configuration ─────────────────────────────────────────────────────────

def _setting(name, default):
    return getattr(settings, name, default)


def pool_enabled():
    return bool(_setting("OJ_CONTAINER_POOL_ENABLED", True))


def _min_idle():
    return max(0, int(_setting("OJ_CONTAINER_POOL_MIN_IDLE", 1)))


def _concurrency():
    try:
        return max(1, int(_setting("OJ_JUDGE_CONCURRENCY", 4)))
    except (TypeError, ValueError):
        return 4


def _max_size():
    """Per-image cap on live (idle + in-use + warming) containers.

    The default (thread-pool concurrency + min-idle reserve) guarantees that
    even when every judge thread uses the same image, at least the idle
    reserve remains available; the pool therefore never blocks on the
    steady-state path.
    """
    configured = _setting("OJ_CONTAINER_POOL_MAX_SIZE", None)
    if configured:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass
    return _concurrency() + _min_idle()


def _memory_mb():
    # Fixed cgroup cap for the whole container life. Compilation needs at
    # least ~512 MB; the cap is a limit, not a reservation, so a generous
    # default costs nothing for idle ``sleep`` containers. Per-case memory
    # is still measured by /usr/bin/time inside the container.
    try:
        return max(512, int(_setting("OJ_CONTAINER_POOL_MEMORY_MB", 1024)))
    except (TypeError, ValueError):
        return 1024


def _max_uses():
    return max(1, int(_setting("OJ_CONTAINER_POOL_MAX_USES", 50)))


def _max_age_sec():
    return max(60, int(_setting("OJ_CONTAINER_POOL_MAX_AGE_SEC", 3600)))


def _idle_ttl_sec():
    # Surplus idle containers (beyond min_idle) survive this long after
    # their last checkout, so a burst reuses them; then they are reaped.
    return max(30, int(_setting("OJ_CONTAINER_POOL_IDLE_TTL_SEC", 300)))


def _acquire_timeout_sec():
    return max(1.0, float(_setting("OJ_CONTAINER_POOL_ACQUIRE_TIMEOUT", 15)))


def _work_root():
    root = Path(
        str(_setting("OJ_CONTAINER_POOL_WORK_ROOT", "/tmp/oj_container_pool"))
    )
    return root


_MAINTAIN_INTERVAL_SEC = 5
_SWEEP_INTERVAL_SEC = 600
_SWEEP_MIN_AGE_SEC = 600
_SANITIZE_TIMEOUT_SEC = 10
_KILL_TIMEOUT_SEC = 10

# Container reset between checkouts.
#
# The init chain (tini + the ``sleep infinity`` keepalive) lives in PID
# namespace session 1, while every ``docker exec`` payload runs in its own
# freshly created session (including processes orphaned after the exec
# client goes away). Therefore "every task whose session id is neither 1
# nor this reset shell's session" is exactly the set of leftover contestant
# processes. An unprivileged task cannot join sid 1 after the container
# started, so a contestant cannot survive the reset by faking a process
# name. Two sweeps catch children of parents killed in sweep one; then the
# tmpfs scratch spaces are wiped (/dev/shm does not exist with --ipc=none).
_SANITIZE_SCRIPT = (
    'read -r _m < /proc/$$/stat; _r=${_m##*)}; set -- $_r; _mysid=$4; '
    'for round in 1 2; do '
    'victims=""; '
    'for p in /proc/[0-9]*; do '
    'pid=${p#/proc/}; '
    '[ "$pid" = "1" ] && continue; '
    '[ "$pid" = "$$" ] && continue; '
    'read -r st < "$p/stat" 2>/dev/null || continue; '
    'rest=${st##*)}; set -- $rest; '
    '[ "$4" = "1" ] && continue; '
    '[ "$4" = "$_mysid" ] && continue; '
    'victims="$victims $pid"; '
    'done; '
    '[ -z "$victims" ] && break; '
    'kill -9 $victims >/dev/null 2>&1 || true; '
    'sleep 0.05; '
    'done; '
    'rm -rf /tmp/* /tmp/.[!.]* >/dev/null 2>&1 || true; '
    'if [ -d /dev/shm ]; then rm -rf /dev/shm/* /dev/shm/.[!.]* >/dev/null 2>&1 || true; fi; '
    'exit 0'
)


# ── pooled container handle ───────────────────────────────────────────────

class PooledContainer:
    """A warm container plus the host directory bind-mounted at ``/sandbox``."""

    __slots__ = ("cid", "image", "host_root", "created_at", "last_used_at",
                 "use_count", "memory_mb")

    def __init__(self, cid, image, host_root, created_at, memory_mb):
        self.cid = cid
        self.image = image
        self.host_root = host_root
        self.created_at = created_at
        self.last_used_at = created_at
        self.use_count = 0
        # Cgroup cap currently applied to the container; resized per
        # checkout to match the problem's memory limit.
        self.memory_mb = memory_mb

    def age_sec(self):
        return time.monotonic() - self.created_at

    def exec(self, command, timeout_sec, stdin=None, workdir=None):
        return docker_exec(
            self.cid, command, timeout_sec, stdin=stdin, workdir=workdir
        )


class _ImageState:
    __slots__ = ("idle", "live", "warming")

    def __init__(self):
        self.idle = deque()
        self.live = 0
        # Containers whose ``docker run`` is currently in flight, so the
        # maintainer never double-warms when creation takes longer than one
        # maintenance interval.
        self.warming = 0


# ── the pool ──────────────────────────────────────────────────────────────

class ContainerPool:
    def __init__(self, images):
        self._images = tuple(sorted(set(images)))
        self._states = {image: _ImageState() for image in self._images}
        self._cond = threading.Condition(threading.RLock())
        self._handles = set()  # every live container (idle + in-use)
        self._stop = threading.Event()
        self._maintainer = None
        self._worker_pid = os.getpid()
        self._work_root = _work_root()
        self._last_sweep = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self):
        try:
            self._work_root.mkdir(parents=True, exist_ok=True)
            os.chmod(self._work_root, 0o711)
        except OSError:
            logger.exception(
                "Container pool work root %s is unusable; pool disabled",
                self._work_root,
            )
            return False
        self._maintainer = threading.Thread(
            target=self._maintain_loop,
            daemon=True,
            name="judge-container-pool",
        )
        self._maintainer.start()
        logger.info(
            "Judge container pool started (images=%s, min_idle=%s, max_size=%s)",
            ",".join(self._images), _min_idle(), _max_size(),
        )
        return True

    def shutdown(self):
        """Stop the maintainer and kill every pooled container.

        Called from the worker's graceful-shutdown path, after in-flight
        judge threads have finished, so only idle handles should remain.
        """
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
            handles = list(self._handles)
            self._handles.clear()
            for state in self._states.values():
                state.live = 0
                state.warming = 0
                state.idle.clear()
        if self._maintainer is not None:
            self._maintainer.join(timeout=10)
        for handle in handles:
            self._destroy(handle)
        if handles:
            logger.info("Judge container pool shut down (%s containers killed)",
                        len(handles))

    def knows(self, image):
        return image in self._states

    # ── checkout / return ────────────────────────────────────────────────

    def acquire(self, image, timeout=None, memory_mb=None):
        """Check out one healthy container for *image*.

        If *memory_mb* is given, the container's cgroup cap is resized to it
        (``docker update``) before checkout; a resize failure recycles the
        container and retries with a fresh one. Raises
        :class:`ContainerPoolUnavailable` if none can be provided within
        *timeout* seconds.
        """
        if timeout is None:
            timeout = _acquire_timeout_sec()
        if memory_mb is not None:
            memory_mb = max(int(memory_mb), 32)
        deadline = time.monotonic() + float(timeout)
        state = self._states[image]
        last_error = None

        while not self._stop.is_set():
            handle = None
            create_now = False

            with self._cond:
                if state.idle:
                    handle = state.idle.popleft()
                elif state.live < _max_size():
                    # Reserve the slot; docker IO happens outside the lock.
                    state.live += 1
                    create_now = True
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ContainerPoolUnavailable(
                            f"No pooled {image} container available "
                            f"({last_error or 'all slots busy'})"
                        )
                    self._cond.wait(min(remaining, 1.0))
                    continue

            if handle is not None:
                if not self._is_healthy(handle):
                    # Died while idle (daemon restart, OOM, manual kill, ...).
                    logger.warning(
                        "Pooled container %s (%s) is gone; discarding",
                        handle.cid[:12], image,
                    )
                    self._destroy(handle)
                    with self._cond:
                        state.live -= 1
                        self._cond.notify_all()
                    continue
                handle.use_count += 1
            else:
                # create_now
                try:
                    handle = self._create(image)
                except Exception as exc:  # normalised DockerNotAvailableError
                    last_error = str(exc)
                    logger.warning(
                        "Failed to warm pooled %s container: %s", image, exc
                    )
                    with self._cond:
                        state.live -= 1
                        self._cond.notify_all()
                    if deadline - time.monotonic() <= 0:
                        raise ContainerPoolUnavailable(str(exc)) from exc
                    time.sleep(0.2)
                    continue
                handle.use_count = 1
                logger.debug("Handed out fresh pooled %s container %s",
                             image, handle.cid[:12])

            if not self._apply_memory(handle, state, memory_mb):
                continue
            return handle

        raise ContainerPoolUnavailable("pool is shutting down")

    def _apply_memory(self, handle, state, memory_mb):
        """Resize *handle* to *memory_mb*; recycle it on failure.

        Returns True when the container is usable, False when it was
        destroyed (caller should loop for a replacement).
        """
        if memory_mb is None or handle.memory_mb == memory_mb:
            return True
        try:
            update_judge_container_memory(handle.cid, memory_mb)
            handle.memory_mb = memory_mb
            return True
        except Exception as exc:
            logger.warning(
                "Resizing pooled container %s to %sMB failed (%s); "
                "recycling it",
                handle.cid[:12], memory_mb, exc,
            )
            self._destroy(handle)
            with self._cond:
                state.live -= 1
                self._cond.notify_all()
            return False

    def release(self, handle, force_destroy=False):
        """Return a container: sanitise and re-pool, or recycle it."""
        state = self._states.get(handle.image)
        recycle = (
            force_destroy
            or handle.use_count >= _max_uses()
            or handle.age_sec() >= _max_age_sec()
            or self._stop.is_set()
        )

        if not recycle:
            try:
                self._sanitize(handle)
            except Exception as exc:
                logger.warning(
                    "Sanitising pooled container %s failed (%s); recycling",
                    handle.cid[:12], exc,
                )
                recycle = True

        if recycle:
            self._destroy(handle)

        with self._cond:
            if state is not None:
                state.live -= 1
                if not recycle:
                    handle.last_used_at = time.monotonic()
                    state.idle.append(handle)
            self._cond.notify_all()

        if recycle:
            logger.debug(
                "Recycled pooled %s container %s after %s use(s)",
                handle.image, handle.cid[:12], handle.use_count,
            )

    # ── container operations (blocking docker IO; never under the lock) ──

    def _create(self, image):
        ensure_docker_ready()
        ensure_judge_image_available(image)
        host_root = self._work_root / f"w{self._worker_pid}-{uuid.uuid4().hex[:12]}"
        host_root.mkdir(parents=True, exist_ok=False)
        os.chmod(host_root, 0o711)
        labels = {
            POOL_ROLE_LABEL: "pool",
            POOL_IMAGE_LABEL: image,
            POOL_WORKER_LABEL: str(self._worker_pid),
        }
        try:
            cid = start_judge_container(
                str(host_root),
                _memory_mb(),
                image,
                # Compile seccomp profile is the superset; pooled containers
                # serve both compile and execute steps.
                is_compile=True,
                labels=labels,
                use_init=True,
            )
        except Exception:
            shutil.rmtree(host_root, ignore_errors=True)
            raise
        handle = PooledContainer(
            cid, image, str(host_root), time.monotonic(), _memory_mb()
        )
        with self._cond:
            self._handles.add(handle)
        return handle

    def _is_healthy(self, handle):
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", handle.cid],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _sanitize(self, handle):
        """Kill leftover processes and wipe tmpfs state between checkouts."""
        result = handle.exec(
            ["/bin/bash", "-lc", _SANITIZE_SCRIPT],
            _SANITIZE_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"sanitize exited {result.returncode}: "
                f"{getattr(result, 'stderr', '') or ''}".strip()
            )
        if not self._is_healthy(handle):
            raise RuntimeError("container not running after sanitize")

    def _destroy(self, handle):
        try:
            subprocess.run(
                ["docker", "kill", handle.cid],
                capture_output=True,
                timeout=_KILL_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
        shutil.rmtree(handle.host_root, ignore_errors=True)
        with self._cond:
            self._handles.discard(handle)

    # ── background maintenance ───────────────────────────────────────────

    def _maintain_loop(self):
        while not self._stop.is_set():
            try:
                self._maintain_once()
            except Exception:
                logger.exception("Container pool maintenance iteration failed")
            self._stop.wait(_MAINTAIN_INTERVAL_SEC)

    def _maintain_once(self):
        for image, state in self._states.items():
            if self._stop.is_set():
                return

            # Recycle idle containers that outlived the max age.
            aged = []
            now = time.monotonic()
            with self._cond:
                while state.idle and (now - state.idle[0].created_at) >= _max_age_sec():
                    aged.append(state.idle.popleft())
                    state.live -= 1
                self._cond.notify_all()
            for handle in aged:
                logger.debug("Pooled %s container %s reached max age",
                             image, handle.cid[:12])
                self._destroy(handle)

            # Shrink surplus idle capacity after the idle TTL (idle deque is
            # FIFO, so idle[0] is the longest-unused container).
            stale = []
            with self._cond:
                while (
                    len(state.idle) > _min_idle()
                    and (now - state.idle[0].last_used_at) >= _idle_ttl_sec()
                ):
                    stale.append(state.idle.popleft())
                    state.live -= 1
                self._cond.notify_all()
            for handle in stale:
                self._destroy(handle)

            # Warm up to min_idle (bounded by the per-image cap); containers
            # still being created count toward the target.
            want_create = 0
            with self._cond:
                while (
                    len(state.idle) + state.warming < _min_idle()
                    and state.live < _max_size()
                ):
                    state.live += 1
                    state.warming += 1
                    want_create += 1

            for _ in range(want_create):
                try:
                    handle = self._create(image)
                except Exception as exc:
                    with self._cond:
                        state.live -= 1
                        state.warming -= 1
                        self._cond.notify_all()
                    # e.g. image not built on this host yet; retry next tick.
                    logger.warning("Pool warm-up for %s failed: %s", image, exc)
                    break
                with self._cond:
                    state.warming -= 1
                    state.idle.append(handle)
                    self._cond.notify_all()

        if time.monotonic() - self._last_sweep > _SWEEP_INTERVAL_SEC:
            self._last_sweep = time.monotonic()
            self._sweep_orphan_dirs()

    def _sweep_orphan_dirs(self):
        """Remove host dirs left behind by crashed worker processes.

        A dir name encodes its owner pid (``w<pid>-<uuid>``). Only dirs whose
        owner is dead *and* which have been untouched for a while are
        removed, so another live worker can never be touched.
        """
        try:
            entries = list(self._work_root.iterdir())
        except (FileNotFoundError, OSError):
            return
        now = time.time()
        for entry in entries:
            if not entry.is_dir():
                continue
            head, _, _ = entry.name.partition("-")
            owner_pid = None
            if head.startswith("w") and head[1:].isdigit():
                owner_pid = int(head[1:])
            # Owned by a live process: never touch.
            if owner_pid is not None and self._pid_alive(owner_pid):
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if owner_pid is None and age < 3600:
                continue
            if age < _SWEEP_MIN_AGE_SEC:
                continue
            logger.info("Sweeping orphaned pool work dir %s", entry)
            shutil.rmtree(entry, ignore_errors=True)

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


# ── process-wide singleton ────────────────────────────────────────────────

_pool = None
_pool_lock = threading.Lock()


def start_pool(images=None):
    """Start this worker process's container pool (idempotent).

    No-op when pooling is disabled or Docker is switched off.
    """
    global _pool
    if not pool_enabled():
        return None
    with _pool_lock:
        if _pool is not None:
            return _pool
        if images is None:
            # Lazy import: judge.py imports this module, so importing it at
            # module level would be circular.
            from .judge import LANG_IMAGE
            images = set(LANG_IMAGE.values())
        try:
            pool = ContainerPool(images)
            if not pool.start():
                return None
        except Exception:
            logger.exception("Failed to start judge container pool")
            return None
        _pool = pool
        return _pool


def shutdown_pool():
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is not None:
        pool.shutdown()


def is_running():
    return _pool is not None


def acquire(image, timeout=None, memory_mb=None):
    """Return a :class:`PooledContainer` or ``None``.

    ``None`` means "use the one-container-per-submission fallback": pooling
    disabled, pool not started on this process, unknown image, or pool
    temporarily exhausted.
    """
    pool = _pool
    if pool is None or not image or not pool.knows(image):
        return None
    try:
        return pool.acquire(image, timeout=timeout, memory_mb=memory_mb)
    except ContainerPoolUnavailable:
        logger.warning(
            "Container pool exhausted for %s; using ephemeral container",
            image,
        )
        return None
    except Exception:
        logger.exception("Unexpected container pool error for %s", image)
        return None


def release(handle, force_destroy=False):
    if handle is None:
        return
    pool = _pool
    if pool is None:
        # Pool vanished (shutdown): destroy the now-orphaned container.
        try:
            subprocess.run(
                ["docker", "kill", handle.cid],
                capture_output=True,
                timeout=_KILL_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
        shutil.rmtree(handle.host_root, ignore_errors=True)
        return
    pool.release(handle, force_destroy=force_destroy)
