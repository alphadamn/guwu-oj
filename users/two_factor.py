"""Time-based One-Time Password (TOTP, RFC 6238) two-factor authentication.

Stores the user's TOTP secret encrypted at rest with ``cryptography.Fernet``;
the symmetric key is derived from Django's ``SECRET_KEY`` so the secret is
only readable from inside the running application. The verification logic
uses the well-audited ``pyotp`` library and accepts a ±1 step window so a
user whose clock is a few seconds off is not locked out.

The same module powers both the user opt-in flow (profile page) and the
"staff-reauth" sudo mode used to gate destructive admin actions
(database backup / restore, bulk privilege changes, etc.).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets as _secrets
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# ``cryptography.fernet.Fernet`` is imported lazily so a missing optional
# dependency surfaces with a clear error only when 2FA is actually used.
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    Fernet = None  # type: ignore[assignment]
    class InvalidToken(Exception):
        pass

try:
    import pyotp
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    pyotp = None  # type: ignore[assignment]


# Window of TOTP steps (each 30s by default) to accept on either side of the
# current time. 1 → accept previous, current, and next step (the de-facto
# standard tolerance for clock skew).
DEFAULT_TOTP_VALID_WINDOW = 1
DIGITS = 6
STEP_SECONDS = 30

_ISSUER = '谷物 OJ'

# Derive a stable Fernet key from SECRET_KEY via PBKDF2-HMAC-SHA256 with a
# fixed salt. The salt does not need to be secret — its only purpose is to
# derive a distinct key for the "2FA secret at rest" use case so reusing
# SECRET_KEY directly never happens.
_KEY_SALT = b'guwu-oj-two-factor-secret-v1'
_KEY_INFO = b'fernet-key-v1'


def _fernet_key() -> bytes:
    if Fernet is None:
        raise RuntimeError(
            'cryptography is not installed; cannot manage 2FA secrets'
        )
    secret = getattr(settings, 'SECRET_KEY', '') or ''
    if not secret:
        raise RuntimeError('SECRET_KEY is not configured; cannot encrypt 2FA secrets')
    kdf = hashlib.pbkdf2_hmac(
        'sha256',
        secret.encode('utf-8'),
        _KEY_SALT,
        iterations=200_000,
        dklen=32,
    )
    return base64.urlsafe_b64encode(kdf)


def generate_secret() -> str:
    """Return a fresh base32-encoded TOTP secret (20 bytes / 160 bits)."""
    if pyotp is None:
        # Fallback: pyotp.random_base32() does the same thing.
        return base64.b32encode(_secrets.token_bytes(20)).decode('ascii')
    return pyotp.random_base32()


def encrypt_secret(secret_b32: str) -> str:
    """Encrypt a base32 secret for storage. Returns a URL-safe Fernet token
    as a string. Empty input is preserved as an empty string."""
    if not secret_b32:
        return ''
    if Fernet is None:
        # No cryptography available — store as-is (deployment without 2FA
        # support). Logged so it is visible without being noisy.
        logger.warning('Storing 2FA secret without encryption: cryptography missing')
        return secret_b32
    token = Fernet(_fernet_key()).encrypt(secret_b32.encode('ascii'))
    return token.decode('ascii')


def decrypt_secret(stored: str) -> str:
    """Decrypt a stored secret. Returns the base32 secret, or '' on failure."""
    if not stored:
        return ''
    if Fernet is None:
        return stored
    try:
        plain = Fernet(_fernet_key()).decrypt(stored.encode('ascii'))
        return plain.decode('ascii')
    except (InvalidToken, Exception) as exc:  # noqa: BLE001
        logger.warning('Failed to decrypt 2FA secret: %s', exc)
        return ''


def verify_code(secret_b32: str, code: str, *, window: int = DEFAULT_TOTP_VALID_WINDOW) -> bool:
    """Verify a 6-digit TOTP ``code`` against ``secret_b32``.

    ``window`` is the number of TOTP steps to tolerate on either side of the
    current step (1 → −1, 0, +1, i.e. ±30s).
    """
    if not secret_b32 or not code:
        return False
    code = (code or '').strip().replace(' ', '')
    if not code.isdigit() or len(code) != DIGITS:
        return False
    if pyotp is not None:
        try:
            return bool(pyotp.TOTP(secret_b32, digits=DIGITS, interval=STEP_SECONDS).verify(code, valid_window=window))
        except Exception as exc:  # noqa: BLE001
            logger.warning('pyotp verification failed: %s', exc)
            return False
    # Fallback RFC 6238 implementation if pyotp is missing.
    return _totp_verify_fallback(secret_b32, code, window=window)


def _totp_verify_fallback(secret_b32: str, code: str, *, window: int = 0) -> bool:
    """Pure-stdlib TOTP verification used when ``pyotp`` is unavailable."""
    import struct, time
    try:
        key = base64.b32decode(secret_b32, casefold=True)
    except Exception:
        return False
    now = int(time.time())
    for offset in range(-window, window + 1):
        counter = (now // STEP_SECONDS) + offset
        msg = struct.pack('>Q', counter)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        offset_byte = digest[-1] & 0x0F
        binary = struct.unpack('>I', digest[offset_byte:offset_byte + 4])[0] & 0x7FFFFFFF
        candidate = f'{binary % (10 ** DIGITS):0{DIGITS}d}'
        # Constant-time compare to avoid timing side channels.
        if hmac.compare_digest(candidate, code):
            return True
    return False


def otpauth_url(secret_b32: str, username: str) -> str:
    """Build the ``otpauth://`` provisioning URL embedded in setup QR codes."""
    if pyotp is not None:
        return pyotp.TOTP(secret_b32, digits=DIGITS, interval=STEP_SECONDS).provisioning_uri(
            name=username or '',
            issuer_name=_ISSUER,
        )
    # Fallback: build the URL ourselves (RFC 6238 / Google Authenticator format).
    from urllib.parse import quote, urlencode
    label = f'{_ISSUER}:{username or ""}'
    params = {
        'secret': secret_b32,
        'issuer': _ISSUER,
        'algorithm': 'SHA1',
        'digits': DIGITS,
        'period': STEP_SECONDS,
    }
    return f'otpauth://totp/{quote(label, safe=":")}?{urlencode(params)}'


