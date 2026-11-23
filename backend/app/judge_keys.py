"""Saved judge keys: encrypted at rest, owned by one user account, and valid until an expiry date.

The server secret (AFC_JUDGE_KEY_SECRET) is a Fernet key. Without it, saving is refused, because a stored key must not
sit in the database as plain text. A leaked database alone does not expose the keys.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet


class JudgeKeyVault:
    def __init__(self, secret: str) -> None:
        self._fernet = Fernet(secret.encode())

    def seal(self, key: str) -> str:
        return self._fernet.encrypt(key.encode()).decode()

    def open(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()


class VaultSecretInvalid(ValueError):
    pass


def vault_from_env() -> JudgeKeyVault | None:
    """Build the vault from AFC_JUDGE_KEY_SECRET. Returns None when unset, which disables saving."""
    secret = os.getenv("AFC_JUDGE_KEY_SECRET")
    if not secret:
        return None
    try:
        return JudgeKeyVault(secret)
    except (ValueError, TypeError) as error:
        raise VaultSecretInvalid(
            "AFC_JUDGE_KEY_SECRET must be a Fernet key. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from error
