"""The newsroom: who exists, what each one does and its defaults. Synced to agent_registry on every run,
so the dashboard's /agents page always matches the code."""
from pathlib import Path
from typing import TypedDict

from core.settings import Specialist

AGENT = "substack"
AUTHOR = "Shivam"
PROMPTS = Path(__file__).parent / "prompts"
MAX_STEPS = 14  # specialist turns per run; supervisor decisions don't count

SPECIALISTS = [
    Specialist("supervisor", "Super agent", "Reads every report and decides who works next, within hard limits.",
               "writer", max_calls=MAX_STEPS, required=True),
    Specialist("research", "Research", "Picks the story and the musing to build on, reads the full articles and"
               " writes a brief: thesis, outline, facts with evidence.", "writer", max_calls=2, required=True),
    Specialist("writer", "Writer", "Writes the post from the brief in the author's voice; rewrites from the editor's,"
               " fact-checker's and final check's notes.", "voice", max_calls=3, required=True),
    Specialist("editor", "Editor", "Scores hook, thesis, structure, depth, concreteness, voice and human (plus the"
               " humanizer scanner, 40%) and leaves line notes.", "check", max_calls=3),
    Specialist("fact_checker", "Fact-checker", "Checks every factual claim against the source text and flags"
               " numbers that aren't in the sources.", "check", max_calls=3),
    Specialist("illustrator", "Illustrator", "Plans and generates the cover and inline images, has a vision model"
               " check each one, and draws charts from sourced numbers.", "image", max_calls=2),
    Specialist("final_check", "Final check", "Runs every guardrail plus a publish-readiness read. Nothing is"
               " saved as passing without it.", "check", max_calls=3, required=True),
]
WORKERS = [s.name for s in SPECIALISTS if s.name != "supervisor"]


class Workspace(TypedDict, total=False):
    # inputs
    slot: str
    items: list[dict]
    musings: list[dict]
    seed_draft_id: str | None          # forced from the dashboard ("Expand to Substack")
    recent_titles: list[str]
    voice_posts: list[str]
    lessons: str                       # what the owner's past decisions taught the newsroom (core/lessons.py)
    learn: bool                        # False in evals: don't consume the owner's queue decisions
    learned: dict
    # specialists' work
    brief: dict
    sources: list[dict]
    seed: dict | None
    drafts: list[dict]                 # [{"version", "md"}]
    edit_report: dict
    fact_report: dict
    images: list[dict]
    image_notes: list[str]
    image_headings: list[str]          # section headings the images were placed against
    final_report: dict
    candidates: dict                   # str(version) -> editor + fact-check results, for ranking.best()
    # orchestration
    settings: dict                     # specialist -> core.settings.Resolved
    seq: int
    work_steps: int
    calls: dict
    history: list[dict]                # [{"who", "summary"}] for the supervisor
    next: str
    instructions: str
    finish_reason: str
    final: dict                        # what was saved: title, subtitle, body_md, version, passed
    judge: dict
    draft_id: str
