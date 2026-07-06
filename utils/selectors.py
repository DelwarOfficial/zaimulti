#!/usr/bin/env python3
"""
Multi-strategy element detection for Z.ai (chat.z.ai / GLM-5.2) automation.

The Z.ai UI is text-heavy and changes frequently, so single-strategy
selectors (``button:has-text("Continue")``) break constantly. This module
exposes a small framework that resolves an element through several
independent strategies, in priority order:

  1. **data-testid / aria**      - stable hooks, preferred when present.
  2. **role + accessible name**   - resilient to text tweaks.
  3. **text content**             - flexible substring / regex matching.
  4. **CSS attribute hints**      - type, name, placeholder, autocomplete.
  5. **JS fallback**              - scan the DOM for elements whose text or
                                    attributes match a hint, returning a
                                    playwrigh-resolvable descriptor.

Strategies are tried in order and short-circuit on the first hit that is
visible and enabled. Every helper returns a Playwright ``Locator`` or
``None`` (never raises), so callers can compose with ``or``.

The same strategy library is shared between account creation
(``create_account.py``) and the red-team driver (``redteam/driver.py``) so a
UI fix in one place benefits both.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence

from rich.console import Console

console = Console()


# ------------------------------------------------------------
# Locator hints
#
# Each hint is a list of CSS / Playwright selector fragments tried in order.
# Keep them small and specific; broad selectors (``textarea``) intentionally
# come last as a fallback.
# ------------------------------------------------------------

EMAIL_BUTTON_HINTS: List[str] = [
    '[data-testid*="email" i]',
    '[data-testid*="continue-with-email" i]',
    'button[aria-label*="email" i]',
    'a[aria-label*="email" i]',
    '[role="button"][aria-label*="email" i]',
    'button:has-text("Continue with Email")',
    'button:has-text("Sign in with Email")',
    'button:has-text("Email")',
    'a:has-text("Continue with Email")',
    'a:has-text("Email")',
    'text="Continue with Email"',
    'text="Sign in with Email"',
]

EMAIL_INPUT_HINTS: List[str] = [
    'input[type="email"]',
    'input[autocomplete="email"]',
    'input[name*="email" i]',
    'input[id*="email" i]',
    'input[placeholder*="email" i]',
    'input[placeholder*="mail" i]',
]

PASSWORD_INPUT_HINTS: List[str] = [
    'input[type="password"]',
    'input[autocomplete="new-password"]',
    'input[autocomplete="current-password"]',
    'input[name*="password" i]',
    'input[placeholder*="password" i]',
]

PASSWORD_CONFIRM_HINTS: List[str] = [
    'input[name*="confirm" i]',
    'input[placeholder*="confirm" i]',
    'input[autocomplete="new-password"]:nth-of-type(2)',
]

SUBMIT_BUTTON_HINTS: List[str] = [
    '[data-testid*="submit" i]',
    '[data-testid*="continue" i]',
    '[data-testid*="signup" i]',
    '[data-testid*="sign-up" i]',
    'button[type="submit"]',
    'button[aria-label*="continue" i]',
    'button[aria-label*="sign" i]',
    'button:has-text("Continue")',
    'button:has-text("Sign up")',
    'button:has-text("Create")',
    'button:has-text("Register")',
    'button:has-text("Next")',
    'role=button[name*="continue" i]',
    'role=button[name*="sign" i]',
]

CHAT_INPUT_HINTS: List[str] = [
    'textarea[placeholder*="message" i]',
    'textarea[placeholder*="ask" i]',
    'textarea[placeholder*="chat" i]',
    'textarea[data-testid*="prompt" i]',
    'div[contenteditable="true"][role="textbox"]',
    '[role="textbox"][contenteditable="true"]',
    'textarea',
    'div[contenteditable="true"]',
    '[role="textbox"]',
    'input[type="text"]',
]

SEND_BUTTON_HINTS: List[str] = [
    'button[data-testid*="send" i]',
    'button[aria-label*="send" i]',
    'button[type="submit"]:has(svg)',
    'button:has-text("Send")',
    '[role="button"][aria-label*="send" i]',
    'form button[type="submit"]',
]

ASSISTANT_RESPONSE_HINTS: List[str] = [
    '[data-message-author="assistant"]',
    '[data-role="assistant"]',
    '[data-testid*="assistant" i]',
    '.message.assistant',
    'div[role="article"]:last-of-type',
    'article:last-of-type',
    '.chat-message.assistant',
    'div.prose',
    'div[class*="response"]',
    'div[class*="assistant"]',
]

USER_MESSAGE_HINTS: List[str] = [
    '[data-message-author="user"]',
    '[data-role="user"]',
    '.message.user',
    'div[class*="user-message"]',
]


# ------------------------------------------------------------
# Low-level resolver
# ------------------------------------------------------------

def find_first_visible(
    page: Any,
    selectors: Sequence[str],
    *,
    timeout: int = 6000,
    must_be_enabled: bool = True,
) -> Optional[Any]:
    """
    Try each selector in order; return the first visible (and enabled) Locator.

    Visibility check uses Playwright's ``wait_for(state="visible")`` with a
    short per-strategy timeout. Any Playwright error is swallowed so the next
    strategy is attempted.
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            if must_be_enabled:
                try:
                    if loc.is_disabled() is True:
                        continue
                except Exception:
                    pass
            return loc
        except Exception:
            continue
    return None


