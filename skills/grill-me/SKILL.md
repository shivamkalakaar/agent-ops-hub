---
name: grill-me
description: Interview the user with 10-15 targeted questions BEFORE building anything, write a short spec with acceptance tests, get a yes, build, then check the result against the spec. Use when the user asks Claude to build, create, make, code, design, draft or generate anything non-trivial (app, website, script, tool, document, deck, spreadsheet, campaign, plan, automation), even when the request looks detailed, and above all for vague one-liners like "make me a dashboard" or "build an app for my gym". Also use when the user says "grill me" or "interview me first". Not for pure questions, explanations, debugging, tiny edits or reformatting, and not to re-interview a project that was already grilled.
license: MIT
metadata:
  brand: How to AI by Ruben Hassid
  version: 2.0.0
  updated: 2026-09-29
---

# Grill Me

A build request captures about 20% of what the user pictures. Building from that 20% gets thrown away.
Ten to fifteen good questions cost two minutes and save two hours. This skill picks the questions that
change the build most, turns skipped ones into visible assumptions, writes a spec with tests, and checks
the finished work against it.

Commands run from the skill folder: `python3 scripts/grill.py ...` (use `python` if `python3` is missing).

## Core rules
- Interview first. No "quick draft" or "starting point": a draft anchors the user and turns questions into a formality.
- Never ask what you know. Scan the conversation, uploaded files and memory first; pass those question ids as `--known`.
- Every question must change the build. If all answers lead to the same decision, drop it.
- Batch, do not barrage: rounds of 4-6 numbered questions (1 foundations, 2 shape, 3 edges). Number across rounds so the user can answer by number.
- Use tappable options (if the interface has them) for `choice` questions; plain text for open ones.
- Budget: 10-15 questions in total, including your own follow-ups. Hobby script 10, client-facing product 15.

## Workflow
1. Triage. Non-trivial (apps, sites, scripts over ~20 lines, documents, decks, sheets, campaigns, plans, automations)? Grill. One-liner, explanation, edit, debug, conversion? Do not. A project already grilled gets 1-2 clarifying questions, never a second interview.
2. Detect the type: `python3 scripts/grill.py types --detect "the user's request"`. Pick one or two types (`types` lists all 13). No fit: use universal questions only.
3. Mine context. List every bank question the conversation already answers; note their ids.
4. Ask round 1: `python3 scripts/grill.py ask --type website --known id1,id2`. Use the bank wording, add the user's own nouns, show the options for choice questions. Never show the `Drives:` line to the user; it is for you. Wait for answers.
5. Between rounds run `python3 scripts/grill.py coverage --type website --known ...`. It shows areas settled vs open, weight left, and says CONTINUE or STOP. Then `ask` again (add `--extra N` for follow-ups you asked outside the bank; `--skip-areas taste` drops an area).
6. Add adaptive follow-ups (below) when an answer opens something the bank cannot know.
7. Stop when the coverage advice says STOP, at 15, or when the user is tired. Everything unasked becomes an assumption.
8. Ledger: `python3 scripts/grill.py assume --type website --known ... --json` prints the default for each unanswered question, with a risk level. Copy the ones you rely on into the spec.
9. Write the spec (below), then `check`, fix, `render`, show it, and get an explicit yes before building.
10. Build against the spec. If the user later asks for something that contradicts a line, point at the line and confirm the change; update the spec; do not drift silently.
11. Acceptance run (below) before you say it is done.

## Adaptive follow-ups (beyond the bank)
The bank is generic on purpose. Your best questions come from the user's own answers. Ask one when an answer:
- contains a vague word ("simple", "modern", "fast", "professional"): "Show me one thing that counts as simple to you."
- contradicts an earlier answer or the request: name both lines and ask which wins.
- reveals a person, tool or number nobody has mentioned: ask how it is involved.
- says "like X": ask what exactly to copy from X and what to change.
Follow-ups count toward the 15. If you cannot say what you would do differently for each answer, do not ask.

## Handling pushback
- "Just build it": respect it, compress do not skip. Run `python3 scripts/grill.py ask --type T --fast`: the 3 highest-weight questions in one message, and say you will proceed on stated assumptions right after.
- They decline even those: build now. Put `assume` output at the top of the deliverable as "Correct me before I build" so wrong guesses are cheap to fix. Skip the acceptance run's ceremony but still test the must-haves yourself.
- They answer "whatever you think" to a question: record its `if_skipped` default as an assumption and move on.

## The spec
JSON in `assets/spec.schema.json` shape; complete example in `assets/spec-example.json`. Fields: title, type[], goal (one sentence), success (observable), users, must_have[{item, accept}], nice_to_have[], out_of_scope[], constraints[], assumptions[{text, from, risk}], reject_if[], open_risks[].
- Every must-have has an `accept` test anyone can run: what you do and what you must see. "Works well" is not a test.
- `success` names a number, a time, or something a person can do. `reject_if` comes from the "what would make you send it back" answer.
- `out_of_scope` is never empty, and a term never sits in both must_have and out_of_scope.
- Assumptions carry a risk (`high` = the build changes a lot if wrong). Ask the user to confirm any `high` one before building; `check` warns on them.
- `python3 scripts/grill.py check spec.json` must exit 0. `python3 scripts/grill.py render spec.json` prints Markdown to paste; add `--format html --out spec.html` for a printable page. Assumptions appear last as "Correct me before I build".

## Acceptance run
After building: `python3 scripts/grill.py accept spec.json` (add `--full` to include success and out-of-scope). It prints one row per accept test and reject_if. Verify each for real (open it, click it, run it), then report a table with pass, partial or fail and one line of evidence.
- Lead with failures. A failed must-have or triggered reject_if means not done: say so, then fix or ask.
- Never mark pass from memory of the code. If you could not check something, write "not checked" and why.
- Then list which assumptions turned out wrong or still unconfirmed.

## If code cannot run
Read `assets/question-bank.json` directly. Match the request to a type by its `cues`; take questions where `types` contains it plus those with an empty `types` (universal). Sort by `weight` (3 first), then `round`; ask 4-6 per round, one per `area`, and always include one universal. Unanswered questions: use `if_skipped` as the assumption. Write the spec by hand in the same fields, check the rules above by eye, and do the acceptance run as a table you fill in yourself.

## File map
| Path | Open or run it when | What you get |
|---|---|---|
| `scripts/grill.py` | any step above | `types`, `ask`, `coverage`, `assume`, `check`, `render`, `accept`; `--help` on each |
| `assets/question-bank.json` | no code execution, or you need a question's full text | 13 types with detection cues, 14 universal and about 106 type questions with area, round, weight, options, what each drives, default if skipped |
| `assets/spec.schema.json` | writing or debugging a spec | field types and every rule `check` enforces |
| `assets/spec-example.json` | writing your first spec | a complete spec that passes `check` |

## Example (habit tracker)
User: "Build me a habit tracker app." Type `app`, nothing known.
Round 1: who uses it, where it runs, what it keeps, what triggered this now, what a normal session looks like.
Answers: "just me, phone browser, 6 habits, my streaks keep breaking". Follow-up (not in the bank): "When a streak breaks, reset to zero or forgive a day?" That answer changes the core logic.
Then a second and third round (10-12 questions in all), `assume` for the rest, a spec with tests such as "skip one day: the streak keeps its count", the user's yes, the build, and `accept`.
