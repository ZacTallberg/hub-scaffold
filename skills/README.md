# Skills

A skill is a directory with a `SKILL.md` (front-matter `name` + `description`, then the
instructions) and optionally the scripts and text assets it uses. Skills here are runtime-neutral:
Claude Code installs them under `~/.claude/skills/<name>/`; Codex under `~/.agents/skills/<name>/`
(a distributor may prefix the name, for example `hub-<name>`, to keep shared skills apart from a
person's own); `agents/openai.yaml` carries Codex-specific metadata where a skill needs it.

## Skills are documentation, not payloads

A skill travels to every machine through whatever channel an adopter uses to distribute it — a
checkout, a package, a publish step that writes it into a store agents read. Keep every file in a
skill **UTF-8 text**, **at most 256 KiB per file** and **2 MiB per skill**. The reason is not
tidiness: a distributor that stores skills as documents refuses a binary, and one font or logo
committed under `skills/` then stops EVERY skill from publishing on every machine — silently, if
the refusal lands in a pipeline nobody reads.

- **Binaries live beside the skills, not in them.** Fonts, images and other payloads go in a
  sibling directory (for example `skill-assets/<skill>/`), and the skill's script resolves them
  through an explicit, ordered chain: the skill's own directory → the repository's asset directory
  relative to the script (when run from a checkout) → a documented install location → an operator
  override. If nothing in the chain has the file, the script **fails loudly and names every place
  it looked** — it never silently emits output in a fallback.
- **Paths inside a skill are relative to the selected `SKILL.md`** (write `<skilldir>/scripts/x.py`
  in instructions), never one machine's profile path. An installed copy may live under a prefixed
  name, and a hardcoded `C:\Users\<someone>\...` works on exactly one machine.
- **Check the rule where the mistake is made.** If your distributor enforces a text-only rule, the
  real proof is its own dry-run over the source tree before merge — the publish operation is the
  check. Do not bank a separate test that re-implements the distributor's rule; it drifts from the
  real one (see `docs/TESTING.md`).

## Shipped skills

- **verification-closer** — an independent, transient closer for one rare critical boundary. See
  `campaigns/verification-closer.md`.
