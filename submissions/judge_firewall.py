"""Keep the direct judge API firewall in sync with worker-reported IPs.

Workers behind NAT get a new public IP regularly, so a static source
whitelist cannot survive. Instead each worker reports the IP it appears as
(via ``POST /internal/judge/report_ip/``) and this module rebuilds the
iptables chains from the reported state:

    INPUT/VLESS_MIN  -p tcp --dport <port> -j JUDGE_DIRECT / OJ_JUDGE_BROKER
    JUDGE_DIRECT       -s <reported ip> -j ACCEPT          # direct API (8446)
    JUDGE_DIRECT       -j DROP
    OJ_JUDGE_BROKER    -s <ip> -p tcp -m multiport --dports 6379,8446 -j ACCEPT
    OJ_JUDGE_BROKER    -j DROP

``OJ_JUDGE_BROKER`` gates both Redis (6379) and the direct API (8446) and
sits in ``VLESS_MIN_INPUT`` ahead of the catch-all DROP, so it is the
chain that actually matters for port 8446 in production. Reported IPs are
synced to **both** chains; the broker chain additionally includes any
private LAN IPs from ``OJ_JUDGE_BROKER_STATIC_IPS``.
"""

from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time

from django.conf import settings

logger = logging.getLogger(__name__)

IPTABLES = shutil.which('iptables') or '/usr/sbin/iptables'
LOCK_PATH = os.path.join(tempfile.gettempdir(), 'oj-judge-firewall.lock')


def _port():
    return int(getattr(settings, 'OJ_JUDGE_DIRECT_PORT', 8446))


def _chain():
    return getattr(settings, 'OJ_JUDGE_DIRECT_CHAIN', 'JUDGE_DIRECT')


def _state_path():
    return getattr(
        settings, 'OJ_JUDGE_DIRECT_STATE', '/etc/guwu/judge-direct-ips.json',
    )


def _static_ips():
    return list(getattr(settings, 'OJ_JUDGE_DIRECT_STATIC_IPS', []) or [])


# --- broker chain (Redis 6379 + direct API 8446) ---------------------------

def _broker_chain():
    return getattr(settings, 'OJ_JUDGE_BROKER_CHAIN', 'OJ_JUDGE_BROKER')


def _broker_ports():
    """Comma-separated port list for the broker chain's multiport rules."""
    raw = getattr(settings, 'OJ_JUDGE_BROKER_PORTS', '6379,8446')
    return [p.strip() for p in str(raw).split(',') if p.strip()]


def _broker_static_ips():
    """Static IPs for the broker chain (may include private LAN addresses)."""
    return list(getattr(settings, 'OJ_JUDGE_BROKER_STATIC_IPS', []) or [])


def is_usable_source(ip):
    """True when ``ip`` is a public IPv4 literal worth whitelisting."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


def _is_ipv4(ip):
    """True when ``ip`` is any valid IPv4 literal (including private)."""
    try:
        return ipaddress.ip_address(ip).version == 4
    except ValueError:
        return False


def load_state():
    """Return ``{worker_id: {'ip': str, 'updated_at': float}}``."""
    try:
        with open(_state_path(), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(worker): entry
        for worker, entry in data.items()
        if isinstance(entry, dict) and entry.get('ip')
    }


@contextlib.contextmanager
def _firewall_lock():
    """Serialise state writes and chain rebuilds across worker processes.

    Reports arrive concurrently (several workers share one edge second), so
    both the read-modify-write of the state file and the flush/re-add of the
    chain must happen under one lock; otherwise a report can clobber another
    worker's entry or rebuild the chain from a stale snapshot.
    """
    os.makedirs(os.path.dirname(LOCK_PATH) or '.', exist_ok=True)
    with open(LOCK_PATH, 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _write_state(state):
    path = _state_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # A unique temp file per writer: a fixed ``.tmp`` name let two concurrent
    # reports race on ``os.replace`` (one renames it away, the loser raises).
    fd, tmp = tempfile.mkstemp(
        dir=parent or '.', prefix='.judge-direct-', suffix='.tmp',
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def record_worker_ip(worker_id, ip):
    """Remember ``ip`` as ``worker_id``'s current source. Returns previous."""
    with _firewall_lock():
        state = load_state()
        previous = (state.get(str(worker_id)) or {}).get('ip')
        state[str(worker_id)] = {'ip': ip, 'updated_at': time.time()}
        _write_state(state)
    return previous


