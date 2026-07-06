#!/usr/bin/env python3
"""
Central configuration for the z_ai_multi_account project.

All tunable values (timeouts, cooldowns, rate-limit keywords, temp-mail
provider order, paths, encryption toggle, etc.) live here. Runtime overrides
are supported via an optional ``config.json`` placed next to this file and
via environment variables (prefix ``ZAI_``).

Importing ``config`` is cheap and side-effect free, so it is safe to load
from CLI entrypoints before the heavy Playwright stack is installed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config.json"


# ------------------------------------------------------------
# Defaults (used when no override is present)
# ------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    # ---- Paths ----
    "accounts_file": "accounts/accounts.json",
    "logs_dir": "logs",
    "data_dir": "data",
    "transcripts_dir": "redteam/transcripts",
    "cookies_dir": "cookies",

    # ---- Cooldowns (minutes) ----
    "exhausted_cooldown_min": 45,
    "invalid_cooldown_min": 15,
    "cooling_down_min": 5,

    # ---- Selection / rotation ----
    "soft_skip_recent_used_sec": 30,
    "max_validation_attempts": 6,

    # ---- Token / usage tracking ----
    # Rough chars-per-token heuristic used when real token counts are unknown.
    "chars_per_token_estimate": 4.0,
    # Soft cap that, when exceeded, lowers an account's selection priority
    # (does NOT mark exhausted - only re-balances load).
    "soft_token_budget_per_account": 200_000,

    # ---- Rate / exhaustion detection ----
    "rate_limit_keywords": [
        "rate limit", "too many requests", "too many", "quota", "exceeded",
        "token limit", "free limit", "usage limit", "please wait",
        "try again later", "limit reached", "tokens remaining: 0",
        "no more tokens", "insufficient quota", "429", "service unavailable",
    ],
    # Strong exhaustion signals - if seen, account is definitely exhausted.
    "hard_exhaustion_keywords": [
        "your free quota is exhausted", "no free tokens remaining",
        "upgrade your plan to continue", "account limit reached",
    ],
    # Signals that indicate invalid / dead session (not just exhausted).
    "invalid_session_keywords": [
        "session expired", "please sign in", "log in to continue",
        "unauthorized", "403", "forbidden", "account suspended",
        "account banned",
    ],

    # ---- Temp mail ----
    "temp_mail_providers": ["mail.tm", "guerrillamail", "1secmail"],
    "temp_mail_timeout_sec": 240,
    "temp_mail_poll_interval_sec": 5,
    "temp_mail_max_retries_per_provider": 2,

    # ---- Account creation ----
    "create_headless": False,  # visible by default for slider / captcha
    "create_min_delay_sec": 1.2,
    "create_max_delay_sec": 3.8,
    "create_wait_login_sec": 45,
    "create_wait_slider_sec": 30,
    "create_browser_args": [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas",
        "--no-first-run",
        "--no-zygote",
        "--disable-gpu",
    ],

    # ---- Red team driver ----
    "chat_input_timeout_sec": 10,
    "response_capture_max_wait_sec": 120,
    "response_capture_stable_polls": 3,
    "response_capture_min_len": 20,
    "response_stable_interval_sec": 2.2,

    # ---- Deep validation ----
    "deep_validation_enabled": True,
    "deep_validation_test_prompt": "ping",
    "deep_validation_timeout_sec": 30,
    "deep_validation_min_response_len": 2,

    # ---- Proxy ----
    "proxy_env_var": "ZAI_PROXIES",
    "proxy_assign_per_account": True,

    # ---- Security / storage ----
    # When True, sensitive fields (cookies, password) are encrypted at rest
    # using Fernet if the ``cryptography`` package is installed. If the
    # package is missing we fall back to plaintext and log a warning.
    "encrypt_at_rest": False,
    "encryption_key_env_var": "ZAI_STORAGE_KEY",
    # When True, refuse to start if encryption is requested but unavailable.
    "require_encryption": False,

    # ---- Logging ----
    "structured_logging": True,
    "log_level": "INFO",
}


# ------------------------------------------------------------
# Loader
# ------------------------------------------------------------

def _coerce(default: Any, value: Any) -> Any:
    """Best-effort coerce a JSON/env value to match the default's type."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return value


