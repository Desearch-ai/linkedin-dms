"""Encryption at rest for auth and proxy data.

Key is read from env DESEARCH_ENCRYPTION_KEY (32-byte Fernet key, base64url).
If unset, no encryption is performed and a warning is logged once.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)
_warned_no_key = False


def _get_key() -> Optional[bytes]:
    global _warned_no_key
    raw = os.environ.get("DESEARCH_ENCRYPTION_KEY", "").strip()
    if not raw:
        if not _warned_no_key:
            logger.warning(
                "DESEARCH_ENCRYPTION_KEY not set; auth/proxy stored in plaintext. "
                "Set a Fernet key for encryption at rest."
            )
            _warned_no_key = True
        return None
    try:
        from cryptography.fernet import Fernet

        # Fernet key must be 32 bytes base64url
        return raw.encode("ascii")
    except Exception:
        logger.exception("Invalid DESEARCH_ENCRYPTION_KEY")
        return None


def encrypt_if_configured(plaintext: str) -> str:
    """Encrypt plaintext when a key is set; otherwise return as-is."""
    key = _get_key()
    if key is None:
        return plaintext
    try:
        from cryptography.fernet import Fernet

        f = Fernet(key)
        return f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    except Exception:
        logger.exception("Encryption failed")
        return plaintext


def decrypt_if_encrypted(ciphertext: str) -> str:
    """Decrypt if value looks like Fernet ciphertext; otherwise return as-is (legacy plaintext)."""
    if not ciphertext:
        return ciphertext
    key = _get_key()
    if key is None:
        return ciphertext
    try:
        from cryptography.fernet import Fernet, InvalidToken

        f = Fernet(key)
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        # Legacy: stored as plaintext before encryption was enabled
        return ciphertext
    except Exception:
        logger.exception("Decryption failed")
        return ciphertext