def click_first(
    page: Any,
    selectors: Sequence[str],
    *,
    timeout: int = 6000,
    post_click_sleep: float = 0.0,
) -> bool:
    """Resolve + click; return True on success, False otherwise."""
    loc = find_first_visible(page, selectors, timeout=timeout)
    if loc is None:
        return False
    try:
        loc.click()
    except Exception:
        return False
    if post_click_sleep:
        import time as _t
        _t.sleep(post_click_sleep)
    return True


def fill_first(
    page: Any,
    selectors: Sequence[str],
    value: str,
    *,
    timeout: int = 8000,
) -> bool:
    """Resolve + fill; return True on success."""
    loc = find_first_visible(page, selectors, timeout=timeout)
    if loc is None:
        return False
    try:
        loc.fill(value)
        return True
    except Exception:
        # Some custom inputs reject .fill() (e.g. contenteditable). Try click+type.
        try:
            loc.click()
            loc.type(value)
            return True
        except Exception:
            return False


# ------------------------------------------------------------
# JavaScript fallback
# ------------------------------------------------------------

def _js_click_hint(page: Any, hints: Sequence[str], text_only: bool = False) -> bool:
    """
    Last-resort click via DOM scan.

    Iterates over buttons / links / role=button elements and clicks the first
    whose innerText OR any attribute (data-testid, aria-label, name, id)
    contains one of the provided case-insensitive hints.

    Returns True if anything was clicked.
    """
    if not hints:
        return False
    js = r"""
    (hints, textOnly) => {
        const lowerHints = hints.map(h => h.toLowerCase());
        const nodes = Array.from(document.querySelectorAll(
            'button, a, [role="button"], input[type="submit"]'
        ));
        for (const el of nodes) {
            const txt = (el.innerText || el.textContent || '').toLowerCase();
            const attrs = textOnly ? [] : [
                (el.getAttribute('data-testid') || ''),
                (el.getAttribute('aria-label') || ''),
                (el.getAttribute('name') || ''),
                (el.getAttribute('id') || ''),
                (el.getAttribute('placeholder') || ''),
            ].map(s => (s || '').toLowerCase());
            const hay = [txt, ...attrs];
            if (lowerHints.some(h => hay.some(s => s.includes(h)))) {
                try { el.click(); return true; } catch (e) {}
            }
        }
        return false;
    }
    """
    try:
        return bool(page.evaluate(js, [list(hints), text_only]))
    except Exception:
        return False


def _js_find_input_and_fill(
    page: Any,
    hints: Sequence[str],
    value: str,
    by_type: Optional[str] = None,
) -> bool:
    """
    Last-resort input fill via DOM scan.

    ``by_type`` lets us restrict to e.g. ``email`` / ``password`` inputs when
    the attribute hints are ambiguous.
    """
    js = r"""
    (args) => {
        const hints = args.hints.map(h => h.toLowerCase());
        const byType = args.by_type;
        const inputs = Array.from(document.querySelectorAll('input, textarea'));
        for (const el of inputs) {
            if (byType && (el.getAttribute('type') || '').toLowerCase() !== byType) continue;
            const attrs = [
                el.getAttribute('name') || '',
                el.getAttribute('id') || '',
                el.getAttribute('placeholder') || '',
                el.getAttribute('autocomplete') || '',
            ].map(s => s.toLowerCase());
            if (hints.some(h => attrs.some(s => s.includes(h)))) {
                try {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set || Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    )?.set;
                    setter ? setter.call(el, args.value) : (el.value = args.value);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                } catch (e) {}
            }
        }
        return false;
    }
    """
    try:
        return bool(page.evaluate(js, {
            "hints": list(hints),
            "value": value,
            "by_type": by_type,
        }))
    except Exception:
        return False