# ---------------------------------------------------------------------------
# Backup / scratch codes
# ---------------------------------------------------------------------------

BACKUP_CODE_COUNT = 10
BACKUP_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no ambiguous chars
BACKUP_CODE_LENGTH = 8


def generate_backup_codes() -> list[str]:
    """Return ``BACKUP_CODE_COUNT`` random human-readable codes."""
    return [
        ''.join(_secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))
        for _ in range(BACKUP_CODE_COUNT)
    ]


def hash_backup_code(code: str) -> str:
    """Return a salted SHA-256 hash of a backup code (for storage)."""
    if not code:
        return ''
    salted = f'backup-code:{code.strip().upper()}:{_KEY_SALT.decode("ascii")}'
    return hashlib.sha256(salted.encode('utf-8')).hexdigest()


def hash_backup_codes(codes: list[str]) -> str:
    """Comma-joined list of hashed codes (for storage in a single TextField)."""
    return ','.join(hash_backup_code(c) for c in codes if c)


def verify_backup_code(stored_hashes: str, code: str) -> bool:
    """Return True if ``code`` matches any stored hash. The caller is
    responsible for removing the used hash from storage after success."""
    if not stored_hashes or not code:
        return False
    candidate = hash_backup_code(code)
    hashes = [h.strip() for h in stored_hashes.split(',') if h.strip()]
    return any(hmac.compare_digest(candidate, h) for h in hashes)


def consume_backup_code(stored_hashes: str, code: str) -> Optional[str]:
    """If ``code`` matches one of the stored hashes, return a new
    comma-joined string with that hash removed. Otherwise return None."""
    if not verify_backup_code(stored_hashes, code):
        return None
    candidate = hash_backup_code(code)
    hashes = [h.strip() for h in stored_hashes.split(',') if h.strip()]
    remaining = [h for h in hashes if not hmac.compare_digest(candidate, h)]
    return ','.join(remaining)
