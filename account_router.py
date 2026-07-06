#!/usr/bin/env python3
"""
Z.ai Account Router & Smart Session Manager
===========================================

Production-grade multi-account manager for Z.ai (GLM-5.2 chat).

Features
--------
- Automatic smart rotation (least-used + cooldown aware + token-budget aware).
- Status tracking: ``active`` / ``exhausted`` / ``invalid`` / ``cooling_down``.
- Real per-account usage tracking: request count + approximate token usage
  (configurable heuristic) feeding back into rotation decisions.
- Multi-signal exhaustion detection: page content + chat response +
  hard-exhaustion keywords + invalid-session keywords.
- Deep session validation: optional lightweight test message probe.
- Per-account proxy binding with sticky assignment.
- Optional at-rest encryption for sensitive fields.
- Clean importable API for GLM / ZCode coding agents.
- Structured JSONL logging + legacy plain-text rotation log.

The public API is preserved for backward compatibility::

    get_next_session, get_valid_session, mark_exhausted, mark_invalid,
    rotate_account, report_request, get_account_by_email,
    create_playwright_context_with_cookies, validate_session, list_accounts

Usage from a coding agent::

    from account_router import get_next_session, mark_exhausted
    session = get_next_session()
    # ... use session["cookies"] ...
    if hit_rate_limit():
        mark_exhausted(session["email"])
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table

import config
from utils.logging_config import get_logger, log_event, log_exception
from utils.proxy import ProxyManager, get_default_manager, parse_proxy_url
from utils.security import SecureStorage

console = Console()
log = get_logger("zai.router")


# ------------------------------------------------------------
# Playwright lazy loader (lets --help work pre-install)
# ------------------------------------------------------------

def _get_playwright_stealth():
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    return sync_playwright, Stealth


# ------------------------------------------------------------
# Persistence (config-driven + optional encryption)
# ------------------------------------------------------------

_storage = SecureStorage.from_config()


def _accounts_file() -> Path:
    return config.accounts_file()


def load_accounts() -> Dict[str, Any]:
    """Load accounts store (decrypting sensitive fields if enabled)."""
    path = _accounts_file()
    if not path.exists():
        return {"accounts": [], "last_updated": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log_exception(log, exc, "load_accounts")
        return {"accounts": [], "last_updated": None}
    if "accounts" not in data or not isinstance(data["accounts"], list):
        data["accounts"] = []
    # Decrypt in-place for in-memory use.
    data = _storage.decrypt_pool(data)
    return data


def save_accounts(data: Dict[str, Any]) -> None:
    """Persist accounts store (encrypting sensitive fields if enabled)."""
    path = _accounts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _storage.encrypt_pool(data)
    payload["last_updated"] = datetime.now().isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        tmp.replace(path)  # atomic on same filesystem
    except OSError as exc:
        log_exception(log, exc, "save_accounts")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


# ------------------------------------------------------------
# Cooldown + selection helpers
# ------------------------------------------------------------

def _is_in_cooldown(account: Dict[str, Any]) -> bool:
    cd = account.get("cooldown_until")
    if not cd:
        return False
    try:
        return datetime.fromisoformat(cd) > datetime.now()
    except (TypeError, ValueError):
        return False


def _account_token_pressure(acc: Dict[str, Any]) -> float:
    """
    Return a 0..1 score of how 'loaded' this account is for the current
    soft token budget. 0 = fresh, 1+ = saturated.
    """
    budget = float(config.get("soft_token_budget_per_account", 200_000)) or 1.0
    used = float(acc.get("approx_tokens", 0) or 0)
    return used / budget


def _get_healthy_accounts(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return accounts usable right now."""
    healthy: List[Dict[str, Any]] = []
    now = datetime.now()
    soft_skip = float(config.get("soft_skip_recent_used_sec", 30))
    for acc in accounts:
        status = acc.get("status", "active")
        if status != "active":
            continue
        if _is_in_cooldown(acc):
            continue
        last = acc.get("last_used")
        if last and soft_skip > 0:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < soft_skip:
                    continue
            except (TypeError, ValueError):
                pass
        healthy.append(acc)
    return healthy


