#!/usr/bin/env python3
"""
Z.ai Account Creator - Fully Automatic
======================================

- Playwright + playwright-stealth
- Multi-provider temporary email with automatic fallback (mail.tm /
  1secmail / guerrillamail) via ``utils.temp_mail.TempMailManager``.
- Multi-strategy element detection (``utils.selectors``) so the flow
  survives UI text changes - text, role, data-testid, JS DOM scan.
- Optional per-account proxy assignment via ``utils.proxy``.
- Human-like typing + delays driven by ``config``.
- Visible browser by default so human challenges (slider / captcha) can be
  solved manually; the script pauses only when such a challenge is detected.
- Saves rich metadata (cookies, fingerprint, password, proxy) through the
  router's persistence layer (encryption-aware).
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeout, Page
from playwright_stealth import Stealth
from rich.console import Console

import config
from utils import selectors
from utils.fingerprint import get_random_fingerprint
from utils.logging_config import get_logger, log_event, log_exception
from utils.proxy import get_default_manager, parse_proxy_url
from utils.temp_mail import NewMailboxAfterFallback, TempMailManager

console = Console()
log = get_logger("zai.creator")


# ============================================================
# Helpers
# ============================================================

def human_delay(min_s: Optional[float] = None, max_s: Optional[float] = None) -> None:
    """Random human-like delay driven by config defaults."""
    lo = float(min_s if min_s is not None else config.get("create_min_delay_sec", 1.2))
    hi = float(max_s if max_s is not None else config.get("create_max_delay_sec", 3.8))
    time.sleep(random.uniform(lo, hi))


def generate_password() -> str:
    """Generate a reasonably strong password for signup."""
    base = "Zai" + str(random.randint(100000, 999999))
    return base + "!" + random.choice(["x", "p", "q", "r"]) + str(random.randint(10, 99))


def _detect_slider_or_captcha(page: Page) -> bool:
    """Heuristic detection for common slider / captcha elements."""
    texts = [
        "slider", "verify", "security check", "drag", "captcha",
        "human", "drag the", "slide to", "puzzle",
    ]
    try:
        content = (page.locator("body").inner_text() or "").lower()
        if any(t in content for t in texts):
            return True
    except Exception:
        pass
    # Also check for common captcha iframes.
    for sel in ['iframe[src*="captcha"]', 'iframe[src*="recaptcha"]',
                'iframe[title*="captcha"]', '[class*="captcha"]']:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _is_logged_in(page: Page) -> bool:
    """Heuristically detect whether the chat UI is loaded + interactive."""
    try:
        # Strongest signal: chat input is present + visible.
        if selectors.find_chat_input(page, timeout=2500) is not None:
            return True
    except Exception:
        pass
    try:
        url = (page.url or "").lower()
        if "chat.z.ai" in url and "/auth" not in url and "/login" not in url:
            return True
    except Exception:
        pass
    return False


def _auto_wait_for_login(page: Page, max_wait: int = 0) -> bool:
    """Poll for login success. Returns True once the chat UI is ready."""
    if max_wait <= 0:
        max_wait = int(config.get("create_wait_login_sec", 45))
    start = time.time()
    while time.time() - start < max_wait:
        if _is_logged_in(page):
            return True
        try:
            page.wait_for_timeout(1200)
        except Exception:
            time.sleep(1.2)
    return False


def _wait_with_fallback(page: Page, seconds: float, message: str = "") -> None:
    """Wait ``seconds`` using page.wait_for_timeout, falling back to sleep."""
    if message:
        console.print(f"[cyan]{message}[/cyan]")
    try:
        page.wait_for_timeout(int(seconds * 1000))
    except Exception:
        time.sleep(seconds)


# ============================================================
# Signup flow
# ============================================================

def _build_context_args(fingerprint: Dict[str, Any]) -> Dict[str, Any]:
    """Build Playwright new_context kwargs from a fingerprint."""
    return {
        "user_agent": fingerprint["user_agent"],
        "viewport": fingerprint["viewport"],
        "locale": fingerprint["locale"],
        "timezone_id": fingerprint["timezone_id"],
        "device_scale_factor": fingerprint["device_scale_factor"],
        "permissions": ["geolocation"],
        "extra_http_headers": {
            "sec-ch-ua": '"Chromium";v="133", "Not;A=Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    }


def _submit_signup_form(page: Page, email: str, password: str) -> None:
    """Drive the registration form using the shared selector library."""
    # 1. Click the "Continue with Email" entry point.
    if not selectors.click_email_option(page, timeout=6000):
        console.print(
            "[yellow]Could not auto-click the email option. "
            "Please click it in the visible browser if needed.[/yellow]"
        )
        _wait_with_fallback(page, 15, "Waiting 15s for manual email-option click...")
    human_delay(1.5, 3.0)

    # Wait for the email input to ensure the form loaded
    try:
        page.wait_for_selector('input[type="email"], input[placeholder*="email" i]', timeout=15000)
    except Exception:
        pass

    # Click the "Sign up" toggle if the form defaults to "Sign in" mode
    try:
        # Check if the page has a "Sign up" link/span
        signup_toggle = page.locator('a:has-text("Sign up"), span:has-text("Sign up"), button:has-text("Sign up")').first
        if signup_toggle.count() > 0 and signup_toggle.is_visible():
            # If "Already have an account?" is not on the page, we are in Sign In mode
            already_have = page.locator('text="Already have an account?"').first
            if already_have.count() == 0 or not already_have.is_visible():
                console.print("[cyan]Page defaults to Sign In. Clicking 'Sign up' link...[/cyan]")
                signup_toggle.click()
                human_delay(2.0, 3.5)
    except Exception as e:
        console.print(f"[dim]Toggle to signup failed: {e}[/dim]")

    # If there is a Name field (Open WebUI Sign Up form requires it), fill it!
    try:
        name_loc = page.locator('input[placeholder*="name" i], input[type="text"]').first
        if name_loc.count() > 0 and name_loc.is_visible():
            name_val = email.split('@')[0]
            name_loc.fill(name_val)
            human_delay(0.6, 1.4)
    except Exception as e:
        console.print(f"[dim]Name field fill skipped: {e}[/dim]")

    # 2. Fill email.
    if not selectors.fill_email(page, email, timeout=10000):
        console.print(
            "[yellow]Email field not auto-detected. "
            "Please fill it manually in the browser if needed.[/yellow]"
        )
        _wait_with_fallback(page, 12, "Waiting 12s for manual email entry...")
    human_delay(0.8, 1.8)

    # 3. Fill password fields if present (some flows are passwordless).
    pw_filled = selectors.fill_password(page, password, timeout=4000)
    if pw_filled:
        selectors.fill_password_confirm(page, password, timeout=3000)
        console.print("[dim]Password fields filled[/dim]")
    human_delay(1.0, 2.5)

    # 4. Submit.
    if not selectors.click_submit(page, timeout=6000):
        console.print(
            "[yellow]Submit button not detected. "
            "Click it manually in the browser if needed.[/yellow]"
        )
        _wait_with_fallback(page, 10, "Waiting 10s for manual submit...")


def create_zai_account(
    *,
    proxy: Optional[str] = None,
    visible: Optional[bool] = None,
) -> Optional[str]:
    """
    Fully automatic Z.ai account creation.

    Args:
        proxy: Optional proxy URL to bind to this account. When None, the
            default ProxyManager (env ``ZAI_PROXIES``) may assign one.
        visible: Override ``config.create_headless`` (visible = not headless).

    Returns:
        The created email address on success, None on failure.

    The browser stays visible by default for any manual slider / captcha
    solve. Failures of the active temp-mail provider automatically fall
    back to the next configured provider.
    """
    from playwright.sync_api import sync_playwright

    fingerprint = get_random_fingerprint()
    temp_mail = TempMailManager()  # config-driven timeout + provider order
    password = generate_password()

    headless = bool(config.get("create_headless", False)) if visible is None else (not visible)

    # Resolve a proxy: explicit arg > default manager.
    proxy_url = proxy
    if not proxy_url:
        try:
            pm = get_default_manager()
            if len(pm) > 0:
                proxy_url = pm.get_proxy(for_key="pending")
        except Exception:
            proxy_url = None

    email: Optional[str] = None

    try:
        email = temp_mail.create_account()

        with sync_playwright() as p:
            launch_args = list(config.get("create_browser_args", []) or [])
            browser = p.chromium.launch(headless=headless, args=launch_args)

            ctx_kwargs = _build_context_args(fingerprint)
            if proxy_url:
                parsed = parse_proxy_url(proxy_url)
                if parsed:
                    ctx_kwargs["proxy"] = parsed

            context = browser.new_context(**ctx_kwargs)
            Stealth().apply_stealth_sync(context)
            page = context.new_page()

            console.print("[cyan]Starting Z.ai automatic registration...[/cyan]")
            console.print(f"[cyan]Using temp email: {email}[/cyan]")
            if proxy_url:
                console.print(f"[dim]Using proxy: {proxy_url}[/dim]")

            # 1. Open auth page.
            page.goto("https://chat.z.ai/auth", wait_until="domcontentloaded", timeout=60000)
            human_delay(2.0, 4.0)

            # 2-4. Drive the form.
            _submit_signup_form(page, email, password)

            console.print("[cyan]Form submitted. Waiting for verification email...[/cyan]")

            # 5. Wait for the verification link, with provider fallback.
            verify_link = _wait_for_verification_with_fallback(
                page, temp_mail, email, password,
            )

            # 6. Navigate to the verification link if found.
            if verify_link:
                console.print("[cyan]Navigating to verification link...[/cyan]")
                try:
                    page.goto(verify_link, wait_until="domcontentloaded", timeout=30000)
                    human_delay(2.0, 5.0)
                except Exception as nav_err:
                    console.print(f"[yellow]Verification navigation issue: {nav_err}. Continuing...[/yellow]")

            # 7. Land on the chat.
            try:
                page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=30000)
                human_delay(2.0, 4.0)
            except Exception:
                pass

            # 8. Slider / captcha pause if needed.
            if _detect_slider_or_captcha(page):
                console.print("[bold yellow]!!! Security slider / captcha detected !!![/bold yellow]")
                console.print("[yellow]Solve it manually in the visible browser.[/yellow]")
                _wait_with_fallback(
                    page,
                    int(config.get("create_wait_slider_sec", 30)),
                    "Waiting for manual slider solve...",
                )

            # 9. Auto-detect login completion.
            console.print("[cyan]Waiting for chat UI to appear (auto-detect login)...[/cyan]")
            logged = _auto_wait_for_login(page)
            if not logged:
                console.print(
                    "[yellow]Login not auto-detected. Complete any remaining steps "
                    "(password, etc) in the browser.[/yellow]"
                )
                _wait_with_fallback(
                    page, 25,
                    "Waiting additional 25s then will attempt to save cookies anyway...",
                )

            # 10. Extract + persist.
            cookies = context.cookies()
            _persist_account(
                email=email,
                cookies=cookies,
                fingerprint=fingerprint,
                password=password,
                proxy=proxy_url,
            )

            console.print(f"[bold green][OK] Account creation complete for {email}[/bold green]")
            browser.close()
            return email

    except Exception as exc:
        log_exception(log, exc, "create_zai_account")
        console.print(f"[red]Account creation failed: {exc}[/red]")
        if email:
            console.print("[yellow]Partial failure. You may retry or clean up manually.[/yellow]")
        return None
    finally:
        try:
            temp_mail.cleanup()
        except Exception:
            pass


def _wait_for_verification_with_fallback(
    page: Page,
    temp_mail: TempMailManager,
    initial_email: str,
    password: str,
) -> Optional[str]:
    """
    Wait for a verification link, switching providers on timeout.

    When a fallback provider produces a NEW mailbox address, we re-drive the
    signup form with that address (the original address is now useless).
    Returns the verification link or None.
    """
    max_provider_rounds = max(1, len(temp_mail.provider_order))
    for round_idx in range(max_provider_rounds):
        try:
            link = temp_mail.wait_for_verification_link()
            if link:
                return link
        except NewMailboxAfterFallback as fb:
            # A new mailbox was created with a different provider - we need
            # to re-submit the signup form using the new address.
            new_email = fb.new_address
            console.print(f"[cyan]Re-submitting signup with new mailbox: {new_email}[/cyan]")
            try:
                page.goto("https://chat.z.ai/auth", wait_until="domcontentloaded", timeout=60000)
                human_delay(1.5, 3.0)
                _submit_signup_form(page, new_email, password)
            except Exception as exc:
                log_exception(log, exc, "fallback_resubmit")
            continue
        except (PlaywrightTimeout, Exception) as exc:
            console.print(f"[red]{exc}[/red]")
            console.print(
                "[yellow]No verification link auto-detected. If the email arrived, "
                "click the link in the browser now.[/yellow]"
            )
            _wait_with_fallback(page, 20, "Waiting 20s for manual verification...")
            return None
    return None


def _persist_account(
    email: str,
    cookies: List[Dict[str, Any]],
    fingerprint: Dict[str, Any],
    password: Optional[str],
    proxy: Optional[str],
) -> None:
    """Save the new account via the router's persistence layer."""
    try:
        from account_router import load_accounts, save_accounts
    except Exception:
        # Fall back to a local-only write if the router is unavailable.
        return _persist_local(email, cookies, fingerprint, password, proxy)

    data = load_accounts()
    new_account: Dict[str, Any] = {
        "email": email,
        "password": password,
        "created_at": datetime.now().isoformat(),
        "status": "active",
        "cookies": cookies,
        "fingerprint": fingerprint,
        "proxy": proxy,
        "last_used": None,
        "usage_count": 0,
        "request_count": 0,
        "approx_tokens": 0,
        "exhausted_at": None,
        "exhausted_count": 0,
        "cooldown_until": None,
        "last_failure_reason": None,
    }
    data.setdefault("accounts", []).append(new_account)
    data["last_updated"] = datetime.now().isoformat()
    save_accounts(data)
    log_event(email, "ACCOUNT_CREATED", f"proxy={'yes' if proxy else 'no'}")
    console.print(f"[bold green][OK] Account saved: {email}[/bold green]")


