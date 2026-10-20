# DESIGN.md template

Copy this into the project as `DESIGN.md`, fill every line, and keep it updated. Every page uses these values through CSS variables.

## Brand in one line
<what it is> for <who>, feeling <three adjectives>.

## Color roles
| Role | Value | Used for |
|---|---|---|
| background | #______ | page background |
| surface | #______ | cards, panels |
| text | #______ | body text (contrast with background at least 4.5:1) |
| muted | #______ | secondary text |
| accent | #______ | the one action, links |
| accent-text | #______ | text on the accent color |
| danger | #______ | errors |

## Type
- Heading font: <name>, weights <..>
- Body font: <name>, weight <..>
- Scale (rem): 0.875 / 1 / 1.25 / 1.563 / 2 / 3
- Body line height 1.6 (sans) or 1.7 (serif). Line length under 75 characters.

## Space and shape
- Spacing scale: 4 / 8 / 12 / 16 / 24 / 32 / 48 / 72 / 112 px
- Radius: <0 / 4 / 8 / 16> px, used consistently
- Borders: <width and color>. Shadows: <none / one soft shadow>

## Components
- **Button (primary)**: accent background, accent-text, radius, hover and focus states
- **Button (secondary)**: outline
- **Link**: color, underline rule, hover
- **Card**: surface, padding, border
- **Form field**: border, focus ring, error message
- **Navigation**: layout on desktop and on phone
- **Footer**: contents

## Voice
- Three adjectives: <..>
- Sounds like: "<example line>" / "<example line>"
- Never sounds like: "<example line>"

## Rules
- No color, size or spacing outside this file.
- One accent color. One primary button per screen.
- Every image has alt text.
