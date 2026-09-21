#!/usr/bin/env python3
"""Trigger-precision audit for the vendored A+C skill kernel.

Two modes:
  --lint-only   CI mode: every vendored SKILL.md has parseable frontmatter
                with non-empty name + description, and sane size bounds.
  (default)     lint + probe-set verification: each probe prompt must
                keyword-match its intended skill's description (the same
                frontmatter the host agent scores at selection time).

Exit 1 on any failure. Pure stdlib.
"""
import os, re, sys

SKILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      ".agents", "skills")

# probe prompt -> (intended skill, keyword stems that MUST appear in its description)
# grill-me is EXCLUDED: it ships disable-model-invocation (user-invoked by design).
PROBES = {
    "this UI feels bland, make it pop":              ("impeccable", ["bland", "bolder", "delight", "design", "polish"]),
    "tests are failing inexplicably, fix it":        ("systematic-debugging", ["debug", "root cause", "failure", "bug", "investigat"]),
    "context is getting huge, save tokens":          ("headroom", ["compress", "context", "token", "summar"]),
    "is there a skill for writing changelogs?":      ("find-skills", ["discover", "find", "install", "search", "skill"]),
    "scrape this page and click through the flow":   ("agent-browser", ["browser", "page", "click", "navigate", "automat"]),
    "implementation is done, integrate the work":    ("finishing-a-development-branch", ["integrat", "complet", "tests pass"]),
    "review this PR before we merge":                ("verification-before-completion", ["verif", "claim", "evidence", "complet", "check"]),
}

def parse_frontmatter(path):
    """Return (meta dict, body_bytes) or (None, reason)."""
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError as exc:
        return None, str(exc)
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", raw, re.S)
    if not m:
        return None, "no frontmatter block"
    meta = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" in line and not line.startswith((" ", "-", "\t")):
            k, _, v = line.partition(":")
            v = v.strip().strip('"')
            if v in (">", "|",">-", "|-"):
                # YAML folded/literal scalar: accumulate indented continuation lines
                block = []
                i += 1
                while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                    block.append(lines[i].strip())
                    i += 1
                meta[k.strip()] = " ".join(b for b in block if b).strip('"')
                continue
            meta[k.strip()] = v
        i += 1
    return meta, raw

def main():
    lint_only = "--lint-only" in sys.argv
    failures = []

    if not os.path.isdir(SKILLS):
        print(f"FATAL: vendored kernel dir missing: {SKILLS}")
        return 1

    names = sorted(d for d in os.listdir(SKILLS) if os.path.isdir(os.path.join(SKILLS, d)))
    descs = {}
    boot_chars = 0

    print(f"── Lint: {len(names)} vendored skills ──")
    for name in names:
        path = os.path.join(SKILLS, name, "SKILL.md")
        if not os.path.exists(path):
            failures.append(f"{name}: SKILL.md missing")
            print(f"  FAIL {name}: SKILL.md missing")
            continue
        meta, raw = parse_frontmatter(path)
        if meta is None:
            failures.append(f"{name}: {raw}")
            print(f"  FAIL {name}: {raw}")
            continue
        d = meta.get("description", "")
        if not meta.get("name") or not d:
            failures.append(f"{name}: frontmatter missing name/description")
            print(f"  FAIL {name}: missing name/description")
            continue
        if len(d) < 40:
            failures.append(f"{name}: description too short for reliable triggering")
            print(f"  FAIL {name}: description <40 chars")
            continue
        descs[name] = d.lower()
        boot_chars += len(meta.get("name", "")) + len(d)
        ui = " [user-invoked]" if str(meta.get("disable-model-invocation", "")).lower() in ("true", "1") else ""
        print(f"  ok   {name:32s} desc={len(d):4d}ch body={len(raw)//1024:3d}KB{ui}")

    print(f"── Boot listing budget: ~{boot_chars//4} tokens ({len(names)} skills) ──")
    if boot_chars // 4 > 2000:
        failures.append(f"boot listing ~{boot_chars//4} tok exceeds the ~2K cap")

    if not lint_only:
        print("── Probe set (mechanical keyword contract check; user-invoked skills excluded) ──")
        for probe, (want, stems) in PROBES.items():
            if want not in descs:
                failures.append(f"probe '{probe[:30]}…': intended skill {want} not vendored/linted")
                print(f"  FAIL '{probe[:38]}…' -> {want} (not linted)")
                continue
            d = descs.get(want, "")
            hits = [s for s in stems if s in d]
            if want not in descs:
                failures.append(f"probe '{probe[:30]}…': intended skill {want} not vendored/linted")
                print(f"  FAIL '{probe[:38]}…' -> {want} (not linted)")
            elif len(hits) >= 2:
                print(f"  PASS '{probe[:38]}…' -> {want} (stems: {', '.join(hits[:4])})")
            else:
                failures.append(f"probe '{probe[:30]}…' -> {want}: only {len(hits)} keyword stem(s) "
                                f"{hits} in description — trigger contract weak")
                print(f"  WARN '{probe[:38]}…' -> {want} (stems: {hits or 'none'})")

    if failures:
        print(f"\nAUDIT FAILED: {len(failures)} issue(s)")
        for f_ in failures:
            print(f"  - {f_}")
        return 1
    print("\nAUDIT PASSED: lint + trigger contracts verified")
    return 0

if __name__ == "__main__":
    sys.exit(main())