def allowed_ips(state=None):
    """Ordered, de-duplicated set of sources the direct-API chain accepts.

    Only public IPv4 unicast addresses — the direct base URL is an IPv4
    literal and private LAN workers don't use it.
    """
    if state is None:
        state = load_state()
    ordered = []
    for ip in _static_ips():
        if is_usable_source(ip) and ip not in ordered:
            ordered.append(ip)
    for entry in state.values():
        ip = entry.get('ip')
        if is_usable_source(ip) and ip not in ordered:
            ordered.append(ip)
    return ordered


def broker_allowed_ips(state=None):
    """Ordered, de-duplicated IPs for the broker chain (Redis + direct API).

    Unlike :func:`allowed_ips` this includes private LAN addresses (from
    ``OJ_JUDGE_BROKER_STATIC_IPS``) because local judge workers reach Redis
    over the LAN.  Reported (public) IPs are included so NAT workers get
    Redis access too.
    """
    if state is None:
        state = load_state()
    ordered = []
    for ip in list(_static_ips()) + list(_broker_static_ips()):
        if _is_ipv4(ip) and ip not in ordered:
            ordered.append(ip)
    for entry in state.values():
        ip = entry.get('ip')
        if _is_ipv4(ip) and ip not in ordered:
            ordered.append(ip)
    return ordered


def _run(args, quiet=False):
    """Run an iptables command and report whether it succeeded.

    ``quiet`` suppresses the warning for probes whose failure is an
    expected outcome (chain/rule already exists, chain already absent).
    """
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode != 0 and not quiet:
        logger.warning(
            'iptables command failed (%s): %s',
            ' '.join(args), (result.stderr or '').strip(),
        )
    return result.returncode == 0


def _ensure_chain(chain):
    """Create ``chain`` unless it already exists (the normal re-run case)."""
    if _run([IPTABLES, '-N', chain], quiet=True):
        return
    # ``-N`` failed. The chain merely existing already is expected and
    # harmless; any other failure (e.g. missing privileges) is worth
    # surfacing.
    if _run([IPTABLES, '-S', chain], quiet=True):
        logger.debug('iptables chain %s already exists', chain)
        return
    logger.warning('iptables chain %s could not be created or inspected', chain)


def apply_firewall(state=None):
    """Rebuild the direct-API and broker chains from state.

    Safe to run repeatedly.  Returns the list of accepted direct-API
    sources, or ``None`` when the chains could not be rebuilt (iptables
    missing, or the writes failed) — the caller uses that to tell the
    reporter whether its IP was actually applied.
    """
    if not os.path.exists(IPTABLES):
        logger.debug('iptables not present; skipping firewall sync')
        return None

    chain = _chain()
    port = str(_port())
    broker_chain = _broker_chain()
    broker_ports = _broker_ports()
    # multiport match args shared by every broker rule
    bports_args = ['-p', 'tcp', '-m', 'multiport', '--dports', ','.join(broker_ports)]

    with _firewall_lock():
        # Snapshot the state *inside* the lock: taken any earlier, a
        # concurrent report could be re-added over by this worker's flush
        # and silently dropped from the chain.
        sources = allowed_ips(state)
        broker_sources = broker_allowed_ips(state)

        ok = True

        # --- JUDGE_DIRECT (port 8446, public IPs only) -------------------
        _ensure_chain(chain)
        if not _run([
            IPTABLES, '-C', 'INPUT', '-p', 'tcp', '--dport', port,
            '-j', chain,
        ], quiet=True):
            ok = _run([
                IPTABLES, '-I', 'INPUT', '1', '-p', 'tcp',
                '--dport', port, '-j', chain,
            ]) and ok

        ok = _run([IPTABLES, '-F', chain]) and ok
        for ip in sources:
            ok = _run([IPTABLES, '-A', chain, '-s', ip, '-j', 'ACCEPT']) and ok
        ok = _run([IPTABLES, '-A', chain, '-j', 'DROP']) and ok

        # --- OJ_JUDGE_BROKER (ports 6379+8446, includes private LAN) -----
        # This chain sits in VLESS_MIN_INPUT ahead of the catch-all DROP,
        # so it is the chain that actually gates port 8446 in production.
        _ensure_chain(broker_chain)
        ok = _run([IPTABLES, '-F', broker_chain]) and ok
        for ip in broker_sources:
            ok = _run(
                [IPTABLES, '-A', broker_chain, '-s', ip] + bports_args + ['-j', 'ACCEPT'],
            ) and ok
        ok = _run([IPTABLES, '-A', broker_chain, '-j', 'DROP']) and ok

    if not ok:
        logger.warning(
            'Direct judge API :%s firewall rebuild failed; chain may be stale',
            port,
        )
        return None

    logger.info(
        'Direct judge API :%s allows %s; broker allows %s',
        port, sources or '(none)', broker_sources or '(none)',
    )
    return sources