def _load_overrides() -> Dict[str, Any]:
    """Load config.json (if present) merged with ZAI_* env overrides."""
    overrides: Dict[str, Any] = {}

    # 1. config.json
    config_path = Path(os.environ.get("ZAI_CONFIG_FILE", DEFAULT_CONFIG_FILE))
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                overrides.update(json.load(f) or {})
        except (json.JSONDecodeError, OSError) as exc:
            # Defer printing: rich may not be the desired sink at import time.
            os.environ.setdefault("ZAI_CONFIG_LOAD_ERROR", str(exc))

    # 2. Env vars: ZAI_<UPPER_KEY> override scalar defaults.
    for key, default in DEFAULTS.items():
        env_key = f"ZAI_{key.upper()}"
        if env_key in os.environ:
            overrides[key] = _coerce(default, os.environ[env_key])

    return overrides


def get(key: str, default: Any = None) -> Any:
    """
    Lookup a config value with override resolution.

    Order: environment (ZAI_<KEY>) -> config.json -> DEFAULTS.
    Lists/dicts come from config.json only (env coercion is scalar-only).
    """
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    return DEFAULTS.get(key, default)


def as_dict() -> Dict[str, Any]:
    """Return the fully-resolved configuration as a dict (copy)."""
    merged = dict(DEFAULTS)
    merged.update(_OVERRIDES)
    return merged


def reload() -> Dict[str, Any]:
    """Force re-read of config.json / env. Mainly for tests + hot reload."""
    global _OVERRIDES
    _OVERRIDES = _load_overrides()
    return as_dict()


# Resolve once at import. Call ``config.reload()`` to refresh.
_OVERRIDES: Dict[str, Any] = _load_overrides()


# ------------------------------------------------------------
# Convenience accessors (typed shortcuts used across the codebase)
# ------------------------------------------------------------

def accounts_file() -> Path:
    p = Path(get("accounts_file", "accounts/accounts.json"))
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def logs_dir() -> Path:
    p = Path(get("logs_dir", "logs"))
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def data_dir() -> Path:
    p = Path(get("data_dir", "data"))
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def transcripts_dir() -> Path:
    p = Path(get("transcripts_dir", "redteam/transcripts"))
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def cookies_dir() -> Path:
    p = Path(get("cookies_dir", "cookies"))
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def rate_limit_keywords() -> List[str]:
    return list(get("rate_limit_keywords", []))


def hard_exhaustion_keywords() -> List[str]:
    return list(get("hard_exhaustion_keywords", []))


def invalid_session_keywords() -> List[str]:
    return list(get("invalid_session_keywords", []))


def estimate_tokens(text: str) -> int:
    """Rough token estimate from text length using configured heuristic."""
    if not text:
        return 0
    chars = len(text)
    per_token = float(get("chars_per_token_estimate", 4.0)) or 4.0
    return max(1, int(chars / per_token))


@dataclass
class Cooldowns:
    exhausted_min: int = field(default_factory=lambda: int(get("exhausted_cooldown_min", 45)))
    invalid_min: int = field(default_factory=lambda: int(get("invalid_cooldown_min", 15)))
    cooling_down_min: int = field(default_factory=lambda: int(get("cooling_down_min", 5)))


__all__ = [
    "PROJECT_ROOT",
    "DEFAULTS",
    "get", "as_dict", "reload",
    "accounts_file", "logs_dir", "data_dir",
    "transcripts_dir", "cookies_dir",
    "rate_limit_keywords", "hard_exhaustion_keywords", "invalid_session_keywords",
    "estimate_tokens", "Cooldowns",
]
