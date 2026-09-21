#!/usr/bin/env python3
"""Generate skills-lock.json for the vendored A+C kernel in autoclaw-autologin.
Scheme (documented in the lockfile itself): computedHash = SHA-256 over
sorted relative file paths, each contribution = hashlib update(path + '\\0' + bytes).
"""
import hashlib, json, os, sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".agents", "skills")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills-lock.json")

CHECK = "--check" in sys.argv

# vendored name -> (source repo, pinned SHA, skillPath inside source repo)
PINNED = {
    "using-superpowers":             ("obra/superpowers", "5bf4e78011075bcfc0dc295f0724994cd123ee71", "skills/using-superpowers"),
    "brainstorming":                 ("obra/superpowers", "5bf4e78011075bcfc0dc295f0724994cd123ee71", "skills/brainstorming"),
    "writing-plans":                 ("obra/superpowers", "5bf4e78011075bcfc0dc295f0724994cd123ee71", "skills/writing-plans"),
    "executing-plans":               ("obra/superpowers", "5bf4e78011075bcfc0dc295f0724994cd123ee71", "skills/executing-plans"),
    "systematic-debugging":          ("obra/superpowers", "5bf4e78011075bcfc0dc295f0724994cd123ee71", "skills/systematic-debugging"),
    "verification-before-completion":("obra/superpowers", "5bf4e78011075bcfc0dc295f0724994cd123ee71", "skills/verification-before-completion"),
    "dispatching-parallel-agents":   ("obra/superpowers", "5bf4e78011075bcfc0dc295f0724994cd123ee71", "skills/dispatching-parallel-agents"),
    "subagent-driven-development":   ("obra/superpowers", "5bf4e78011075bcfc0dc295f0724994cd123ee71", "skills/subagent-driven-development"),
    "finishing-a-development-branch":("obra/superpowers", "5bf4e78011075bcfc0dc295f0724994cd123ee71", "skills/finishing-a-development-branch"),
    "impeccable":                    ("pbakaus/impeccable", "f2c7051853848826aac2f4646581d62a732155ad", "skill"),
    "grill-me":                      ("mattpocock/skills", "c55ee46073ed923f86ce59a5eb3b6d895095d1b7", "skills/productivity/grill-me"),
    "grilling":                      ("mattpocock/skills", "c55ee46073ed923f86ce59a5eb3b6d895095d1b7", "skills/productivity/grilling"),
    "find-skills":                   ("vercel-labs/skills", "7407f3893ad4dceab546ac002c3ef806e4000c73", "skills/find-skills"),
    "agent-browser":                 ("vercel-labs/agent-browser", "44583ac8385d814ab98cbf40feec97620376b50e", "skills/agent-browser"),
    "headroom":                      ("roman-ryzenadvanced/headroom-skill", "118466d7737ebc42806c331e44bd251b5222d59b", "."),
}

def skill_hash(dirpath):
    h = hashlib.sha256()
    files = []
    for base, _, names in os.walk(dirpath):
        for n in names:
            fp = os.path.join(base, n)
            files.append((os.path.relpath(fp, dirpath), fp))
    for rel, fp in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        with open(fp, "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest(), len(files)

lock = {
    "version": 1,
    "generator": "scripts/gen_skills_lock.py (vendored kernel; scheme: SHA-256 over sorted relpath\\0content\\0)",
    "hashScheme": "sorted(relpath + NUL + filebytes + NUL) -> sha256",
    "skills": {},
}
total_files = 0
for name in sorted(PINNED):
    src, ref, sp = PINNED[name]
    digest, nfiles = skill_hash(os.path.join(ROOT, name))
    total_files += nfiles
    lock["skills"][name] = {
        "source": src,
        "sourceUrl": f"https://github.com/{src}",
        "ref": ref,
        "skillPath": sp,
        "vendoredPath": f".agents/skills/{name}",
        "files": nfiles,
        "computedHash": digest,
        "modifications": ("SKILL.src.md renamed to SKILL.md; template placeholders "
                          "concretized (scripts_path -> npx impeccable)") if name == "impeccable" else "none",
    }

if CHECK:
    # CI mode: verify the committed lockfile matches freshly computed hashes.
    # Exit 1 on drift -> PR fails until skills are re-vendored and the lockfile
    # diff (the supply-chain review) is approved.
    if not os.path.exists(OUT):
        print(f"CHECK FAILED: {OUT} missing")
        sys.exit(1)
    committed = json.load(open(OUT, encoding="utf-8"))
    errors = []
    for name, meta in sorted(lock["skills"].items()):
        old = committed.get("skills", {}).get(name)
        if old is None:
            errors.append(f"{name}: missing from committed lockfile")
        elif old.get("computedHash") != meta["computedHash"]:
            errors.append(f"{name}: HASH DRIFT (vendored files changed without re-lock)")
        elif old.get("ref") != meta["ref"]:
            errors.append(f"{name}: ref changed {old.get('ref','?')[:8]} -> {meta['ref'][:8]}")
    for name in sorted(set(committed.get("skills", {})) - set(lock["skills"])):
        errors.append(f"{name}: in lockfile but not vendored on disk")
    if errors:
        print("CHECK FAILED — skills-lock.json drift detected:")
        for e in errors:
            print(f"  - {e}")
        print("Fix: re-vendor the changed skill(s) at a pinned ref and run "
              "scripts/gen_skills_lock.py, then review the lockfile diff in the PR.")
        sys.exit(1)
    print(f"CHECK OK: {len(lock['skills'])} skills, {total_files} files, all hashes match lockfile")
    sys.exit(0)

with open(OUT, "w") as f:
    json.dump(lock, f, indent=2, sort_keys=True)
    f.write("\n")

print(f"lockfile written: {OUT}")
print(f"skills: {len(lock['skills'])}  total files hashed: {total_files}")
for name, meta in sorted(lock["skills"].items()):
    print(f"  {name:32s} {meta['source']:38s} {meta['ref'][:8]}  {meta['files']:3d} files  {meta['computedHash'][:12]}…")
