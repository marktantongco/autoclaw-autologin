# AGENTS.md — Session Discipline for Agent Contributors

Operating rules for any agent (or human) committing to this repo. The
vendored skill kernel below is this repo's own supply chain — treat it
like code, not like documentation.

## Session start — the /grill-me gate (SMP Stage-2)

`grill-me` is vendored at `.agents/skills/grill-me` with
`disable-model-invocation: true`. That is a FEATURE, not a limitation:
the agent cannot grill itself, so the interrogation must be
user-invoked.

- Before any high-stakes work (kernel edits, release cuts, auth-surface
  changes), the operator runs `/grill-me` and the agent answers until
  the plan survives. The agent's job is to request the gate, answer
  honestly, and update the plan — never to execute around an unanswered
  grill.
- This maps to the SMP v5.4-PD Workflow Stage 2 (Brainstorm: 2–3
  options for high-stakes work, await approval when cost is high).
  `/grill-me` IS the Stage-2 approval ritual; the gate is enforced by
  the agent, the trigger stays with the operator.

## Kernel edits — the CI guard runs you

- `.agents/skills/` is a closed set: 15 skills from 6 pinned sources
  (see `.agents/skills/VENDORED.md`). Additions require an eviction
  decision, not just free space.
- NEVER edit vendored skill files in place. Re-vendor at the pinned
  SHA, then regenerate the lockfile (`python3 scripts/gen_skills_lock.py`)
  and review the `computedHash` diff — that diff IS the supply-chain
  review.
- `.github/workflows/skills-lock-guard.yml` enforces both sides on every
  PR and every push to main: hash drift fails `--check`, and
  `audit_skill_triggers.py --lint-only` fails on frontmatter drift.
  Do not weaken the workflow to make a PR pass.

## Release discipline

- Local commits are cheap; the push is the promise. Ordinary work stays
  local until an explicit release cycle.
- Tags are semver over the whole ecosystem. Before choosing the next
  version, check `git ls-remote --tags origin` — never release below
  the newest existing tag, and never reuse a pushed tag.
- Credentials (PAT, tokens, account pools) never enter files, logs, or
  commit messages; they travel as environment variables only.
