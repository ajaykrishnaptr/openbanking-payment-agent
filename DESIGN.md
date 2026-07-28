# Design

## Theme

Light, cool, typographic. The scene: a clearing-house reading room in north light. Paper that
is white rather than warm, ink-black type, one green ledger stamp for what completed, amber
for the one moment a human must act.

Colour strategy: **restrained**. Tinted neutrals carry the surface; green appears only on
completed steps and the primary action; amber appears only where the agent is waiting on a
person; red only on refusal. Under 10% of the page is coloured, so when colour shows up it
means something.

## Colour

OKLCH throughout. Neutrals carry a 0.004 chroma tint toward the brand hue (150) so the greys
belong to the green rather than reading as generic slate. Cream is explicitly rejected: hue
stays at 150, never 40-100.

| Role | Value | Use |
|---|---|---|
| `--bg` | `oklch(0.985 0.003 150)` | page |
| `--surface` | `oklch(0.968 0.005 150)` | panels, the run log |
| `--surface-raised` | `oklch(1 0 0)` | the approval card, so it lifts off the page |
| `--line` | `oklch(0.90 0.006 150)` | hairlines, 1px only |
| `--ink` | `oklch(0.22 0.02 150)` | body text, 15.8:1 on bg |
| `--ink-muted` | `oklch(0.45 0.018 150)` | secondary text, 7.4:1 on bg |
| `--primary` | `oklch(0.42 0.12 150)` | approve action, completed steps |
| `--primary-ink` | `oklch(0.98 0.01 150)` | text on primary |
| `--attention` | `oklch(0.62 0.14 75)` | waiting on a human |
| `--attention-bg` | `oklch(0.96 0.04 85)` | the approval card's field |
| `--refuse` | `oklch(0.50 0.17 25)` | held, denied, rejected |

Every pairing above was checked with a contrast calculation, not by eye. Nothing carries
meaning by colour alone; each state also has a text label and a distinct glyph.

## Typography

Three families, each with a job:

- **Newsreader** (serif, 400/500, optical sizing) — headings and the agent's narration. The
  narration is prose, so it gets the reading face.
- **Inter** (sans, 400/500/600) — interface text, labels, buttons.
- **IBM Plex Mono** (400/500) — money, account identifiers, payment ids, statuses. Anything a
  reader might compare digit by digit or copy.

Scale ratio 1.25. Display capped at 3.2rem, well under the 6rem ceiling, because this is a
tool rather than a landing page. Body measure capped at 68ch. `text-wrap: balance` on
headings, `pretty` on prose.

## Layout

Two columns above 900px: the intent form on the left at a fixed comfortable width, the agent's
run and its narration on the right. Below 900px they stack, form first, and the run region
scrolls into view when it updates. No cards for the run steps; they are a bordered list with
generous leading, since a card grid would be the lazy answer here. The approval card is the
one genuinely raised surface on the page, which is what makes it read as the moment.

Semantic z-index scale: `--z-sticky: 10`, `--z-overlay: 20`, `--z-toast: 30`. No arbitrary
values.

## Motion

Steps appear as the agent completes them: 180ms fade with a 4px rise, ease-out-quart,
staggered by arrival rather than by index. The approval card gets a slightly longer 260ms
entrance because it is asking for something. Reduced motion replaces both with an instant
crossfade. Content is visible by default and never gated behind a transition class.

## Components

- **Field** — label above input, mono for numeric fields, error text below tied by
  `aria-describedby`.
- **Run step** — glyph, node name, one-line result, optional detail. Four states: pending,
  running, done, refused.
- **Approval card** — the raised surface. Prose explanation first, the reasons as a plain
  list, the two actions last, with the destructive one visually secondary but equally
  reachable by keyboard.
- **Handoff panel** — appears after approval with the payment id, status, and the bank link
  for SCA, labelled as leaving the site.
