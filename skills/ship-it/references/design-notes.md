# Design notes for the ship-it kit

## Pick a preset from the brief
Read the interview answers, name the feeling in three words, then match. If two fit, pick the one closer to the sites the user loves.

| Brief sounds like | Preset | Why |
|---|---|---|
| Consultant, coach, writer, newsletter, publication, "serious but human" | `editorial` | Serif type, paper, ink and one terracotta accent read as calm authority |
| Bakery, studio, salon, wellness, local shop, makers, "friendly, handmade" | `warm-craft` | Soft shapes, cream, forest green, big rounded buttons |
| Founder, creator, launch, event, product with a strong opinion, "loud" | `bold-contrast` | Heavy type, black and white, one cobalt accent, hard edges |
| Designer, photographer, agency, film, music, portfolio, "the work speaks" | `studio-dark` | Near-black, off-white, one amber accent, serif italic in headlines |
| Clinic, finance, legal, B2B software, anything that must feel safe | `clinical-clean` | White, teal, highly legible sans, soft outlines, lots of air |

Editorial, clinical-clean follow the visitor's dark mode; set `"dark_mode": false` to keep them light. Presets are a starting point: keep the preset, change tokens.

## Change tokens without breaking contrast
Tokens live under `"tokens"` in site.json (or flags: `--accent`, `--preset`, `--fonts`).
- Accent: `"tokens": {"colors": {"accent": "#1F6F43"}}` or `--accent "#1F6F43"`. The builder picks a readable ink for buttons, a hover shade, a tint, and a text-safe variant. It stops with a suggested replacement if the accent has under 3:1 contrast with the page.
- Other colors: `bg`, `surface`, `text`, `muted`, `border`, `danger` (and `dark` for the night palette). Text pairs need 4.5:1, so light-gray text on white fails. The build lists each failing pair.
- Shape: `"radius": {"sm": "4px", "md": "8px", "lg": "16px"}`; density: `"space_unit": "0.3rem"` (bigger is airier); width: `"container": "64rem"`.
- Type: `"type_scale": {"display": [2.5, 5]}` gives a headline size from phone to desktop, in rem. Feel: `"style": {"heading_weight": "700", "btn_radius": "999px", "decor": "glow"}` (decor is none, glow, blob, wash or grid). Run `build_site.py tokens NAME` to see every knob with its value.
- Fonts: prefer the preset's. To swap, give `family`, `google` (a valid Google Fonts family spec, weights the family really has) and `fallback`. A wrong spec breaks font loading for everyone, so build and look. `--fonts system` removes Google Fonts entirely (faster, more private, slightly plainer).
- Anything else: `"custom_css"` appended after the base styles. Use `var(--accent)` style variables, never raw colors or fonts; the checker flags both.

## Anti-generic rules
The AI-made look is: purple-to-blue gradient, centered hero over a stock photo, three identical cards, emoji icons, one highlighted word in every headline, uppercase labels above everything, "Empower your journey" copy. Avoid all of it.
- Start from something concrete in the user's world (a product, a place, a sentence they said) and let it set the words.
- Vary the blocks: `features` as `rows` for three to five long items, `cards` only for many short ones; a `split` with a fact panel; `steps` for a process. Never stack four card grids.
- One accent, one primary button per screen, at most one italic emphasis word in the whole page.
- No stock photos and no invented proof. A page without a testimonials or stats block beats one with made-up numbers. Real photos only when the owner supplies them.
- Headlines say what and for whom in plain words; buttons say the action ("Book a free call"), not "Learn more".
- Every claim, price and address comes from the user. Unknown means `TODO`, then ask.

## When to leave the kit
Use the kit for brochure sites: landing pages, portfolios, small business, event and product pages, with links, email and an optional form service. Leave it when the user needs accounts and logins, a database, payments or checkout, user-generated content, dashboards, search across data, or real-time behaviour. Say something like:

"This needs accounts and stored data, which a simple site cannot do on its own. I can still build the public marketing page with the kit, and the app itself as a proper web app. That is a bigger project: shall I start with the marketing page?"

Then hand-build the app: write DESIGN.md from `references/design-system-template.md` first, reuse the chosen preset's colors and fonts as the design tokens, keep secrets out of the code, and keep the same confirm-before-repo and confirm-before-publish rules.
