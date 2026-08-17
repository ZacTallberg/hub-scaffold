# Authored Hub identity — visual brief

## Open question

How can one canonical cockpit remain instantly recognizable as the same high-throughput system
while every adopter feels deliberately art-directed for its own product?

## Findings

- Layout, status colors, interaction semantics, and realtime motion are system truth and must stay
  shared. Forking those would turn identity into drift.
- Identity belongs at the portable project boundary: a mark, two-hue accent relationship, display
  voice, surface character, and optional ambient motif are enough to make the shell unmistakable
  without inventing project-specific components.
- Every value must be a bounded token, not arbitrary CSS or markup. This keeps the renderer safe,
  preserves contrast and reduced-motion behavior, and lets upgrades replace the whole managed unit.
- Motion remains state-derived. A motif may change the shape of the resting field, but activity,
  warning, failure, completion, and connection are still driven only by canonical board state.

## Experience brief

The Hub should feel like the project has its own operations room, not like its name was pasted onto
a generic admin template. The authored layer is visible first in the mark, paired chromatic field,
typographic cadence, material treatment, and ambient geometry. The shared layer remains visible in
the Flow / Now / Next / Outcome hierarchy, semantic status vocabulary, accessibility, and literal
push behavior.

## Decision input

Add a validated `visual` object to `PROJECT/project.json`; have the shared shell consume it through
safe root attributes and CSS custom properties; keep semantic status hues fixed; and give the
initializer deterministic, overridable starter art direction so two new projects are distinct
before any renderer fork exists.
