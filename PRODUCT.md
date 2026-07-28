# Product

## Register

product

## Users

Fintech hiring managers, payments product people, and engineers who arrive from a LinkedIn
post with roughly forty seconds of patience. They are on a phone as often as a laptop. They
have seen many agent demos and assume most are videos or mockups, so their first question is
whether this thing actually runs and their second is whether it is honest about what is real.

The job to be done: run one payment through an agent, be stopped by it, understand why it
stopped, and decide. Everything else is secondary to that loop completing.

## Product Purpose

A working Open Banking payment agent that refuses to move money on its own. It verifies the
payee, checks consent, scores risk, and then pauses and explains itself before a human
decides. On approval it creates a real payment on the TrueLayer sandbox and hands off to the
bank for Strong Customer Authentication, which the agent cannot perform by design.

Success is a visitor completing the whole loop, including the pause, and coming away able to
describe the product decision it demonstrates: the interesting part of an agent that spends
money is not the spending, it is the authority to spend and the boundary where it stops.

## Brand Personality

Exact, plain-spoken, unhurried. Writes in sentences rather than status codes. States what is
simulated without being asked. Never oversells: the page says sandbox and test money in the
first screenful, and marks Verification of Payee as simulated at the point where it is used,
not in a footnote.

Three words: precise, candid, calm.

## Anti-references

- Generic fintech SaaS. No navy and gold, no hero metric row, no three identical feature
  cards, no stock gradient.
- Crypto and web3 aesthetics. No neon glow, no dark purple gradients, no glassmorphism, no
  animated mesh.
- The AI cream-and-sand default. No warm near-white paper background with a large serif
  headline. Light here means cool and neutral, not parchment.
- Demo theatre. No fake progress bars, no invented latency, no confetti on success.

## Design Principles

1. **The pause is the product.** The approval moment gets the most design attention, not the
   submit button and not the success state.
2. **Explain in sentences, not status codes.** The agent says why it stopped in prose a
   non-engineer can read. Raw values stay available underneath for the ones who want them.
3. **Label what is not real, where it is used.** Sandbox, test money, simulated payee check.
   Stated at the point of use, not hidden in a footer.
4. **Refusals are first-class states.** Held, denied and uncheckable are designed as
   carefully as approved. They are the states that prove the agent has judgment.
5. **Readable on a phone in bad light.** The audience arrives from a feed, one-handed.

## Accessibility & Inclusion

WCAG 2.2 AA. Body text at or above 4.5:1, large text at or above 3:1, verified rather than
assumed. Full keyboard path through the form, the approval decision, and the SCA handoff.
Status changes announced via a live region, since the agent's progress is the content.
Decisions never carried by color alone: every state has a text label. Motion respects
`prefers-reduced-motion` with a crossfade fallback.
