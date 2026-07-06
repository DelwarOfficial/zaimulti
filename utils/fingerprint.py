"""
Fingerprint generation utilities.
Provides realistic randomized browser fingerprints for evasion.
"""

import random
from typing import Dict, Any


# Base realistic pools (expand as needed)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

LOCALES = ["en-US", "en-GB", "en-CA", "zh-CN", "de-DE"]
TIMEZONES = [
    "America/New_York", "Europe/London", "Asia/Shanghai", "Europe/Berlin",
    "America/Los_Angeles", "Asia/Singapore"
]
VIEWPORT_SIZES = [
    {"width": 1280, "height": 720},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]


def get_random_fingerprint() -> Dict[str, Any]:
    """
    Generate realistic randomized fingerprint parameters for Playwright context.

    Returns:
        Dict with user_agent, viewport, locale, timezone_id, device_scale_factor
    """
    ua = random.choice(USER_AGENTS)
    viewport = random.choice(VIEWPORT_SIZES).copy()
    locale = random.choice(LOCALES)
    timezone = random.choice(TIMEZONES)
    device_scale = round(random.uniform(1.0, 1.25), 2)

    # Small jitter on viewport for realism
    viewport["width"] += random.randint(-20, 20)
    viewport["height"] += random.randint(-20, 20)

    return {
        "user_agent": ua,
        "viewport": viewport,
        "locale": locale,
        "timezone_id": timezone,
        "device_scale_factor": device_scale,
    }
