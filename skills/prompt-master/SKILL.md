---
name: prompt-master
description: >-
  Restructure a messy, rambling or voice-dictated request into a clean task spec BEFORE executing it, so
  nothing buried in the dump is lost. Use when the message is a brain dump: long unformatted paragraphs,
  several tangled asks, contradictions, "sorry this is messy", the real ask buried mid-ramble, or pasted
  notes ending in "can you do this". A scorer decides whether to restructure at all, then atoms, slots and a
  loss check keep every constraint, number, date, name and "do NOT" alive. Not for short clear requests,
  follow-ups inside an ongoing task, or casual chat; restructuring a clean request is pure friction.
license: MIT
metadata:
  brand: How to AI by Ruben Hassid
  version: 2.0.0
  updated: 2026-09-29
  source: claude-skills.free (How to AI original)
---

# Prompt Master

A brain dump has the information; it is just disorganized. Run directly, a model anchors on the first task
mentioned, drops constraints buried mid-paragraph, averages contradictions and answers the last sentence
instead of the goal. Fix the structure first, then execute from the structure. The user also sees their own
request organized, which catches a misreading in five seconds instead of after a finished, wrong deliverable.

Commands run from this folder: `python3 scripts/dump.py ...` (use `python` if `python3` is missing). Save the
message to a file or pipe it with `-`. Every output is a suggestion from heuristics; you judge.

## Workflow

1. **Calibrate.** `python3 scripts/dump.py score dump.txt` counts six signals (over ~150 words with no
   formatting, several distinct asks, a contradiction, the ask buried mid-message, apologetic framing,
   dictation artifacts) and recommends:
   - `skip`: answer directly. One clear ask with lots of clean context is a well-fed prompt, not a dump.
   - `light`: compact readback, then proceed in the same response.
   - `confirm`: full structure, then wait for a yes. Also chosen for multi-task cues ("first... second...",
     "three things") once 2 signals are present.
   Override only for a stated reason. Both failures cost: running a tangled dump directly drops a buried
   constraint (invisible cause); ceremony on "fix the typo in line 3" is visible and teaches distrust, so
   when unsure between skip and light, choose skip.
2. **Mine.** `python3 scripts/dump.py atoms dump.txt` numbers every sentence or clause (a1, a2, ...) with tags
   (ask, constraint, context, question, aside, emotion), entities and possible contradictions. Add `--json >
   atoms.json` for step 6. Read the whole dump before deciding the task: the real goal is often mid-dump or
   implied by the complaints, not the first verb. Sort atoms into buckets:
   - **Goal** (the outcome, not the first verb), **Deliverable** (the artifact, with format if stated),
     **Context** the task depends on, **Constraints** (deadlines, tone, length, budget, things to avoid; the
     most often buried and dropped), **Tasks** (several distinct asks are listed separately, never merged or
     silently dropped).
   - **Nothing gets lost.** Every atom lands in a bucket or is parked or dropped with a stated reason.
3. **Resolve, do not average.** Contradictions ("keep it short... also cover X, Y, Z in detail") get one
   winner, never a mushy middle. Prefer the later statement (dumps are thinking in progress), unless a stated
   preference ("actually", "I think X is better") points elsewhere. Write it as an interpreted decision the
   user can veto in one word. Treat the flagged candidates as questions: some are false alarms.
4. **Fill gaps.** `python3 scripts/dump.py slots --detect --text dump.txt` guesses the task type (or pass
   `--type email`; `--list` shows all 14) and reports:
   - **already answered**, with the evidence atom: never ask about these. An avoidable question about
     something the user wrote proves the dump was not read, and is worse than a wrong guess.
   - **blockers**: required slots with no sane default, at most 3. Ask only these.
   - **assumptions**: everything else, stated with its default, not asked.
   If detection is `ambiguous`, that is usually the contradiction from step 3 (deck or one-pager).
