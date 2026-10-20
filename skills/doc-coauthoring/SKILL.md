---
name: doc-coauthoring
description: >-
  Co-author a business or technical document in three stages: gather context, build it section by section,
  then test it on a cold reader. Ships 14 document blueprints (decision doc, proposal, one-pager, PRD, RFC,
  project plan, post-mortem, strategy memo, business case, meeting brief, SOP, policy, report, status or
  launch doc), a draft checker, a cold-reader test kit and a Word/HTML exporter. Use when the user wants to
  write, draft, outline or tighten a doc, memo, proposal, spec, plan, SOP, policy, report or brief, says
  "write this up", or wants a Word file from a draft. Not for short emails or chat replies, social posts,
  ad copy, slide decks, or proofreading one paragraph.
license: Apache-2.0
metadata:
  brand: How to AI by Ruben Hassid
  version: 2.0.0
  updated: 2026-09-29
  source: https://github.com/anthropics/skills/tree/main/skills/doc-coauthoring
  source-license: Apache-2.0
---

# Doc Co-Authoring

Write documents that work for readers who were not in the room. You run three stages (context, drafting,
cold-reader test) and use the scripts for what a model does badly: counting, checking, converting.
Script paths below are relative to this skill folder; call them by that path while your working directory stays where
the document lives. Use `python3` (`python` if `python3` is missing). No network, no installs.

## Offer the workflow
Trigger: the user wants a doc, proposal, spec, plan, memo, SOP, policy or report, or names a type (PRD, RFC, decision
doc).
Explain in three lines and why it helps (the doc works for readers, including readers who paste it into Claude), then
ask "use it, or write freeform?":
1. Context: you dump what you know, I ask questions until I can discuss trade-offs instead of basics.
2. Drafting: we build one section at a time (questions, brainstorm, pick, draft, small edits).
3. Reader test: a fresh Claude with no context reads it, and we fix whatever confuses it.
Freeform: just write it, then offer `check`. If they want to skip a stage, confirm and skip it.

## 1. Context gathering
Goal: close the gap between what the user knows and what you know.
1. Ask five meta questions (shorthand is fine): document type; who reads it; what should change once they have read
   it; template or required format; constraints.
2. Pick the type: `python3 scripts/doc_check.py types`, then `python3 scripts/doc_check.py blueprint TYPE`. A user
   template beats the blueprint. If nothing fits, propose 3-5 sections and ask if the structure works.
3. Invite an unstructured dump: background, threads, why alternatives lost, politics, deadlines, stakeholder worries.
   Routes: stream of consciousness, channels to read, linked docs. With connected tools (Slack, Drive, ...) say you
   will read them, and ask before searching for names you do not know. Without tools, say so and ask them to paste.
4. Editing an existing shared doc: read its current state first. Images without alt text are invisible to Claude when
   readers paste the doc; offer to write alt text if they paste the images.
