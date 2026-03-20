"""Encryption at rest for auth and proxy data.

Key is read from DESEARCH_ENCRYPTION_KEY env var (Fernet key, 32 bytes base64url).
If unset, data is stored in plaintext with a one-time warning.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_warned_no_key = False


def validate_fernet_key(raw: str) -> bytes:
    """Validate that a string is a well-formed Fernet key.

    A Fernet key is 44 characters of URL-safe base64 encoding 32 bytes.
    Raises ValueError with a clear message if the key is malformed.
    """
    encoded = raw.encode("ascii")
    if len(raw) != 44:
        raise ValueError(
            f"DESEARCH_ENCRYPTION_KEY must be 44 characters (got {len(raw)}). "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    from cryptography.fernet import Fernet

    try:
        Fernet(encoded)
    except Exception as exc:
        raise ValueError(f"DESEARCH_ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc
    return encoded


def _get_fernet_key() -> Optional[bytes]:
    """Read and validate the Fernet key from environment."""
    from libs.core.redaction import configure_logging
    configure_logging()  # ensure redaction filter is active before we log

    global _warned_no_key
    raw = os.environ.get("DESEARCH_ENCRYPTION_KEY", "").strip()
    if not raw:
        if not _warned_no_key:
            logger.warning(
                "DESEARCH_ENCRYPTION_KEY not set; auth/proxy stored in plaintext. "
                "Generate a key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
            _warned_no_key = True
        return None
    return validate_fernet_key(raw)


def encrypt_if_configured(plaintext: str) -> str:
    """Encrypt plaintext with Fernet if a key is configured, otherwise return as-is."""
    key = _get_fernet_key()
    if key is None:
        return plaintext
    from cryptography.fernet import Fernet

    f = Fernet(key)
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_if_encrypted(ciphertext: str) -> str:
    """Decrypt Fernet ciphertext if a key is configured.

    Falls back gracefully for legacy plaintext rows (pre-encryption data).
    """
    if not ciphertext:
        return ciphertext
    key = _get_fernet_key()
    if key is None:
        return ciphertext
    from cryptography.fernet import Fernet, InvalidToken

    try:
        f = Fernet(key)
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ciphertext