5. **Show the structure, sized to the stakes.** Write SPEC.json (keys: `goal`, `deliverable`, `context[]`,
   `constraints[]`, `decisions[]`, `assumptions[]`, `questions[]` max 3, `parked[]`, `tasks[]`, `success[]`,
   `role`, `output_format`), then `python3 scripts/dump.py render SPEC.json --style readback|confirm`.
   - **Light (default):** `Taking from this: [goal] as [deliverable], constraints: [list]. Assuming
     [decisions]. Parked: [items]. Proceeding.` Then execute.
   - **Confirm:** stop and wait for a yes when a contradiction is material to the outcome, several tasks have
     unclear order or selection, the deliverable is costly to redo, or two readings of the goal diverge badly.
     The block has Goal, Deliverable, Key context, Constraints, Interpreted decisions, Open questions, Parked,
     Task order.
   - **Multi-task dumps** always confirm the task list itself: number the tasks, propose an order
     (dependencies first), ask whether to run all or start with one.
6. **Prove nothing was lost.** Mandatory in confirm mode and for dumps over ~250 words; optional in light
   mode. Write LEDGER.json mapping atom ids to buckets, then run
   `python3 scripts/dump.py check atoms.json ledger.json --spec spec.txt`. It fails on any unmapped atom, any
   parked or dropped atom without a reason, any unresolved contradiction, and any number, date, name, quote or
   negation ("do NOT ...") missing from the spec. Fix what it prints and rerun until it says OK. Ledger:
   ```json
   {"goal":[2],"deliverable":[1],"context":[3,4],"constraints":[5,6],"tasks":[],
    "parked":[{"id":7,"reason":"background only"}],"dropped":[{"id":8,"reason":"repeats a2"}],
    "resolved":[{"contradiction":"c1","decision":"one-pager wins, the speaker's own preference"}],
    "preserve":["9h to 2h"]}
   ```
7. **Execute from the structure**, not the original prose. If a parked detail turns out to matter, unpark it
   and say so. Do not re-explain the restructuring with the deliverable; the deliverable is the point. If the
   user does this kind of task repeatedly, offer once, briefly, to save it: `render SPEC.json --style prompt`
   writes a copy-ready prompt (role, task, context, constraints, output format, success criteria). Do not push.

## Degradation (no code execution)

The skill still works by hand. Apply the six signals from step 1 yourself (2 or more means restructure).
Number the sentences, bucket each, list parked items with reasons. Open `assets/task_types.json`, pick the
type whose `cues` fit, and walk its required slots: found in the text means never ask; no default means
blocker (max 3); otherwise state the `default_assumption`. Before executing, rescan the dump for every
number, date, name and negation and confirm each appears in your spec. Use the templates in step 5.

## File map

| Path | Open or run it when | What you get |
|---|---|---|
| `scripts/dump.py` | Steps 1, 2, 4, 5, 6 (`score`, `atoms`, `slots`, `render`, `check`) | Signal score and skip/light/confirm advice; numbered atoms with tags and contradiction candidates; answered slots, blockers, assumptions; readback, confirm block or copy-ready prompt; loss report |
| `assets/task_types.json` | Step 4 by hand, or to add a task type | 14 task types: detection cues, required and optional slots, blocker questions, default assumptions, spec hints |

## Example

**Dump:** "ok so we have this client meeting thursday and sarah wants a deck but honestly i think a one pager
is better, the client is an HVAC company, family owned, they were upset last quarter about response times
which we fixed, avg ticket time went from 9h to 2h, also need to mention the new pricing but do NOT bring up
the contract renewal yet legal said to wait, oh and can you make it not corporate sounding they hate that,
anyway something i can send sarah tonight"

`score` says 3 of 6 signals: light. `atoms` flags deck vs one-pager, settled by "i think a one pager is
better". `slots` finds audience, purpose, facts and deadline already answered: no questions. `check` confirms
9h, 2h, thursday, tonight, Sarah and the legal negation all reached the spec.

**Readback (light mode):** Taking from this: a client-ready one-pager (your call over Sarah's deck preference,
flagging that as a decision for you two) for the family-owned HVAC client, to send Sarah tonight. Leading
with the response-time win (9h to 2h), introducing new pricing. Constraints: no mention of contract renewal
(legal), non-corporate tone. Parked: last quarter's friction as background, not to be dwelled on. Proceeding
with the one-pager.

What the structure caught that direct execution misses: the deck-vs-one-pager conflict (surfaced, not
averaged), the legal constraint buried mid-ramble, and the real deadline (tonight for Sarah, not Thursday
for the client).