# ------------------------------------------------------------
# High-level semantic helpers
# ------------------------------------------------------------

def click_email_option(page: Any, timeout: int = 6000) -> bool:
    """Click the 'Continue with Email' / equivalent entry point."""
    if click_first(page, EMAIL_BUTTON_HINTS, timeout=timeout):
        return True
    if _js_click_hint(page, ["email", "continue with email", "sign in with email"]):
        return True
    return False


def fill_email(page: Any, email: str, timeout: int = 8000) -> bool:
    if fill_first(page, EMAIL_INPUT_HINTS, email, timeout=timeout):
        return True
    if _js_find_input_and_fill(page, ["email", "mail"], email, by_type="email"):
        return True
    # Last resort: first visible text-like input of any kind.
    return fill_first(page, ['input[type="email"]', 'input:not([type])'], email, timeout=timeout)


def fill_password(page: Any, password: str, timeout: int = 8000) -> bool:
    if fill_first(page, PASSWORD_INPUT_HINTS, password, timeout=timeout):
        return True
    if _js_find_input_and_fill(page, ["password", "pwd"], password, by_type="password"):
        return True
    return False


def fill_password_confirm(page: Any, password: str, timeout: int = 5000) -> bool:
    return fill_first(page, PASSWORD_CONFIRM_HINTS, password, timeout=timeout)


def click_submit(page: Any, timeout: int = 6000) -> bool:
    if click_first(page, SUBMIT_BUTTON_HINTS, timeout=timeout):
        return True
    if _js_click_hint(page, ["continue", "sign up", "signup", "create", "register", "next"]):
        return True
    return False


def find_chat_input(page: Any, timeout: int = 8000) -> Optional[Any]:
    return find_first_visible(page, CHAT_INPUT_HINTS, timeout=timeout)


def find_send_button(page: Any, timeout: int = 3000) -> Optional[Any]:
    return find_first_visible(page, SEND_BUTTON_HINTS, timeout=timeout, must_be_enabled=False)


def get_assistant_messages(page: Any) -> List[Any]:
    """Return all visible assistant message Locators, best-effort."""
    out: List[Any] = []
    seen_handles = set()
    for sel in ASSISTANT_RESPONSE_HINTS:
        try:
            locs = page.locator(sel).all()
        except Exception:
            continue
        for loc in locs:
            try:
                # De-duplicate by underlying DOM node via evaluate.
                key = page.evaluate(
                    "(el) => el.outerHTML.slice(0, 200)",
                    loc.element_handle(timeout=1000) if hasattr(loc, "element_handle") else loc
                ) if False else id(loc)
            except Exception:
                key = id(loc)
            if key in seen_handles:
                continue
            seen_handles.add(key)
            try:
                if loc.is_visible():
                    out.append(loc)
            except Exception:
                out.append(loc)
    return out


def get_last_assistant_text(page: Any, max_chars: int = 8000) -> str:
    """Return inner_text of the most recent assistant message, or ''."""
    msgs = get_assistant_messages(page)
    for loc in reversed(msgs):
        try:
            txt = loc.inner_text(timeout=2000) or ""
        except Exception:
            continue
        txt = txt.strip()
        if txt:
            return txt[:max_chars]
    return ""


__all__ = [
    # Hint banks
    "EMAIL_BUTTON_HINTS", "EMAIL_INPUT_HINTS", "PASSWORD_INPUT_HINTS",
    "PASSWORD_CONFIRM_HINTS", "SUBMIT_BUTTON_HINTS",
    "CHAT_INPUT_HINTS", "SEND_BUTTON_HINTS",
    "ASSISTANT_RESPONSE_HINTS", "USER_MESSAGE_HINTS",
    # Low-level
    "find_first_visible", "click_first", "fill_first",
    "_js_click_hint", "_js_find_input_and_fill",
    # High-level
    "click_email_option", "fill_email", "fill_password",
    "fill_password_confirm", "click_submit",
    "find_chat_input", "find_send_button",
    "get_assistant_messages", "get_last_assistant_text",
]
