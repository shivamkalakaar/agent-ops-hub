"""The LinkedIn team: who exists, what each one does and its defaults. Synced to agent_registry on every run,
so the dashboard's /agents page always matches the code."""
from pathlib import Path

from core.settings import Specialist

AGENT = "market_intel"
AUTHOR = "Shivam"
PROMPTS = Path(__file__).parent / "prompts"
MAX_STEPS = 12  # specialist turns per run (one draft, one rewrite, each fully reviewed); coordinator decisions don't count

SPECIALISTS = [
    Specialist("supervisor", "Coordinator", "Reads every report and decides who works next, within hard limits.",
               "writer", max_calls=MAX_STEPS, required=True),
    Specialist("scout", "Scout", "Proposes three stories with a contrast and a take, tests each take (could a reader"
               " disagree, does it change a decision, is it more than a recap or a guess) and picks one.", "judge", max_calls=1, required=True),
    Specialist("researcher", "Researcher", "Reads the chosen articles and builds a fact sheet: every fact with an"
               " exact quote from its source.", "check", max_calls=1, required=True),
    Specialist("writer", "Writer", "Writes the post from the fact sheet in the author's voice, learning from the drafts"
               " they rewrote, and rewrites it once from the reviewers' notes.", "voice", max_calls=2, required=True),
    Specialist("editor", "Editor", "Scores take, focus, logic, plain words, voice, ending and grounding against the"
               " author's own posts (plus the humanizer scanner, 40%), gives at most 3 fixes and runs the guardrails.", "review", max_calls=2, required=True),
    Specialist("fact_checker", "Fact-checker", "Checks every claim in the post against the fact sheet, on a"
               " different model family from the writer.", "check", max_calls=2),
    Specialist("final_check", "Final check", "Blocks sweeping predictions, reports stated as done deals, misleading"
               " comparisons and hooks the post doesn't deliver. Nothing is saved as passing without it.", "check", max_calls=2, required=True),
    Specialist("onepager", "One-pager", "Writes the companion page linked in the first comment, from the fact"
               " sheet.", "writer", max_calls=1),
]
WORKERS = [s.name for s in SPECIALISTS if s.name != "supervisor"]
