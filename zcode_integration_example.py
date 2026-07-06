#!/usr/bin/env python3
"""
ZCode Multi-Account Integration Template
========================================

Use this script as a guide or utility inside your main ZCode automation.
It shows how to import the router, select healthy sessions, load cookies,
handle limits, and sync status back to the manager.
"""

import sys
from pathlib import Path
from typing import Dict, Any, List

# 1. Add this directory to sys.path so account_router can be imported from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from account_router import (
    get_next_session,
    mark_exhausted,
    mark_invalid,
    report_response,
    create_playwright_context_with_cookies
)

def run_zcode_task(prompt: str) -> str:
    """
    Example function running a prompt using the next healthy account.
    """
    # 2. Get a ready session (automatically rotates + validates)
    session = get_next_session(deep_check=True)
    if not session:
        raise RuntimeError("No healthy accounts available in pool. Please run 'python create_account.py'")

    email = session["email"]
    cookies = session["cookies"]
    proxy = session.get("proxy")

    print(f"[*] Selected account: {email}")

    # 3. Create context preloaded with cookies + stealth + proxy
    p, browser, context, page = create_playwright_context_with_cookies(
        cookies,
        proxy=proxy,
        headless=True
    )

    try:
        # Navigate and execute your ZCode actions
        page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=30000)
        
        # Example actions: fill prompt, click send, wait for response
        # (Using selectors from utils.selectors inside the actual driver)
        
        # Suppose we captured the response text from the UI
        response_text = "Here is the response from GLM..." 
        
        # 4. Sync response back to router to detect limits and update token usage
        signals = report_response(email, response_text, prompt_text=prompt)
        
        if signals["is_hard_exhausted"] or signals["is_rate_limited"]:
            print(f"[!] Account {email} hit rate/exhaustion limits. Rotated.")
        elif signals["is_invalid"]:
            print(f"[!] Account {email} session has expired or is invalid.")
            
        return response_text

    except Exception as e:
        # 5. Handle crashes or navigation failures. 
        # Check if the error looks like a session expiry or a rate limit.
        err_msg = str(e).lower()
        if "login" in err_msg or "auth" in err_msg or "unauthorized" in err_msg:
            mark_invalid(email, reason=f"Exception: {e}")
        elif "rate" in err_msg or "too many requests" in err_msg or "quota" in err_msg:
            mark_exhausted(email, reason=f"Exception: {e}")
        raise
    finally:
        browser.close()
        p.stop()

if __name__ == "__main__":
    print("[*] ZCode Integration helper loaded successfully.")
    # Example call (commented out by default):
    # run_zcode_task("Hello world")