def _select_best_account(healthy: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Pick the healthiest account.

    Sort key (ascending) - lower is better:
      1. Token pressure (saturated accounts sink to the bottom).
      2. Total usage (request_count + usage_count).
      3. Last-used timestamp (oldest first).
    """
    if not healthy:
        return None

    def sort_key(acc: Dict[str, Any]):
        pressure = _account_token_pressure(acc)
        usage = int(acc.get("usage_count", 0)) + int(acc.get("request_count", 0))
        last = acc.get("last_used") or "1970-01-01T00:00:00"
        return (pressure, usage, last)

    healthy.sort(key=sort_key)
    return healthy[0]


def get_next_healthy_account() -> Optional[Dict[str, Any]]:
    """Smart selection of next usable account (lowest pressure + usage)."""
    data = load_accounts()
    accounts: List[Dict[str, Any]] = data.get("accounts", [])

    if not accounts:
        console.print("[red]No accounts in pool. Create some first.[/red]")
        return None

    healthy = _get_healthy_accounts(accounts)
    if not healthy:
        console.print("[yellow]No healthy accounts available right now.[/yellow]")
        return None

    account = _select_best_account(healthy)
    if not account:
        return None

    account["last_used"] = datetime.now().isoformat()
    account["usage_count"] = int(account.get("usage_count", 0)) + 1
    save_accounts(data)
    log_event(account["email"], "SELECTED", "smart rotation (low pressure + usage)")
    return account


# ------------------------------------------------------------
# Exhaustion / limit detection (multi-signal)
# ------------------------------------------------------------

def _match_keywords(text: str, keywords: List[str]) -> List[str]:
    if not text or not keywords:
        return []
    low = text.lower()
    return [kw for kw in keywords if kw in low]


def detect_exhaustion(content: str) -> Dict[str, Any]:
    """
    Inspect arbitrary text (page content or chat response) for limit signals.

    Returns a dict::

        {
          "is_rate_limited": bool,   # soft signals
          "is_hard_exhausted": bool, # strong signals -> definitely done
          "is_invalid": bool,        # auth / ban signals
          "matched": [list of matched keywords],
        }
    """
    rate = _match_keywords(content, config.rate_limit_keywords())
    hard = _match_keywords(content, config.hard_exhaustion_keywords())
    invalid = _match_keywords(content, config.invalid_session_keywords())
    return {
        "is_rate_limited": bool(rate) and not invalid,
        "is_hard_exhausted": bool(hard),
        "is_invalid": bool(invalid),
        "matched": rate + hard + invalid,
    }


def _contains_rate_limit_signal(page_content: str) -> bool:
    """Backward-compatible helper for callers that just want a bool."""
    res = detect_exhaustion(page_content)
    return res["is_rate_limited"] or res["is_hard_exhausted"]


# ------------------------------------------------------------
# Session validation
# ------------------------------------------------------------

def _launch_browser_with_account(account: Dict[str, Any], headless: bool = True):
    """
    Launch Playwright + stealth context with cookies + proxy applied.

    Returns (playwright, browser, context, page) or raises on failure.
    Caller owns browser.close() / playwright.stop().
    """
    sync_playwright, StealthCls = _get_playwright_stealth()
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=headless)
    ctx_kwargs: Dict[str, Any] = {}
    proxy_str = account.get("proxy")
    if proxy_str:
        parsed = parse_proxy_url(proxy_str)
        if parsed:
            ctx_kwargs["proxy"] = parsed

    ctx = browser.new_context(**ctx_kwargs)
    StealthCls().apply_stealth_sync(ctx)
    cookies = account.get("cookies", []) or []
    if cookies:
        ctx.add_cookies(cookies)
    page = ctx.new_page()
    return p, browser, ctx, page


def _send_test_message(page, prompt: str, timeout_sec: int) -> Optional[str]:
    """
    Send a lightweight test prompt and return the assistant text (best-effort).

    Imported lazily so the router doesn't pull the red-team stack at import
    time.  Returns None on any failure - the caller treats that as 'deep
    validation inconclusive' rather than 'invalid'.
    """
    try:
        from utils import selectors
        from redteam.driver import _send_prompt, _capture_response  # type: ignore
    except Exception:
        return None

    try:
        if not _send_prompt(page, prompt):
            return None
        return _capture_response(page, max_wait_seconds=timeout_sec)
    except Exception as exc:
        log_exception(log, exc, "deep_validation_send")
        return None


def validate_session(
    account: Dict[str, Any],
    deep_check: bool = True,
    *,
    test_message: Optional[str] = None,
) -> bool:
    """
    Validate session.

    Steps:
      1. Load cookies + proxy into a stealth context.
      2. Open chat.z.ai; fail fast if redirected to /auth or /login.
      3. (Optional) Scan page content for rate/quota signals.
      4. (Optional) Send a tiny test message and check the reply for
         rate-limit / invalid signals.

    ``deep_check`` toggles both content scan + test message unless overridden
    by config (``deep_validation_enabled``).
    """
    email = account.get("email", "unknown")
    do_deep = deep_check and bool(config.get("deep_validation_enabled", True))
    test_prompt = test_message or str(config.get("deep_validation_test_prompt", "ping"))

    p = browser = ctx = page = None
    try:
        p, browser, ctx, page = _launch_browser_with_account(account, headless=True)
        page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=20000)
        time.sleep(1.5)

        url = (page.url or "").lower()
        if "auth" in url or "/login" in url:
            log_event(email, "VALIDATE_FAIL", "redirected to auth", level=30)
            console.print(f"[red]Session invalid (redirected to auth): {email}[/red]")
            return False

        if do_deep:
            # Phase A: passive content scan.
            try:
                page.wait_for_timeout(1500)
                content = page.locator("body").inner_text() or ""
                signals = detect_exhaustion(content)
                if signals["is_invalid"]:
                    log_event(email, "VALIDATE_FAIL", "invalid signals: " + ",".join(signals["matched"]), level=30)
                    console.print(f"[red]Invalid session signals for {email}: {signals['matched']}[/red]")
                    return False
                if signals["is_hard_exhausted"] or signals["is_rate_limited"]:
                    log_event(email, "VALIDATE_FAIL", "limit signals: " + ",".join(signals["matched"]), level=30)
                    console.print(f"[yellow]Rate / token limit detected for {email}[/yellow]")
                    return False
            except Exception as exc:
                log_exception(log, exc, "deep_validation_scan")

            # Phase B: lightweight test message probe.
            if bool(config.get("deep_validation_enabled", True)):
                try:
                    reply = _send_test_message(
                        page,
                        test_prompt,
                        int(config.get("deep_validation_timeout_sec", 30)),
                    )
                    if reply is not None:
                        min_len = int(config.get("deep_validation_min_response_len", 2))
                        if len(reply.strip()) < min_len:
                            # Empty reply without explicit error text - inconclusive.
                            log_event(email, "VALIDATE_WARN", "test message returned empty reply")
                        reply_signals = detect_exhaustion(reply or "")
                        if reply_signals["is_invalid"]:
                            log_event(email, "VALIDATE_FAIL", "test reply invalid", level=30)
                            return False
                        if reply_signals["is_hard_exhausted"] or reply_signals["is_rate_limited"]:
                            log_event(email, "VALIDATE_FAIL", "test reply limit: " + ",".join(reply_signals["matched"]), level=30)
                            return False
                except Exception as exc:
                    log_exception(log, exc, "deep_validation_message")

        log_event(email, "VALIDATED")
        return True

    except Exception as exc:
        log_exception(log, exc, f"validate_session:{email}")
        console.print(f"[red]Validation error for {email}: {exc}[/red]")
        return False
    finally:
        for closer in (page, ctx, browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        if p is not None:
            try:
                p.stop()
            except Exception:
                pass


def _sync_token_to_zcode(account: Dict[str, Any]) -> None:
    """Extract JWT token from cookies and update ZCode config.json automatically."""
    cookies = account.get("cookies", []) or []
    token_val = None
    for cookie in cookies:
        if cookie.get("name") == "token":
            token_val = cookie.get("value")
            break

    if not token_val:
        return

    config_path = Path("C:/Users/Admin/.zcode/v2/config.json")
    if not config_path.exists():
        return

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        updated = False
        providers = data.get("provider", {})
        for p_name in ["builtin:zai-start-plan", "builtin:zai-coding-plan"]:
            if p_name in providers:
                providers[p_name].setdefault("options", {})["apiKey"] = token_val
                updated = True

        if updated:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            console.print(f"[dim][ZCode] Automatically synced token for {account['email']} to ZCode config.json[/dim]")
    except Exception as exc:
        log.warning(f"Failed to sync token to ZCode config.json: {exc}")


# ------------------------------------------------------------
# Public session entry points
# ------------------------------------------------------------

def get_valid_session(deep_check: bool = True) -> Optional[Dict[str, Any]]:
    """
    Main entry point for the coding agent.
    Returns a ready-to-use account dict or None, rotating on failure.
    """
    max_attempts = int(config.get("max_validation_attempts", 6))
    attempts = 0

    while attempts < max_attempts:
        account = get_next_healthy_account()
        if not account:
            return None

        console.print(f"[cyan]Validating session: {account['email']}[/cyan]")
        if validate_session(account, deep_check=deep_check):
            console.print(f"[bold green][OK] Valid session ready: {account['email']}[/bold green]")
            _sync_token_to_zcode(account)
            return account

        # Decide status from the failure: invalid vs exhausted.
        _mark_validation_failure(account["email"])
        attempts += 1

    console.print("[red]No valid sessions after retries. Pool may need new accounts.[/red]")
    return None


def _mark_validation_failure(email: str) -> None:
    """
    Best-effort classification of a failed validation.

    We re-open the account record to inspect the most recent stored
    failure reason if present; otherwise default to 'exhausted' (safer than
    invalid, which would burn the account for longer).
    """
    data = load_accounts()
    for acc in data.get("accounts", []):
        if acc.get("email") != email:
            continue
        reason = (acc.get("last_failure_reason") or "").lower()
        if any(k in reason for k in config.invalid_session_keywords()):
            acc["status"] = "invalid"
            acc["cooldown_until"] = (
                datetime.now() + timedelta(minutes=int(config.get("invalid_cooldown_min", 15)))
            ).isoformat()
            log_event(email, "MARKED_INVALID", "validation failure (auth signals)")
        else:
            acc["status"] = "exhausted"
            acc["exhausted_at"] = datetime.now().isoformat()
            acc["cooldown_until"] = (
                datetime.now() + timedelta(minutes=int(config.get("exhausted_cooldown_min", 45)))
            ).isoformat()
            log_event(email, "MARKED_EXHAUSTED", "validation failed")
        save_accounts(data)
        return


def list_accounts() -> None:
    """Pretty-print pool status."""
    data = load_accounts()
    accounts = data.get("accounts", [])
    if not accounts:
        console.print("[yellow]No accounts found.[/yellow]")
        return

    table = Table(title="Z.ai Account Pool")
    table.add_column("Email", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Created", style="dim")
    table.add_column("Last Used", style="dim")
    table.add_column("Usage", justify="right")
    table.add_column("Requests", justify="right")
    table.add_column("~Tokens", justify="right")
    table.add_column("Proxy", style="magenta")
    table.add_column("Cooldown", style="yellow")

    for acc in accounts:
        status = acc.get("status", "active")
        color = {
            "active": "green", "exhausted": "yellow",
            "invalid": "red", "cooling_down": "magenta",
        }.get(status, "white")

        last = acc.get("last_used")
        last_str = last[:16] if last else "Never"
        cd = acc.get("cooldown_until")
        cd_str = cd[:16] if cd and _is_in_cooldown(acc) else ""
        proxy = acc.get("proxy") or "-"
        proxy_short = proxy.split("@")[-1] if "@" in (proxy or "") else proxy

        table.add_row(
            acc.get("email", "?"),
            f"[{color}]{status}[/{color}]",
            str(acc.get("created_at", "N/A"))[:10],
            last_str,
            str(acc.get("usage_count", 0)),
            str(acc.get("request_count", 0)),
            str(acc.get("approx_tokens", 0)),
            proxy_short,
            cd_str,
        )

    console.print(table)


# ============================================================
# PUBLIC GLM / ZCODE AGENT INTEGRATION API
# ============================================================

def get_next_session(deep_check: bool = True) -> Optional[Dict[str, Any]]:
    """
    Primary function for coding agents.
    Returns account dict with cookies ready for context.add_cookies().
    Automatically rotates on bad health.
    """
    return get_valid_session(deep_check=deep_check)


def rotate_account(reason: str = "manual rotate") -> Optional[Dict[str, Any]]:
    """Force pick a different account. Useful after seeing a limit in chat."""
    acc = get_next_healthy_account()
    if acc:
        log_event(acc["email"], "ROTATED", reason)
    return acc


def mark_exhausted(
    email: str,
    cooldown_minutes: Optional[int] = None,
    *,
    reason: str = "agent reported limit",
) -> None:
    """
    Mark an account as exhausted. Called when a rate/token limit is hit.

    The cooldown duration auto-extends when the account keeps getting marked
    exhausted (exponential-ish backoff up to 4x the base cooldown) to avoid
    hammering a truly-spent quota.
    """
    cd_min = int(cooldown_minutes if cooldown_minutes is not None
                 else config.get("exhausted_cooldown_min", 45))
    data = load_accounts()
    for acc in data.get("accounts", []):
        if acc.get("email") != email:
            continue

        prior_count = int(acc.get("exhausted_count", 0))
        new_count = prior_count + 1
        # Backoff: 2x on 2nd consecutive, 3x on 3rd, capped at 4x.
        multiplier = min(4, max(1, new_count))
        effective_cd = cd_min * multiplier

        acc["status"] = "exhausted"
        acc["exhausted_at"] = datetime.now().isoformat()
        acc["exhausted_count"] = new_count
        acc["cooldown_until"] = (
            datetime.now() + timedelta(minutes=effective_cd)
        ).isoformat()
        acc["last_failure_reason"] = reason
        save_accounts(data)
        log_event(
            email, "MARKED_EXHAUSTED",
            f"{reason} ({effective_cd}m cooldown, n={new_count})",
        )
        console.print(
            f"[yellow]Marked {email} exhausted for ~{effective_cd} min "
            f"(occurrence #{new_count})[/yellow]"
        )
        return

    console.print(f"[red]mark_exhausted: account not found: {email}[/red]")


def mark_invalid(email: str, reason: str = "session invalid") -> None:
    """Mark account as bad (dead cookies, banned, auth failure)."""
    data = load_accounts()
    for acc in data.get("accounts", []):
        if acc.get("email") != email:
            continue
        acc["status"] = "invalid"
        acc["cooldown_until"] = (
            datetime.now() + timedelta(minutes=int(config.get("invalid_cooldown_min", 15)))
        ).isoformat()
        acc["last_failure_reason"] = reason
        save_accounts(data)
        log_event(email, "MARKED_INVALID", reason)
        console.print(f"[red]Marked {email} as invalid ({reason})[/red]")
        return
    console.print(f"[red]mark_invalid: account not found: {email}[/red]")


def report_request(
    email: str,
    tokens_used: int = 0,
    *,
    prompt_text: Optional[str] = None,
    response_text: Optional[str] = None,
) -> None:
    """
    Lightweight per-request usage tracking.

    Either pass an explicit ``tokens_used`` (preferred when known) or pass
    the raw ``prompt_text`` + ``response_text`` and we'll estimate via the
    configured ``chars_per_token_estimate`` heuristic. The estimate feeds
    back into rotation pressure via the soft token budget.
    """
    if tokens_used <= 0 and (prompt_text or response_text):
        tokens_used = config.estimate_tokens((prompt_text or "") + (response_text or ""))

    data = load_accounts()
    for acc in data.get("accounts", []):
        if acc.get("email") != email:
            continue
        acc["request_count"] = int(acc.get("request_count", 0)) + 1
        if tokens_used > 0:
            acc["approx_tokens"] = int(acc.get("approx_tokens", 0)) + int(tokens_used)
        # If we've crossed the soft budget, demote priority but keep active.
        save_accounts(data)
        return


def report_response(email: str, response_text: str, prompt_text: str = "") -> Dict[str, Any]:
    """
    Inspect a captured response and update account state accordingly.

    Returns the ``detect_exhaustion`` signals dict so callers can react.

    Use this right after every chat turn to keep usage + state in sync::

        signals = report_response(email, response, prompt)
        if signals["is_hard_exhausted"] or signals["is_rate_limited"]:
            # router already marked exhausted; you can rotate.
            ...
    """
    signals = detect_exhaustion(response_text or "")
    if signals["is_invalid"]:
        mark_invalid(email, reason="invalid signals in response: " + ",".join(signals["matched"]))
    elif signals["is_hard_exhausted"]:
        mark_exhausted(email, reason="hard exhaustion: " + ",".join(signals["matched"]))
    elif signals["is_rate_limited"]:
        mark_exhausted(email, reason="rate limit: " + ",".join(signals["matched"]))
    else:
        report_request(email, prompt_text=prompt_text, response_text=response_text)
    return signals


def get_account_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Fetch full record."""
    data = load_accounts()
    for acc in data.get("accounts", []):
        if acc.get("email") == email:
            return acc
    return None


def create_playwright_context_with_cookies(
    cookies: List[Dict[str, Any]],
    proxy: Optional[str] = None,
    headless: bool = True,
):
    """
    Convenience helper: returns (playwright, browser, context, page)
    preloaded with account cookies + stealth + optional proxy.
    Caller owns browser.close(); p.stop().
    """
    sync_playwright, StealthCls = _get_playwright_stealth()
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=headless)
    ctx_kwargs: Dict[str, Any] = {}
    if proxy:
        parsed = parse_proxy_url(proxy)
        if parsed:
            ctx_kwargs["proxy"] = parsed
    context = browser.new_context(**ctx_kwargs)
    StealthCls().apply_stealth_sync(context)
    if cookies:
        context.add_cookies(cookies)
    page = context.new_page()
    return p, browser, context, page


