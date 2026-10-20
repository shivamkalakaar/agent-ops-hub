---
name: handoff
description: >-
  Turn the current conversation into a verified handoff so a new AI chat, a colleague, or future-you can resume
  without losing decisions, constraints, or progress. Sweeps the thread for corrections, constraints, decisions,
  dead ends, files and exact values, builds HANDOFF.md plus a paste-ready resume prompt, and checks every quoted
  value against the transcript. Use when the user says "handoff", is hitting context limits or a thread that is
  too long, wants to brief a teammate on this conversation, or says they will "pick this up later"; offer it
  proactively when a long working thread is winding down. Not for summarizing external documents, articles or
  meetings; this skill summarizes the conversation itself.
license: MIT
metadata:
  brand: How to AI by Ruben Hassid
  version: 2.0.0
  updated: 2026-09-29
  source: claude-skills.free (How to AI original)
---

# Handoff

Long threads die two deaths: the context window fills, or the human walks away for a week. Restarting from
nothing costs hours and silently loses decisions, so the next session relitigates settled questions and repeats
rejected approaches. A handoff is a **state snapshot, not a story**: where things stand, what is ruled out,
what is next. Chronological narration ("First we discussed X...") is the number one failure; it buries the state.

Commands are relative to this skill folder (`python3 scripts/handoff.py ...`; use `python` if `python3` is
missing). Call the script by its full path but run it from the user's project folder, not from the skill folder.

## Workflow

1. **Pick the recipient.** Infer it or ask ONE question. `ai` (default): a fresh chat; needs exact constraints,
   working preferences and a paste-ready opening prompt. `human`: a colleague; more background on why the work
   exists, less instruction-style framing. `self`: future you; leans on reminders of intent ("you chose X because
   the client hated Y"), since you will half-remember everything.
2. **Sweep the transcript.** Claude Code: `python3 scripts/handoff.py sweep --latest` (newest session for the
   current folder; add `--cwd DIR` if running elsewhere). A saved chat or export: `sweep FILE` (Claude Code
   `.jsonl`, claude.ai or ChatGPT export `.json`, or text with `User:`/`Assistant:` lines; `--pick` chooses one
   conversation from an export). Add `--limit 0` for everything or `--only verbatim,open` to save tokens. The
   output is leads with turn numbers plus the files really written. Leads are not facts: open the turn and
   confirm each one. No file (a claude.ai chat)? Do the same mining by hand from the visible thread.
3. **Mine for what transfers**, in priority order:
   1. **Decisions and their reasons.** Not "we chose PostgreSQL" but "chose PostgreSQL over SQLite because of
      concurrent writes". Without the reason the next session reopens the decision.
   2. **Dead ends.** Approaches tried and rejected, and why; if it failed for a fixable reason, say so. The most
      valuable and most-omitted section: it is all that stands between the next session and every failed path.
   3. **Corrections the user made** ("shorter", "not that tone", "the deadline is Friday not Monday"). Each is a
      preference paid for once and never to be paid for again. They hide in throwaway lines.
   4. **Constraints stated once.** A requirement from message 3 never repeated still governs everything.
      Recency is not importance.
   5. **Artifact state.** Each file, draft or deliverable: name, location, status (final, draft, superseded by X).
      Only the current version of each. Never make the reader guess which version is live.
   6. **Open items.** In progress, blocked and on what, and the ONE concrete next step, starting with a verb.
4. **Add what this kind of work needs.** Detect the kind (the sweep prints a guess), then run
   `python3 scripts/handoff.py profiles KIND` (code, writing, research, analysis, design, planning, sales) and
   capture each field in the section it names. Set `kind` in the JSON. Anything else: `general`.
5. **Write HANDOFF.json.** Shape: `assets/handoff.schema.json`; a finished sample: `assets/example.json`.
   - **Verbatim** (`verbatim[]`): names and spellings, numbers, dates, URLs, IDs, error messages, key code, exact
     approved phrasings. Copy character for character; paraphrasing them is corruption, not compression.
   - Dates are calendar dates (`2026-10-09`), never "tomorrow". Nothing says "as above" or "as we discussed".
   - Every section is present: an empty one is `[]`, which says "nothing", not "forgot".
   - **Leave behind:** pleasantries, drafting back-and-forth (keep final state and rationale), abandoned tangents
     that produced no decision, anything the reader could not act on.
6. **Build.** `python3 scripts/handoff.py build HANDOFF.json --for ai` writes `HANDOFF.md` and, for `ai`,
   `resume-prompt.txt` (one block to paste into the new chat; it ends with a VERIFY-BACK step where the new chat
   restates goal, next step and two must-not-dos before doing anything). `--for human --html` adds a printable
   brief. It reports tokens and reading time per section; the target is a 2-3 minute read, one or two pages.
7. **Check.** `python3 scripts/handoff.py check HANDOFF.json --latest` (or `--transcript FILE`). Fix every
   error and read every warning; rerun until it ends `ok` or each remaining warning is justified. `F01` means a
   verbatim value is not in the thread: correct it or drop it, do not argue with it.
8. **Verify against the thread** before delivering. Is any user correction missing from preferences? Any
   constraint stated exactly once that did not make it in? Is each artifact's version the latest? Deliver the
   files (or the fenced markdown) and say what to paste where.

If the thread contains contradictions that were never resolved, do not silently pick a winner: list them under
`unresolved`.

## Fidelity rules

Everything in the handoff must trace to something actually in the thread. Do not fill gaps with plausible
inference: a handoff reader trusts the document completely, so an invented detail becomes an invisible landmine.
Put genuinely uncertain things under `uncertain` ("user implied but never stated"). Keep who-said-what straight:
a suggestion you made that the user never accepted is not a decision (the sweep shows the user's reaction).

## File map

| Path | Open or run it when | What you get |
|---|---|---|
| `scripts/handoff.py sweep` | Step 2, and whenever a thread is too long to re-read | Numbered leads: corrections, constraints, decisions, dead ends, open questions, verbatim values, files written |
| `scripts/handoff.py profiles` | Step 4 | The extra fields for a kind of work |
| `scripts/handoff.py build` | Step 6 | `HANDOFF.md`, `resume-prompt.txt`, optional `HANDOFF.html`, token and time report |
| `scripts/handoff.py check` | Step 7 | Structure and quality lint plus proof each verbatim value occurs in the thread |
| `assets/handoff.schema.json` | Step 5 | Every field of HANDOFF.json and what it means |
| `assets/example.json` | Step 5, when unsure how much detail | A complete, clean example |
| `assets/profiles.json` | Step 4 without code execution | Extra fields by kind of work |

## Without code execution

Everything still works by hand. Read `assets/profiles.json` for the kind and `assets/example.json` for the
shape. Mine the visible thread with the step 3 list. Write the markdown with these headings in order:
Objective, Current state, Decisions (and why), Dead ends (do not retry), Artifacts, Verbatim essentials,
Working preferences, Open items (Next step, Then, Blocked), Unresolved, Unconfirmed. Write "None." under an empty
one. For an AI recipient finish with a Suggested opening prompt that says: "Before doing anything, restate the
goal, the single next step and two things you must not do, then wait for my go." Then do step 8 as a checklist,
and re-read every quoted value against the thread yourself. If the sweep cannot read a file, paste the thread
into a text file with `User:` and `Assistant:` lines first.
