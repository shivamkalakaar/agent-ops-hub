"""The LinkedIn team: who exists, what each one does and its defaults. Synced to agent_registry on every run,
so the dashboard's /agents page always matches the code."""
from pathlib import Path

from core.settings import Specialist

AGENT = "market_intel"
AUTHOR = "Shivam"
PROMPTS = Path(__file__).parent / "prompts"
MAX_STEPS = 14  # specialist turns per run (3 full rewrite rounds fit); coordinator decisions don't count

SPECIALISTS = [
    Specialist("supervisor", "Coordinator", "Reads every report and decides who works next, within hard limits.",
               "writer", max_calls=MAX_STEPS, required=True),
    Specialist("scout", "Scout", "Proposes three angles from the AI Stack Brief and RSS stories, checks them against"
               " recent posts and the lessons, and picks one.", "judge", max_calls=1, required=True),
    Specialist("researcher", "Researcher", "Reads the chosen articles and builds a fact sheet: every fact with an"
               " exact quote from its source.", "check", max_calls=1, required=True),
    Specialist("writer", "Writer", "Writes the post from the fact sheet in the author's voice, and rewrites it from"
               " the editor's, fact-checker's and final check's notes.", "voice", max_calls=3, required=True),
    Specialist("editor", "Editor", "Scores hook, focus, brevity, concreteness, voice, human and grounding (plus the"
               " humanizer scanner, 40%) and runs the guardrails.", "judge", max_calls=3, required=True),
    Specialist("fact_checker", "Fact-checker", "Checks every claim in the post against the fact sheet, on a"
               " different model family from the writer.", "check", max_calls=3),
    Specialist("final_check", "Final check", "Repeats against recent posts, template moves, and the lessons from"
               " your feedback. Nothing is saved as passing without it.", "check", max_calls=3, required=True),
    Specialist("onepager", "One-pager", "Writes the companion page linked in the first comment, from the fact"
               " sheet.", "writer", max_calls=1),
]
WORKERS = [s.name for s in SPECIALISTS if s.name != "supervisor"]