def export_account_cookies(email: str, out_path: Optional[str] = None) -> Optional[str]:
    """Export cookies for an account to a JSON file."""
    acc = get_account_by_email(email)
    if not acc:
        return None
    out = out_path or f"cookies/{email.replace('@', '_').replace('.', '_')}.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"email": email, "cookies": acc.get("cookies", [])}, f, indent=2)
    console.print(f"[green]Exported cookies for {email} -> {out}[/green]")
    return out


def assign_proxy(email: str, proxy: str) -> bool:
    """Bind a specific proxy to an account (sticky assignment)."""
    data = load_accounts()
    for acc in data.get("accounts", []):
        if acc.get("email") == email:
            acc["proxy"] = proxy
            save_accounts(data)
            get_default_manager().bind(email, proxy)
            log_event(email, "PROXY_BOUND", proxy)
            return True
    return False


def warm_account(email: str) -> bool:
    """Optional: perform a light chat interaction to 'warm' a fresh account."""
    acc = get_account_by_email(email)
    if not acc:
        return False
    try:
        from redteam.driver import run_single_interaction
        res = run_single_interaction(
            "Hello, can you confirm you are ready? Reply with 'OK'.",
            acc, headless=True,
        )
        return bool(res.get("success", False))
    except Exception as e:
        log_exception(log, e, "warm_account")
        console.print(f"[yellow]Warm failed: {e}[/yellow]")
        return False


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def main():
    import sys
    if sys.platform.startswith("win"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    import argparse

    parser = argparse.ArgumentParser(description="Z.ai Smart Account Router")
    parser.add_argument("--create", action="store_true", help="Create new account (auto)")
    parser.add_argument("--list", action="store_true", help="List all accounts + status")
    parser.add_argument("--get-session", action="store_true", help="Get next healthy session")
    parser.add_argument("--validate-all", action="store_true", help="Re-validate active accounts")
    parser.add_argument("--probe", metavar="EMAIL", help="Deep probe one account for limits")
    parser.add_argument("--assign-proxy", nargs=2, metavar=("EMAIL", "PROXY"),
                        help="Sticky-bind a proxy to an account")
    args = parser.parse_args()

    if args.create:
        from create_account import create_zai_account
        create_zai_account()

    elif args.list:
        list_accounts()

    elif args.get_session:
        account = get_valid_session()
        if account:
            console.print("\n[bold green]Session ready for GLM/ZCode agent[/bold green]")
            console.print(f"Email: {account['email']}")
            console.print("Cookies length:", len(account.get("cookies", [])))
            console.print("Import: from account_router import get_next_session")

    elif args.validate_all:
        data = load_accounts()
        for acc in data.get("accounts", []):
            if acc.get("status") in ("active", None):
                console.print(f"Checking {acc['email']}...")
                if not validate_session(acc, deep_check=True):
                    _mark_validation_failure(acc["email"])
        console.print("[green]Re-validation done.[/green]")

    elif args.probe:
        acc = get_account_by_email(args.probe)
        if acc:
            console.print(f"Probing {args.probe}...")
            ok = validate_session(acc, deep_check=True)
            console.print("Healthy:", ok)
        else:
            console.print("[red]Account not found[/red]")

    elif args.assign_proxy:
        email, proxy = args.assign_proxy
        if assign_proxy(email, proxy):
            console.print(f"[green]Bound {proxy} -> {email}[/green]")
        else:
            console.print("[red]Account not found[/red]")

    else:
        parser.print_help()
        console.print("\nGLM agent tip: use get_next_session() / mark_exhausted(email)")
        console.print("Red team mode: use `python redteam.py` for campaigns, bulk, probes.")


if __name__ == "__main__":
    main()