5. After the dump, ask 5-10 numbered questions about the gaps. Tell them shorthand works ("1: yes, 2: see #channel, 3:
   no, backwards compat").
Exit when your questions are about edge cases and trade-offs, not basics. Then ask: more context, or start drafting?

## 2. Draft section by section
Setup: create the file (an artifact if the environment has them, else a Markdown file such as `decision-doc.md` in the
working directory) with every blueprint heading as `## Name` and `[To be written]` under it. Keep the blueprint's
exact names so `check` can match them. Follow `write_order` from the blueprint (most unknowns first, summary last):
suggest a start, let the user choose.
With the first drafted section, say once: "Tell me what to change instead of editing the doc yourself; I learn your
style from that, e.g. 'cut the X bullet, Y covers it'." If they edit directly and ask you to re-read, note what changed
and apply it to later sections.
For each section:
1. Ask 5-10 specific clarifying questions.
2. Brainstorm 5-20 numbered points (more for complex sections): forgotten context, angles nobody raised. Offer more.
   Brainstorm lists stay in chat, never in the doc.
3. Curate: they reply "Keep 1,4,7", "Remove 3 (duplicates 1)", "Combine 11 and 12", with brief reasons. Freeform
   replies ("looks good but...") are fine; extract the choices.
4. Gap check: is anything important missing for this section?
5. Draft it into the placeholder with `str_replace` (or the Edit tool), following the blueprint's `good_looks_like`,
   word budget and must-have checks. Use only facts the user gave: never invent numbers, quotes or sources; label
   estimates as estimates.
6. Refine: apply each requested change as a surgical edit. Never reprint the whole document. With artifacts, link it
   after every edit.
After 3 rounds with no substantial change, ask whether anything can go without losing important information, then
confirm the section is done.
At about 80% done, announce a full read-through for flow, redundancy, contradictions, generic filler ("slop") and
whether every sentence carries weight. When all sections exist, read once more and give final suggestions.
Then run `python3 scripts/doc_check.py check DOC.md --type TYPE`. Fix every error (placeholders, missing must-haves)
or get the user's explicit OK to keep it. Verify each warning at its line: fix what a reader would trip on, drop false
positives (`--known CAC,ARR` for terms your readers know). The checker is a heuristic; do not chase the reading grade
alone.

## 3. Reader testing
Goal: prove the doc works for someone with no context. Tell the user this catches blind spots the authors cannot see.
1. Predict 5-10 questions readers would ask of this doc: start from the blueprint's `reader_questions`, add
   doc-specific ones and the user's own worries. Save them one per line in `questions.txt`.
2. Build the kit:
   `python3 scripts/doc_check.py kit DOC.md --type TYPE --questions questions.txt --out reader-test.txt`. It strips
   author-only notes (`<!-- -->`, `[[...]]`), writes ONE paste-ready file, and writes an author-only answer key
   (`reader-test-key.md`).
3. Run it. With sub-agents: give a fresh sub-agent the kit text as its whole prompt and nothing from this conversation
   (one sub-agent per question is stricter). Without: ask the user to open a brand-new chat (no memory, files or
   connectors), paste `reader-test.txt`, and paste the reply back.
4. Grade the answers against the key. Read AMBIGUOUS, CONTRADICTS, ASSUMED KNOWLEDGE and ASK THE AUTHOR too.
5. Report the misses plainly, fix their causes with surgical edits (back to stage 2), rebuild the kit, retest.
Exit when the reader answers everything correctly and surfaces no new ambiguity, contradiction or hidden assumption.

## 4. Finish
Announce that it passed. Ask the user to read it once more (they own it), verify facts, numbers and links themselves,
and confirm it achieves the impact they wanted. Offer one more review.
Tips: link this conversation in an appendix so readers see how it was built; use appendices for depth; update the doc
as real readers respond.
Word or print copy: `python3 scripts/export.py DOC.md --to docx --title "..." --author "..."` (add `--theme formal`,
`--page a4`). `--to html` gives one self-contained page that prints to PDF from Chrome with page numbers. Run `check`
first: export warns about leftover placeholders and notes. Tell the user the file path and to glance at the tables in
Word.

## Working style
Be direct and procedural; give the reason in a clause when it changes what the user does; do not sell the process. Ask
at once when context is missing. If the user seems frustrated, say it is taking longer than expected and offer faster
paths (for example several sections per round). The user can adjust the process at any step. Quality over speed.

## If code cannot run
Everything works by hand. Read `assets/doctypes.json` (or ask the user to paste it) for sections, budgets and
questions. Check by eye: bottom line in the first 120 words; no TBD or `[To be written]`; headings match the
blueprint; every action has an owner and a calendar date; no "next week" without a date; every statistic has a source;
acronyms defined at first use; sentences under 35 words; then walk the blueprint's `checks` labels as a checklist. For
the reader test, give the user a prompt to paste with the doc into a fresh chat: answer only from the document, say
"not in the doc" otherwise, then list what you assumed, what is ambiguous and what contradicts. For Word, tell them to
paste the Markdown into Word or Google Docs.

## File map
| Path | Open or run it when | What you get |
|---|---|---|
| `assets/doctypes.json` | choosing a type, or no code execution | 14 blueprints: sections with word budgets and pitfalls, write order, reader questions, must-have checks |
| `scripts/doc_check.py` | `types` and `blueprint` in stage 1, `check` after drafting, `kit` in stage 3 | type list, blueprint, findings with line numbers, cold-reader kit and answer key |
| `scripts/export.py` | the user wants a Word or print-ready file | `.docx` or one-file `.html` with page numbers, tables and lists |

Derived from Anthropic's doc-coauthoring skill (Apache-2.0); see NOTICE for what changed.
