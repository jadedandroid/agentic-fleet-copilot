"""Password hashing and session tokens. Standard library only, so no new dependencies."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from threading import Lock

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSWORD_LENGTH = 12
_TENANT_ID_PATTERN = re.compile(r"[^a-z0-9]+")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt_hex),
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=32,
    )
    return hmac.compare_digest(candidate, bytes.fromhex(digest_hex))


# Verified against when a login email is unknown, so unknown and wrong-password logins take similar time.
DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-placeholder")


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


def new_session_token() -> tuple[str, str]:
    """Return (token, token_hash). Only the hash is stored; the raw token is returned to the client once."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tenant_id_from_name(name: str) -> str:
    """Derive a stable tenant ID from a display name: lowercase, alphanumerics and dashes."""
    slug = _TENANT_ID_PATTERN.sub("-", name.strip().lower()).strip("-")
    return slug[:60]


class LoginThrottle:
    """Sliding-window limit on failed sign-ins per email. In memory, so it resets on restart.

    A key is blocked once `max_failures` failures fall inside `window`. The block lifts as the oldest of them expire.
    """

    def __init__(self, max_failures: int, window: timedelta) -> None:
        self._max = max_failures
        self._window = window
        self._failures: dict[str, list[datetime]] = {}
        self._lock = Lock()

    def _recent(self, key: str, now: datetime) -> list[datetime]:
        kept = [moment for moment in self._failures.get(key, []) if now - moment < self._window]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)
        return kept

    def retry_after(self, key: str, now: datetime) -> int | None:
        """Seconds until the next attempt is allowed, or None when the key is not blocked."""
        with self._lock:
            recent = self._recent(key, now)
            if len(recent) < self._max:
                return None
            # The block lifts once enough failures expire to drop below the limit.
            lifts_at = recent[len(recent) - self._max] + self._window
            return max(1, int((lifts_at - now).total_seconds()) + 1)

    def record_failure(self, key: str, now: datetime) -> None:
        with self._lock:
            self._recent(key, now)
            self._failures.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
