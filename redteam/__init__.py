"""
Z.ai Red Team Toolkit

Complete offensive testing framework on top of the multi-account manager.
"""

from .prompts import get_all_prompts, get_prompts_by_category, sample_prompts
from .analyzer import analyze_response, summarize_campaign, AnalysisResult

# Heavy imports (playwright etc) are performed inside the modules on demand
# to allow `python redteam.py --help` before full setup
def get_campaign_class():
    from .campaign import RedTeamCampaign
    return RedTeamCampaign

def quick_jailbreak_campaign(*a, **k):
    from .campaign import quick_jailbreak_campaign as _q
    return _q(*a, **k)

__version__ = "1.0.0-redteam"