def _persist_local(
    email: str,
    cookies: List[Dict[str, Any]],
    fingerprint: Dict[str, Any],
    password: Optional[str],
    proxy: Optional[str],
) -> None:
    """Last-resort direct write when account_router cannot be imported."""
    accounts_file = config.accounts_file()
    accounts_file.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {"accounts": [], "last_updated": None}
    if accounts_file.exists():
        try:
            with open(accounts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"accounts": [], "last_updated": None}
    data.setdefault("accounts", []).append({
        "email": email,
        "password": password,
        "created_at": datetime.now().isoformat(),
        "status": "active",
        "cookies": cookies,
        "fingerprint": fingerprint,
        "proxy": proxy,
        "last_used": None,
        "usage_count": 0,
        "request_count": 0,
        "approx_tokens": 0,
        "exhausted_at": None,
        "exhausted_count": 0,
        "cooldown_until": None,
    })
    data["last_updated"] = datetime.now().isoformat()
    with open(accounts_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    console.print(f"[bold green][OK] Account saved (local fallback): {email}[/bold green]")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    console.print("[bold]=== Z.ai Automatic Account Creator ===[/bold]")
    console.print("Multi-provider temp mail + multi-strategy selectors + full automation.")
    console.print("Browser visible only for slider/captcha.\n")

    count = 1
    if len(sys.argv) > 1:
        try:
            count = max(1, int(sys.argv[1]))
        except ValueError:
            pass

    successes = 0
    for i in range(count):
        if count > 1:
            console.print(f"\n[bold cyan]=== Creating account {i + 1}/{count} ===[/bold cyan]")
        created = create_zai_account()
        if created:
            successes += 1
        if i < count - 1:
            time.sleep(3)

    console.print(f"\n[bold]Done. Created {successes}/{count} accounts.[/bold]")
    if successes:
        console.print("Run: python redteam.py list   or   python account_router.py --list")
