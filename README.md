# Z.ai Multi-Account Manager

Professional, production-ready system for managing multiple Z.ai (chat.z.ai / GLM-5.2) accounts.  
Automatically rotates accounts when free tokens or rate limits are hit inside GLM / ZCode coding agents.

## Features

- **Fully automatic account creation** using mail.tm temp mail (no manual email entry)
- Automatic verification link detection and navigation
- Visible browser only for security sliders / human challenges (kept minimal)
- **Smart rotation engine**: least-usage selection, cooldowns, status tracking
- Statuses: `active`, `exhausted`, `invalid`, `cooling_down`
- Rate / token limit detection during health probes
- Rich usage + request counting per account
- **Clean importable API** designed for GLM/ZCode agents
- Proxy support skeleton (easy to plug residential proxies)
- Strong fingerprinting via playwright-stealth
- Professional logging with `rich`
- Ready for Patchright upgrade path

## Project Structure

```
z_ai_multi_account/
├── create_account.py          # Fully automatic account creator
├── account_router.py          # Core router + GLM agent API
├── requirements.txt
├── README.md
├── accounts/
│   └── accounts.json          # All account metadata + cookies
├── logs/
│   └── rotation_log.txt
└── utils/
    ├── __init__.py
    ├── fingerprint.py         # Randomized realistic fingerprints
    ├── temp_mail.py           # mail.tm integration
    └── proxy.py               # ProxyManager skeleton
```

## Quick Setup

```powershell
# 1. Virtual environment (Windows example)
python -m venv venv
.\venv\Scripts\activate

# 2. Install
pip install -r requirements.txt
playwright install chromium

# 3. (Optional but recommended) playwright-stealth
pip install playwright-stealth
```

## Usage

### Create Accounts (Fully Automatic)

```bash
python create_account.py
```

- Uses temporary email automatically
- Detects verification email
- Navigates verification link
- Pauses **only** if a slider appears (solve it, press Enter)
- Saves rich metadata + cookies

Run multiple times to build a healthy pool (5-10 accounts recommended).

### CLI (account_router.py)

```bash
python account_router.py --help
python account_router.py --list
python account_router.py --get-session
python account_router.py --validate-all
python account_router.py --probe user@tempmail.com
```

### GLM / ZCode Coding Agent Integration (Recommended)

Import directly:

```python
from account_router import (
    get_next_session,
    mark_exhausted,
    rotate_account,
    report_request,
    mark_invalid,
)

# Get a ready session
session = get_next_session()
if not session:
    raise RuntimeError("No healthy accounts available")

email = session["email"]
cookies = session["cookies"]   # ready for context.add_cookies(cookies)

# ... use in your agent's Playwright / browser context ...

# When the agent sees "rate limit", "token limit", "quota exceeded" etc:
mark_exhausted(email)

# Optional: report usage
report_request(email, tokens_used=1200)

# Force next account
next_session = rotate_account("agent detected limit in chat")
```

**Example agent usage pattern** (inside your coding agent loop):

```python
session = get_next_session()
if session:
    # configure your browser/context with session["cookies"]
    try:
        result = do_long_code_generation(session["email"])
    except RateLimitError:
        mark_exhausted(session["email"])
        session = rotate_account()
```

### Helper: Ready Playwright Context

```python
from account_router import get_next_session, create_playwright_context_with_cookies

session = get_next_session()
p, browser, context, page = create_playwright_context_with_cookies(
    session["cookies"],
    proxy=session.get("proxy"),
    headless=True
)
# use page...
# don't forget browser.close(); p.stop()
```

## Proxy Support (Optional)

Set environment variable:

```powershell
$env:ZAI_PROXIES = "http://user:pass@host:port,http://another:port"
```

Or extend `utils/proxy.py`.

Proxies are accepted at context creation time and stored per account on creation.

## Account Lifecycle & Statuses

- `active` — usable
- `exhausted` — hit free limit / rate (auto cooldown 45 min)
- `invalid` — cookies dead, login fail, banned (short cooldown)
- `cooling_down` — temporarily skipped

The router automatically skips bad/cooldown accounts and picks the healthiest (lowest usage).

## Important Notes

- This system is for **personal / research** use against free tiers.
- Creating many accounts and heavy usage likely violates Z.ai ToS. Use responsibly.
- Temp mail accounts typically last 24-72 hours.
- Keep a small pool (8-15) and rotate often.
- For maximum undetectability later, migrate to **Patchright** (drop-in for most Playwright calls).

## Upgrade Path to Patchright

```bash
pip install patchright
# Then replace playwright imports + launch with patchright equivalent
# See original project notes + patchright docs
```

## Troubleshooting

- Creation stuck on email form → the selectors in create_account.py may need update (Z.ai UI changes). Use manual fallback when prompted.
- No verification email → increase `TempMailManager(timeout=...)` or check Z.ai sender.
- All accounts exhausted → run more `create_account.py`.
- Validation fails → use `--validate-all` or `mark_invalid(email)`.

## License & Disclaimer

Use at your own risk. Not affiliated with Z.ai or Zhipu.

---

## RED TEAM COMPLETE TOOLKIT (ZRT)

This repo has been extended into a **full red teaming framework** for Z.ai/GLM-5.2.

### Key Red Team Additions

- `redteam/prompts.py` — Categorized prompt library (jailbreaks, extraction, adversarial, behavioral probes)
- `redteam/analyzer.py` — Automatic response classification (refusal rate, leakage, success scoring)
- `redteam/driver.py` — Fully automated chat interaction using account cookies + Playwright
- `redteam/campaign.py` — Campaign runner + metrics + export
- `redteam.py` — Unified CLI entrypoint for everything

### Quick Red Team Workflow

```powershell
# 1. Build a pool (recommended 6-15 accounts)
python redteam.py bulk-create --count 5

# 2. Check status
python redteam.py list

# 3. Run a targeted campaign
python redteam.py run --category jailbreak --count 6

# 4. Full mixed evaluation
python redteam.py campaign --full --n 12 --headless

# Fast jailbreak test
python redteam.py quick-jailbreaks --n 5

# Revalidate everything
python redteam.py validate-all
```

### Advanced Usage (import)

```python
from redteam.campaign import RedTeamCampaign
from redteam.prompts import get_prompts_by_category

c = RedTeamCampaign(name="my_redteam_run", headless=True)
prompts = get_prompts_by_category("extraction") + get_prompts_by_category("jailbreak")
c.run(prompts)
c.print_summary()
c.export_results()
```

Results + transcripts saved under:
- `data/redteam_results.jsonl`
- `redteam/transcripts/`

### Metrics Tracked

- Refusal rate
- Info leakage rate
- Average bypass success score
- Per-category breakdown
- Per-account usage + cooldowns

### Notes for Red Teaming

- Always use proxies + strong fingerprinting for scale.
- Accounts from temp mail have short lifetimes.
- For maximum stealth, consider switching to **Patchright** later.
- Some flows will still require occasional manual slider solves.
- **ETHICAL / LEGAL USE ONLY**. This is a research / authorized security testing tool.

### CLI Reference

```
python redteam.py --help
python redteam.py bulk-create --count 4
python redteam.py run --category extraction --count 3 --headless --export
python redteam.py probe --email xxx@...
```

---

Production quality. Clean. Agent-first design. **Now with full red team capabilities.**
