# Vendored Agent Skill Kernel — A+C Hybrid

Vendored per the operator's A+C decision (post-v2.5.0 separate commit; v2.5.0/SPEC-8b7 shipped first
and stays atomic — see `release-assets/spec-ac-kernel-post-v250.md`).

## What this is
The 8-keeper kernel (plus superpowers execution trio and search/browser add-ons) copied **by value**
into the repo so the agent runs offline-deterministic, supply-chain-diffable skills.
Provenance and integrity live in `skills-lock.json` (per-skill `ref` = pinned source SHA,
`computedHash` = SHA-256 over sorted relpath+NUL+content+NUL).

| Layer | Skills (all pure markdown unless noted) |
|---|---|
| Kernel core | using-superpowers, brainstorming, writing-plans, executing-plans, systematic-debugging, verification-before-completion |
| Execution (C-mode) | dispatching-parallel-agents, subagent-driven-development, finishing-a-development-branch |
| Quality | impeccable (audit/critique/adapt/optimize/distill/delight absorbed; optional transient `npx impeccable` CLI) |
| Interaction | grill-me → grilling |
| Context | headroom ( consciously waived below the 1K-install gate: 4★/167 installs, zero-RAM, on-goal; substitute = caveman-compress 363,905 installs but engine-backed) |
| Meta | find-skills (keep its auto-install nag dismissed; vendored copy is authoritative) |
| Browser | agent-browser (52-line stub; real guide loads from the Rust CLI on demand — needs `npm i -g agent-browser && agent-browser install` on the host) |

## Update procedure (re-vendor, never in-place edit)
1. Clone source at new pinned SHA (tag or 40-char SHA; tree-URL form is safest per vercel-labs/skills#1123).
2. Replace the skill dir, re-run `python3 scripts/gen_skills_lock.py` from repo root.
3. Review the `computedHash` diff in the PR — that diff IS the supply-chain review.
4. Re-review any `scripts/` before trusting it: SKILL.md files are a prompt-injection surface (Snyk ToxicSkills: 36% of surveyed skills flawed).

## Explicitly NOT vendored (ghost-confirmed or engine-heavy)
- langchain-parallel (Python library, not a skill), research-deep, code-research, chain-of-thought,
  feature-research, orchestrate (standalone; obra's dispatching-parallel-agents covers it),
  ralph-prompt-multi-task (no GitHub repo — unauditable; substitutes if ever needed:
  subsy/ralph-tui 2,450★ / oh-my-claudecode ralph / belumume ralph-loop)
- browser-use (Python+Playwright+Chromium ~300–800MB — opt-in on the omarchy host only)
- caveman engine (Node+Go+MCP ~150–250MB resident; duplicates headroom)
- byted-web-search / parallel-web (API-keyed; opt-in on host — parallel pack also absorbs
  research-deep via parallel-deep-research 14,588 installs)

## Trigger-precision rule
Install surface stays ≤15 skills so the boot listing (frontmatter-only, ~50–100 tok/skill) stays
well under the ~1%-of-context cap. Every new skill must displace one (kernel is a closed set —
additions require an eviction decision, not just free space).
