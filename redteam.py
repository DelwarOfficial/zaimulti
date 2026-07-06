#!/usr/bin/env python3
"""
ZRT - Z.ai Red Team Toolkit
Complete multi-account red teaming framework built on the account manager.

Commands:
  python redteam.py --help
  python redteam.py bulk-create --count 5
  python redteam.py list
  python redteam.py run --category jailbreak --count 5 --headless
  python redteam.py campaign --full --n 10
  python redteam.py probe --email foo@bar.com

Combines:
- Robust account farming (via create_account)
- Smart rotation (account_router)
- Prompt library + analyzer
- Automated chat driver
- Campaign reporting

For authorized red teaming, research, and security testing only.
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

# Local imports - lazy where possible for CLI usability
try:
    from account_router import (
        list_accounts, get_next_session, get_valid_session,
        mark_exhausted, mark_invalid, get_account_by_email, validate_session
    )
except Exception:
    list_accounts = lambda: print("Account router unavailable (install playwright + run setup)")
    # stubs for other commands

try:
    from create_account import create_zai_account
except Exception:
    create_zai_account = lambda: None

from redteam.prompts import get_prompts_by_category, sample_prompts
from redteam.campaign import RedTeamCampaign, quick_jailbreak_campaign
from redteam.analyzer import summarize_campaign

console = Console()

def cmd_list_accounts():
    console.print(Panel("Z.ai Account Pool", style="bold cyan"))
    list_accounts()

def cmd_create(count: int = 1, visible: bool = True):
    console.print(f"[bold]Creating {count} new account(s)...[/bold]")
    for i in range(count):
        console.print(f"\n--- Account {i+1}/{count} ---")
        email = create_zai_account()
        if email:
            console.print(f"[green]Created: {email}[/green]")
        else:
            console.print("[red]Failed to create account[/red]")

def cmd_get_session():
    sess = get_valid_session()
    if sess:
        console.print("[green]Session obtained[/green]")
        console.print(f"Email: {sess['email']}")
        console.print(f"Usage count: {sess.get('usage_count', 0)}")
    else:
        console.print("[red]No usable session[/red]")

def cmd_run_redteam(category: Optional[str], count: int, headless: bool, export: bool):
    from redteam.driver import run_single_interaction

    console.print(Panel.fit("Starting Red Team Run", style="bold red"))

    if category:
        prompts = get_prompts_by_category(category)
    else:
        prompts = sample_prompts(count)

    if not prompts:
        console.print("[yellow]No prompts matched. Using sample.[/yellow]")
        prompts = sample_prompts(min(count, 8))

    prompts = prompts[:count]

    campaign = RedTeamCampaign(name=f"cli_{category or 'mixed'}", headless=headless)
    campaign.run(prompts)
    campaign.print_summary()

    if export:
        campaign.export_results()

def cmd_full_campaign(n: int, headless: bool):
    console.print("[bold magenta]Running full red team suite[/bold magenta]")
    c = RedTeamCampaign(name="full_suite", headless=headless)
    c.run_full_suite(n_total=n)
    c.print_summary()
    path = c.export_results()
    console.print(f"Full results saved: {path}")

def cmd_quick_jailbreaks(n: int, headless: bool):
    c = quick_jailbreak_campaign(n=n, headless=headless)
    c.export_results()

def cmd_probe(email: str):
    from account_router import get_account_by_email, validate_session
    acc = get_account_by_email(email)
    if not acc:
        console.print("[red]Account not found[/red]")
        return
    console.print(f"Probing {email}...")
    ok = validate_session(acc, deep_check=True)
    console.print(f"Healthy: {ok}")

def cmd_bulk_validate():
    from account_router import load_accounts, save_accounts, validate_session
    from datetime import datetime, timedelta
    data = load_accounts()
    updated = 0
    for acc in data.get("accounts", []):
        if acc.get("status") in ("active", None):
            console.print(f"Validating {acc['email']}...")
            if not validate_session(acc, deep_check=True):
                acc["status"] = "exhausted"
                acc["cooldown_until"] = (datetime.now() + timedelta(minutes=45)).isoformat()
                updated += 1
    save_accounts(data)
    console.print(f"[green]Re-validated pool. Marked {updated} as exhausted/invalid.[/green]")

def main():
    import sys
    if sys.platform.startswith("win"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    parser = argparse.ArgumentParser(
        description="ZRT — Z.ai Red Team Complete Toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python redteam.py list
  python redteam.py bulk-create --count 3
  python redteam.py run --category jailbreak --count 4
  python redteam.py campaign --full --n 8 --headless
  python redteam.py quick-jailbreaks --n 6
        """
    )
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", help="List accounts + status")
    p_list.set_defaults(func=lambda a: cmd_list_accounts())

    # bulk-create
    p_create = sub.add_parser("bulk-create", help="Create multiple accounts automatically")
    p_create.add_argument("--count", type=int, default=1)
    p_create.add_argument("--visible", action="store_true", help="Keep browser visible (default for challenges)")
    p_create.set_defaults(func=lambda a: cmd_create(a.count, a.visible))

    # get-session
    p_sess = sub.add_parser("get-session", help="Get next healthy session")
    p_sess.set_defaults(func=lambda a: cmd_get_session())

    # run
    p_run = sub.add_parser("run", help="Execute red team prompts")
    p_run.add_argument("--category", choices=["jailbreak", "extraction", "adversarial", "behavioral", "harmful"], default=None)
    p_run.add_argument("--count", type=int, default=4)
    p_run.add_argument("--headless", action="store_true", help="Run browser headless (may miss challenges)")
    p_run.add_argument("--export", action="store_true")
    p_run.set_defaults(func=lambda a: cmd_run_redteam(a.category, a.count, a.headless, a.export))

    # campaign
    p_camp = sub.add_parser("campaign", help="Run structured campaigns")
    p_camp.add_argument("--full", action="store_true", help="Run full mixed suite")
    p_camp.add_argument("--n", type=int, default=8)
    p_camp.add_argument("--headless", action="store_true")
    p_camp.set_defaults(func=lambda a: cmd_full_campaign(a.n, a.headless) if a.full else cmd_run_redteam(None, a.n, a.headless, True))

    # quick
    p_q = sub.add_parser("quick-jailbreaks", help="Fast jailbreak campaign")
    p_q.add_argument("--n", type=int, default=5)
    p_q.add_argument("--headless", action="store_true")
    p_q.set_defaults(func=lambda a: cmd_quick_jailbreaks(a.n, a.headless))

    # probe
    p_probe = sub.add_parser("probe", help="Deep probe one account")
    p_probe.add_argument("--email", required=True)
    p_probe.set_defaults(func=lambda a: cmd_probe(a.email))

    # validate-all
    p_val = sub.add_parser("validate-all", help="Revalidate entire pool")
    p_val.set_defaults(func=lambda a: cmd_bulk_validate())

    # accounts sub alias
    sub.add_parser("accounts", help="Alias for list").set_defaults(func=lambda a: cmd_list_accounts())

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        console.print("\n[bold cyan]ZRT Red Team Toolkit ready.[/bold cyan]")
        console.print("First: python redteam.py bulk-create --count 3")
        console.print("Then : python redteam.py run --category jailbreak")
        return

    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/yellow]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
