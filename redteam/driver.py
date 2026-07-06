"""
Chat Driver for red team automation on Z.ai chat.

Uses the multi-account router to obtain sessions (cookies + stealth) and
automates:
- Loading an authenticated context.
- Sending prompts to the chat (multi-strategy selector resolution).
- Reliably capturing full responses (handles streaming + non-streaming).
- Inline detection of limits / errors via ``account_router.detect_exhaustion``.
- Saving per-interaction transcripts.

Streaming capture strategy
--------------------------
The previous implementation grabbed "the last large text block" repeatedly
which was fragile. The new approach:

1. Wait for the first assistant message whose length clears the configured
   ``response_capture_min_len`` threshold.
2. Track that message's text across polls; when it stabilises for N
   consecutive polls *and* the visible "stop generating" / "regenerate"
   UI affordances disappear, we treat the message as complete.
3. Also detect explicit completion signals (a "regenerate" button, no
   "typing" / "thinking" indicator).
4. Hard cap at ``response_capture_max_wait_sec``.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console

import sys
import config
from utils import selectors
from utils.logging_config import get_logger, log_exception

# Allow running both as a package (``python -m redteam.driver``) and as a
# loose script (``python redteam/driver.py``).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from account_router import (
    create_playwright_context_with_cookies,
    detect_exhaustion,
    get_next_session,
    mark_exhausted,
    mark_invalid,
    report_request,
    report_response,
)

log = get_logger("zai.driver")
console = Console()

TRANSCRIPTS_DIR = Path(config.transcripts_dir() if hasattr(config, "transcripts_dir") else "redteam/transcripts")
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

# Streaming UI affordances - presence indicates generation in progress.
GENERATING_INDICATORS = [
    '[data-testid*="stop" i]',
    'button[aria-label*="stop" i]',
    'button:has-text("Stop")',
    '[class*="typing" i]',
    '[class*="generating" i]',
    '[class*="loading" i][role="status"]',
]
COMPLETE_INDICATORS = [
    'button:has-text("Regenerate")',
    'button[aria-label*="regenerate" i]',
    '[data-testid*="regenerate" i]',
]


# ------------------------------------------------------------
# Prompt submission (uses selectors module)
# ------------------------------------------------------------

def _find_first_visible(page: Any, selectors_list: List[str], timeout: int = 8000) -> Optional[Any]:
    """Backward-compat shim - delegates to the shared selector resolver."""
    return selectors.find_first_visible(page, selectors_list, timeout=timeout)


def _send_prompt(page: Any, prompt_text: str) -> bool:
    """Type the prompt into the chat input and submit it."""
    input_loc = selectors.find_chat_input(
        page, timeout=int(config.get("chat_input_timeout_sec", 10)) * 1000
    )
    if not input_loc:
        console.print("[red]Could not find chat input field[/red]")
        return False

    try:
        input_loc.click()
        time.sleep(0.3)
        try:
            input_loc.fill("")
        except Exception:
            pass
        # Type in chunks for human-like cadence.
        for chunk in [prompt_text[i:i + 60] for i in range(0, len(prompt_text), 60)]:
            try:
                input_loc.type(chunk, delay=random.randint(15, 45))
            except Exception:
                # Fall back to fill if type() is rejected by the widget.
                input_loc.fill(prompt_text)
                break
            time.sleep(random.uniform(0.05, 0.2))
        time.sleep(0.4)

        sent = False
        send_btn = selectors.find_send_button(page, timeout=3000)
        if send_btn is not None:
            try:
                send_btn.click()
                sent = True
            except Exception:
                sent = False
        if not sent:
            try:
                input_loc.press("Enter")
                sent = True
            except Exception:
                sent = False
        return sent
    except Exception as exc:
        log_exception(log, exc, "_send_prompt")
        console.print(f"[yellow]Send error: {exc}[/yellow]")
        return False


# ------------------------------------------------------------
# Response capture (rewritten for streaming reliability)
# ------------------------------------------------------------

def _is_generating(page: Any) -> bool:
    """True if the UI shows an active generation affordance."""
    for sel in GENERATING_INDICATORS:
        try:
            if page.locator(sel).count() > 0:
                # Confirm visible - count() can match hidden nodes.
                if page.locator(sel).first.is_visible():
                    return True
        except Exception:
            continue
    return False


def _is_complete_signal(page: Any) -> bool:
    """True if the UI shows a 'regenerate' / completion affordance."""
    for sel in COMPLETE_INDICATORS:
        try:
            if page.locator(sel).count() > 0:
                if page.locator(sel).first.is_visible():
                    return True
        except Exception:
            continue
    return False


def _latest_assistant_text(page: Any) -> str:
    """Return inner_text of the latest visible assistant message (or '')."""
    try:
        msgs = selectors.get_assistant_messages(page)
    except Exception:
        msgs = []
    for loc in reversed(msgs):
        try:
            txt = loc.inner_text(timeout=2000) or ""
        except Exception:
            continue
        txt = txt.strip()
        if txt:
            return txt
    # Fallback: scrape the largest visible text block via DOM scan.
    try:
        return page.evaluate(
            r"""() => {
                const blocks = Array.from(document.querySelectorAll(
                    'div, article, section, p'
                )).map(el => (el.innerText || '').trim())
                    .filter(t => t && t.length > 30);
                blocks.sort((a, b) => b.length - a.length);
                return blocks[0] || '';
            }"""
        ) or ""
    except Exception:
        return ""


def _capture_response(page: Any, max_wait_seconds: int = 0) -> str:
    """
    Reliably capture an assistant response, streaming-aware.

    Algorithm:
      1. Wait for any assistant text to appear (initial appearance window).
      2. Poll the latest assistant message; track stability.
      3. Treat as complete when EITHER:
           - the message has been stable for ``response_capture_stable_polls``
             consecutive polls AND no generation indicator is visible, OR
           - a completion ('regenerate') affordance is visible AND no
             generation indicator is visible.
      4. Hard cap: ``max_wait_seconds``.

    Returns the captured text (stripped). Empty string on failure.
    """
    if max_wait_seconds <= 0:
        max_wait_seconds = int(config.get("response_capture_max_wait_sec", 120))
    stable_needed = int(config.get("response_capture_stable_polls", 3))
    min_len = int(config.get("response_capture_min_len", 20))
    interval = float(config.get("response_stable_interval_sec", 2.2))

    deadline = time.time() + max_wait_seconds

    # Phase 1: initial appearance (give UI up to ~25s to render first chunk).
    appearance_deadline = time.time() + min(max_wait_seconds, 25)
    text = ""
    while time.time() < appearance_deadline:
        text = _latest_assistant_text(page)
        if text:
            break
        time.sleep(1.0)

    if not text:
        # No assistant text at all - check for inline error / rate signals.
        try:
            body = page.content() or ""
        except Exception:
            body = ""
        if body:
            return body[:4000]
        return ""

    # Phase 2: stream until stable + complete.
    previous = text
    stable_count = 0
    last_text = text

    while time.time() < deadline:
        time.sleep(interval)
        try:
            current = _latest_assistant_text(page)
        except Exception:
            current = previous

        if current and current != previous:
            previous = current
            last_text = current
            stable_count = 0
            continue

        # Text unchanged this round.
        if current:
            stable_count += 1
        generating = _is_generating(page)
        complete_signal = _is_complete_signal(page)

        # Done if stable enough and not still generating.
        if (not generating) and (
            (stable_count >= stable_needed) or complete_signal
        ) and len(last_text) >= min(min_len, len(last_text)):
            break

    return last_text.strip()


# ------------------------------------------------------------
# Single interaction + batch
# ------------------------------------------------------------

def run_single_interaction(
    prompt: str,
    account: Optional[Dict[str, Any]] = None,
    headless: bool = True,
    timeout_response: int = 0,
) -> Dict[str, Any]:
    """
    Run one prompt against a fresh or provided account session.
    Returns a transcript dict with full metadata + response + analysis hints.
    """
    if not account:
        account = get_next_session(deep_check=True)
        if not account:
            raise RuntimeError("No healthy account available from router")

    email = account["email"]
    console.print(f"[cyan]Using account: {email}[/cyan]")

    transcript: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "account_email": email,
        "prompt": prompt,
        "response": "",
        "success": False,
        "error": None,
        "duration_sec": 0,
        "cookies_used": len(account.get("cookies", [])),
    }

    start = time.time()
    p = browser = ctx = page = None

    try:
        p, browser, ctx, page = create_playwright_context_with_cookies(
            account.get("cookies", []),
            proxy=account.get("proxy"),
            headless=headless,
        )

        page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(random.uniform(1.5, 3.5))

        url = (page.url or "").lower()
        if "auth" in url or "login" in url:
            mark_invalid(email, reason="redirected to login during interaction")
            raise RuntimeError("Session expired / redirected to login")

        if not _send_prompt(page, prompt):
            raise RuntimeError("Failed to submit prompt")

        cap_timeout = timeout_response or int(config.get("response_capture_max_wait_sec", 120))
        response = _capture_response(page, max_wait_seconds=cap_timeout)
        transcript["response"] = response
        transcript["success"] = bool(response and len(response) > 10)

        # Inline state sync via the router.
        signals = report_response(email, response, prompt)
        transcript["exhaustion_signals"] = signals
        if signals["is_hard_exhausted"] or signals["is_rate_limited"]:
            transcript["error"] = "rate_limit_detected_in_response"
        elif signals["is_invalid"]:
            transcript["error"] = "invalid_signals_in_response"

    except Exception as exc:
        transcript["error"] = str(exc)
        log_exception(log, exc, f"run_single_interaction:{email}")
        console.print(f"[red]Interaction failed for {email}: {exc}[/red]")
        # Conservative classification from the exception text.
        try:
            low = str(exc).lower()
            if any(k in low for k in config.invalid_session_keywords()):
                mark_invalid(email, reason=str(exc))
            elif any(k in low for k in config.rate_limit_keywords()):
                mark_exhausted(email, reason=str(exc))
        except Exception:
            pass
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

    transcript["duration_sec"] = round(time.time() - start, 1)

    # Persist transcript.
    safe_email = email.replace("@", "_").replace(".", "_")
    fname = TRANSCRIPTS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_email[:12]}.json"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(transcript, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        log_exception(log, exc, "transcript_save")

    return transcript


def run_prompt_batch(
    prompts: List[Dict[str, Any]],
    max_accounts: int = 5,
    headless: bool = True,
    per_prompt_timeout: int = 0,
) -> List[Dict[str, Any]]:
    """
    Run a list of prompt dicts (from ``redteam.prompts``) across the pool.
    Rotates accounts automatically via the router.
    """
    results: List[Dict[str, Any]] = []
    for i, p in enumerate(prompts):
        console.print(f"\n[bold]=== Prompt {i + 1}/{len(prompts)} [{p.get('category')}] ===[/bold]")
        snippet = (p.get("prompt") or "")[:120]
        console.print(f"Prompt: {snippet}...")

        try:
            res = run_single_interaction(
                prompt=p["prompt"],
                account=None,  # forces router selection
                headless=headless,
                timeout_response=per_prompt_timeout,
            )
            res["prompt_id"] = p.get("id")
            res["prompt_category"] = p.get("category")
            res["prompt_notes"] = p.get("notes")
            results.append(res)

            if res.get("error") and "rate" in str(res.get("error", "")).lower():
                console.print("[yellow]Rate limit hit. Will rotate on next.[/yellow]")
        except Exception as exc:
            log_exception(log, exc, f"batch_item:{p.get('id')}")
            console.print(f"[red]Batch item failed: {exc}[/red]")
            results.append({
                "prompt_id": p.get("id"),
                "prompt": p["prompt"],
                "error": str(exc),
                "success": False,
            })

        time.sleep(random.uniform(1.0, 3.5))  # light pacing between prompts

    return results


if __name__ == "__main__":
    print("RedTeam Driver ready. Use via campaign.py or main CLI.")
