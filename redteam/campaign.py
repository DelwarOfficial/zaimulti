"""
Red Team Campaign Orchestrator

High-level API for running structured red team evaluations against Z.ai/GLM.

Usage examples:
    from redteam.campaign import RedTeamCampaign
    from redteam.prompts import get_prompts_by_category

    campaign = RedTeamCampaign(name="jailbreak_v1", headless=True)
    prompts = get_prompts_by_category("jailbreak")[:8]
    results = campaign.run(prompts)
    campaign.print_summary()
"""

import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any

from rich.console import Console
from rich.table import Table
from rich.progress import track

import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).parent.parent))

from redteam.prompts import get_all_prompts, sample_prompts
from redteam.analyzer import analyze_response, AnalysisResult, summarize_campaign

# Lazy heavy imports
def run_single_interaction(*a, **k):
    from redteam.driver import run_single_interaction as _f
    return _f(*a, **k)

def run_prompt_batch(*a, **k):
    from redteam.driver import run_prompt_batch as _f
    return _f(*a, **k)

console = Console()

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
RESULTS_FILE = DATA_DIR / "redteam_results.jsonl"


class RedTeamCampaign:
    def __init__(self, name: str = "default", headless: bool = True, save_transcripts: bool = True):
        self.name = name
        self.headless = headless
        self.results: List[Dict[str, Any]] = []
        self.analyses: List[AnalysisResult] = []
        self.started_at = datetime.now().isoformat()
        self.save_transcripts = save_transcripts

    def run(self, prompts: List[Dict], max_per_run: Optional[int] = None) -> List[Dict]:
        """Execute the campaign."""
        if max_per_run:
            prompts = prompts[:max_per_run]

        console.print(f"[bold green]Starting Red Team Campaign: {self.name}[/bold green]")
        console.print(f"Prompts to run: {len(prompts)} | Headless: {self.headless}")

        batch_results = run_prompt_batch(
            prompts=prompts,
            headless=self.headless
        )

        # Analyze each
        for br in batch_results:
            prompt_meta = {
                "id": br.get("prompt_id"),
                "category": br.get("prompt_category"),
                "prompt": br.get("prompt", br.get("prompt", ""))
            }
            analysis = analyze_response(prompt_meta, br.get("response", ""))
            br["analysis"] = {
                "success_score": analysis.success_score,
                "refusal": analysis.refusal,
                "leaked_info": analysis.leaked_info,
                "error_detected": analysis.error_detected,
                "notes": analysis.notes,
            }
            self.analyses.append(analysis)

        self.results.extend(batch_results)

        # Persist
        self._append_results(batch_results)

        console.print(f"[green]Campaign batch complete. {len(batch_results)} interactions.[/green]")
        return batch_results

    def run_categories(self, categories: List[str], n_per_cat: int = 4):
        """Convenience: sample and run from specific categories."""
        prompts = []
        for cat in categories:
            prompts.extend(sample_prompts(n_per_cat, categories=[cat]))
        return self.run(prompts)

    def run_full_suite(self, n_total: int = 12):
        prompts = sample_prompts(n_total)
        return self.run(prompts)

    def _append_results(self, new_results: List[Dict]):
        with open(RESULTS_FILE, "a", encoding="utf-8") as f:
            for r in new_results:
                f.write(json.dumps({
                    "campaign": self.name,
                    "timestamp": datetime.now().isoformat(),
                    **r
                }, ensure_ascii=False) + "\n")

    def print_summary(self):
        """Pretty summary using analyzer."""
        if not self.analyses:
            console.print("[yellow]No results yet.[/yellow]")
            return

        summary = summarize_campaign(self.analyses)

        table = Table(title=f"Red Team Campaign Summary — {self.name}")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        for k, v in summary.items():
            table.add_row(str(k), str(v))

        console.print(table)

        # Show a few high-score examples
        high = [a for a in self.analyses if a.success_score > 0.6]
        if high:
            console.print(f"\n[bold]High success examples ({len(high)}):[/bold]")
            for a in high[:3]:
                console.print(f"  • {a.category}: score={a.success_score} | {a.notes[:60]}")

    def export_results(self, path: Optional[Path] = None) -> Path:
        path = path or (DATA_DIR / f"campaign_{self.name}_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "campaign": self.name,
                "started": self.started_at,
                "finished": datetime.now().isoformat(),
                "results": self.results,
                "summary": summarize_campaign(self.analyses)
            }, f, indent=2, ensure_ascii=False)
        console.print(f"[green]Exported to {path}[/green]")
        return path

    def load_previous(self, path: Path):
        """Load prior run for continued analysis."""
        with open(path) as f:
            data = json.load(f)
        self.results = data.get("results", [])
        # Rebuild analyses if needed
        self.analyses = []
        for r in self.results:
            pmeta = {"category": r.get("prompt_category"), "prompt": r.get("prompt", "")}
            a = analyze_response(pmeta, r.get("response", ""))
            self.analyses.append(a)


def quick_jailbreak_campaign(n: int = 6, headless: bool = True) -> RedTeamCampaign:
    """One-liner helper."""
    from redteam.prompts import get_prompts_by_category
    c = RedTeamCampaign(name="quick_jailbreak", headless=headless)
    prompts = get_prompts_by_category("jailbreak")[:n]
    c.run(prompts)
    c.print_summary()
    return c


if __name__ == "__main__":
    # Demo run (will fail gracefully without accounts)
    c = RedTeamCampaign(name="demo")
    prompts = sample_prompts(2)
    try:
        c.run(prompts)
    except Exception as e:
        console.print(f"[red]Demo run failed (expected without accounts): {e}[/red]")
    c.print_summary()
