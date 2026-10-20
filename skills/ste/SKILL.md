---
name: ste
description: Write or rewrite text in ASD-STE100 Simplified Technical English, then check it with a bundled script that finds long sentences, banned modals, contractions, passive and continuous verbs, unapproved words and badly built warnings. Use only when the user asks for it by name ("/ste", "use ste", "apply ASD-STE100") or asks for Simplified Technical English. Not for paraphrased wishes such as "simplify this", "make it clearer" or "shorter sentences", and not for marketing, fiction or casual writing.
license: MIT
metadata:
  brand: How to AI by Ruben Hassid
  version: 2.0.0
  updated: 2026-09-29
---

# STE: Simplified Technical English

Write every sentence so a non-native reader cannot misread it. Do not announce that you use STE, do not name the standard, and do not explain the style unless asked. If the user later asks for more natural writing, ask one short question to confirm they want to leave STE.

Commands run from the skill folder: `python3 scripts/ste_check.py ...` (use `python` if `python3` is missing).

## Workflow
1. Classify the text. Procedural (steps someone follows) or descriptive (explanation, background). Mixed documents: section by section. Every limit depends on this.
2. Substitute vocabulary first. `python3 scripts/ste_check.py words` lists unapproved words with their approved replacement. Apply it while you draft.
3. Draft with the rules below.
4. Check: `python3 scripts/ste_check.py check draft.md --mode procedural` (or `descriptive`, or `auto`). Fix every finding, then run it again until it prints no findings. Code, paths, quoted UI text and proper nouns are skipped by the script and by you.
5. Deliver the text only. Never claim certified compliance. For aerospace or defence deliverables, say once that full compliance needs the official ASD specification (asd-ste100.org) and a human sign-off.

## Rules
Sentences
- Procedural: 20 words maximum. Descriptive: 25. Paragraph: 6 sentences maximum, one topic.
- One instruction per sentence. Two actions only if they happen at the same time.
- Condition before command: "If the pressure decreases, close the valve."
- Keep articles, subjects and verbs. Keep "that" after "make sure".
- A number, unit with number, abbreviation, quoted string, code identifier or proper noun counts as one word.

Verbs
- Allowed: infinitive, imperative, simple present, simple past, simple future, past participle as adjective.
- No present perfect, no continuous forms. No -ing word as a verb (allowed inside a technical name: "mounting bracket").
- Active voice. Passive only in descriptive text when the agent is unknown or unimportant.
- Instructions use the imperative: "Open the panel."
- Actions are verbs, not nouns: "compress the file", not "perform compression of the file".
- Modals: can, will, must. Not should, would, could, may, might. A hedge becomes a fact or a "can".
- No phrasal verbs: "set up" becomes "install", "go down" becomes "decrease".

Words
- One word, one meaning, one part of speech. Never rotate synonyms: name a thing once and repeat that name.
- Technical nouns and verbs (part names, tools, UI labels) stay as they are, used consistently.
- Noun cluster: 3 words maximum. Longer: break it with prepositions or hyphenate.
- American spelling. No "e.g.", "i.e.", "etc.": write "for example", "that is", or delete.

Punctuation
- No semicolons, no contractions. Parentheses only for references, abbreviations, item numbers.

Warnings
- WARNING is a risk of injury or death. CAUTION is a risk of damage. NOTE is information and never an instruction.
- The command or condition comes first, then the risk: "WARNING: Do not touch the terminal. The terminal has a dangerous voltage."

## What the script does not judge
`check` finds patterns. It cannot see meaning: a passive it misses, a synonym rotation, an over-long noun cluster. After it passes, reread once for those three. Before/after pairs are in `assets/examples.json` (`python3 scripts/ste_check.py examples`).

## If code cannot run
Read `assets/word-substitutions.json` and `assets/examples.json` directly and walk the list in step 4 by eye: sentence length, contractions, semicolons, should/would/could/may/might, has/have/had been and is/was being, -ing verbs, missing articles, one name per thing, unapproved words, warning order.

## File map
| Path | Open or run it when | What you get |
|---|---|---|
| `scripts/ste_check.py` | steps 2 and 4 | `check`, `words`, `examples`; `--help` on each |
| `assets/word-substitutions.json` | no code execution, or you need the reason for a swap | unapproved word, approved word, note |
| `assets/examples.json` | rewriting existing text, or unsure how a rule applies | verified before/after pairs (procedural, descriptive, warnings) |
