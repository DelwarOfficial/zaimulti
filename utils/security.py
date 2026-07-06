#!/usr/bin/env python3
"""
At-rest encryption for sensitive account data (cookies, passwords).

The project historically stored ``accounts.json`` in plain text. This module
introduces an opt-in, field-level encryption layer that keeps the file
shape and public API intact while protecting sensitive values when the
``cryptography`` package is installed.

Design:
- ``SecureStorage`` wraps a Fernet token derived from a user-provided key
  (env var ``ZAI_STORAGE_KEY``) or, when none is provided, an auto-generated
  one cached under ``accounts/.storage_key`` with restrictive permissions.
- Encryption is opt-in via ``config.encrypt_at_rest``. When disabled (the
  default to preserve backward compatibility) the storage layer is a
  transparent no-op pass-through.
- Only sensitive fields (``cookies``, ``password``) are encrypted. Metadata
  (email, status, timestamps) stays in plain text so the router can still
  introspect the pool without decrypting.

Security notes:
- This is defense-in-depth for the local store, not a vault. Anyone with
  read access to both the JSON and the key file can recover secrets.
- ``SecureStorage.disable()`` returns a plain passthrough instance, useful
  when ``cryptography`` is not installed.
- Decryption is best-effort: a corrupted token simply returns the original
  ciphertext so the caller can decide whether to drop the account.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from rich.console import Console

import config

console = Console()

# Prefix used to identify encrypted strings inside the JSON. Keeping it as a
# literal prefix means unencrypted legacy files stay valid.
_ENC_PREFIX = "enc::"

# Fields considered sensitive and therefore eligible for encryption.
SENSITIVE_FIELDS = ("cookies", "password")

_KEY_CACHE: Optional[bytes] = None


def _cryptography_available() -> bool:
    try:
        import cryptography  # noqa: F401
        from cryptography.fernet import Fernet  # noqa: F401
        return True
    except Exception:
        return False


def _derive_fernet_key(passphrase: Union[str, bytes]) -> bytes:
    """Derive a URL-safe Fernet key from an arbitrary passphrase."""
    from cryptography.fernet import Fernet
    if isinstance(passphrase, str):
        passphrase = passphrase.encode("utf-8")
    digest = hashlib.sha256(passphrase).digest()
    return base64.urlsafe_b64encode(digest)


def _load_or_create_key() -> bytes:
    """
    Load the cached storage key, or create one.

    Priority:
      1. Env var named by ``config.encryption_key_env_var`` (default ZAI_STORAGE_KEY).
      2. Cached key file under ``accounts/.storage_key``.
      3. Auto-generated and cached (with a warning).
    """
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE

    env_var = str(config.get("encryption_key_env_var", "ZAI_STORAGE_KEY"))
    raw = os.environ.get(env_var)
    if raw:
        _KEY_CACHE = _derive_fernet_key(raw)
        return _KEY_CACHE

    key_file = config.accounts_file().parent / ".storage_key"
    try:
        if key_file.exists():
            _KEY_CACHE = base64.urlsafe_b64decode(key_file.read_bytes().strip())
            return _KEY_CACHE
    except Exception:
        pass

    # Generate a fresh Fernet key and cache it.
    from cryptography.fernet import Fernet
    _KEY_CACHE = Fernet.generate_key()
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(base64.urlsafe_b64encode(_KEY_CACHE))
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass  # Windows chmod is best-effort.
        console.print(
            f"[yellow]Generated a new storage key at {key_file}. "
            f"Set {env_var} to use your own and prevent automatic rotation.[/yellow]"
        )
    except OSError as exc:
        console.print(f"[red]Could not persist storage key: {exc}[/red]")
    return _KEY_CACHE


class SecureStorage:
    """
    Field-level encrypt/decrypt for account dicts.

    Usage::

        store = SecureStorage.enabled() if config.get('encrypt_at_rest') else SecureStorage.disabled()
        record = store.encrypt_account(account_dict)   # before save
        plain  = store.decrypt_account(record)         # after load
    """

    def __init__(self, enabled: bool, key: Optional[bytes] = None):
        self.enabled = enabled and _cryptography_available()
        self._fernet = None
        if self.enabled:
            try:
                from cryptography.fernet import Fernet
                self._fernet = Fernet(key or _load_or_create_key())
            except Exception as exc:
                console.print(
                    f"[yellow]Encryption requested but unavailable ({exc}); "
                    f"falling back to plaintext.[/yellow]"
                )
                self.enabled = False

        if bool(config.get("require_encryption")) and not self.enabled:
            raise RuntimeError(
                "Encryption required (config.require_encryption=True) but "
                "the 'cryptography' package is unavailable."
            )

    # ----- Factory helpers -----
    @classmethod
    def from_config(cls) -> "SecureStorage":
        if bool(config.get("encrypt_at_rest", False)):
            return cls.enabled()
        return cls.disabled()

    @classmethod
    def enabled(cls, key: Optional[bytes] = None) -> "SecureStorage":
        return cls(enabled=True, key=key)

    @classmethod
    def disabled(cls) -> "SecureStorage":
        return cls(enabled=False)

    # ----- Low-level helpers -----
    def _encrypt_str(self, value: str) -> str:
        if not self.enabled or self._fernet is None or not value:
            return value
        try:
            token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
            return f"{_ENC_PREFIX}{token}"
        except Exception:
            return value

    def _decrypt_str(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith(_ENC_PREFIX):
            return value
        if self._fernet is None:
            return value
        token = value[len(_ENC_PREFIX):]
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception:
            # Leave as-is so caller can decide (e.g. drop the account).
            return value

    def _encrypt_value(self, value: Any) -> Any:
        """Encrypt a JSON-serialisable value (str / list / dict)."""
        if value is None:
            return None
        if isinstance(value, str):
            return self._encrypt_str(value)
        # Lists / dicts are serialised then encrypted as one blob - more
        # efficient than per-element encryption for cookie lists.
        try:
            return self._encrypt_str(json.dumps(value, ensure_ascii=False))
        except Exception:
            return value

    def _decrypt_value(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith(_ENC_PREFIX):
            plain = self._decrypt_str(value)
            try:
                return json.loads(plain)
            except Exception:
                return plain
        return value

    # ----- Public record API -----
    def encrypt_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy with sensitive fields encrypted."""
        out = dict(account)
        if not self.enabled:
            return out
        for field in SENSITIVE_FIELDS:
            if field in out and out[field] is not None:
                out[field] = self._encrypt_value(out[field])
        out["_encrypted"] = True
        return out

    def decrypt_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy with sensitive fields decrypted (best-effort)."""
        out = dict(account)
        for field in SENSITIVE_FIELDS:
            if field in out:
                out[field] = self._decrypt_value(out[field])
        out.pop("_encrypted", None)
        return out

    def encrypt_pool(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Encrypt every account in a pool dict."""
        out = dict(data)
        accounts = [self.encrypt_account(a) for a in data.get("accounts", [])]
        out["accounts"] = accounts
        if self.enabled:
            out["_encrypted"] = True
        return out

    def decrypt_pool(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Decrypt every account in a pool dict (best-effort)."""
        out = dict(data)
        accounts = [self.decrypt_account(a) for a in data.get("accounts", [])]
        out["accounts"] = accounts
        out.pop("_encrypted", None)
        return out


def is_encrypted_record(account: Dict[str, Any]) -> bool:
    return bool(account.get("_encrypted")) or any(
        isinstance(account.get(f), str) and account.get(f, "").startswith(_ENC_PREFIX)
        for f in SENSITIVE_FIELDS
    )


__all__ = [
    "SecureStorage",
    "SENSITIVE_FIELDS",
    "is_encrypted_record",
]
