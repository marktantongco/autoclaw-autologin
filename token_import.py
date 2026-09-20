"""Token import — Synergy 7 (Phase 3.1): server-first no-CloakBrowser mode.

Source: sitimas9/autoclaw2api ("deliberately drops CloakBrowser — token
harvesting performed elsewhere"), promoted from deferred to the Phase-3.1
token-supply lifeline after the live probe confirmed the upstream OAuth
route deprecation (POST /userapi/overseasv1/google-oauth-url now returns
HTTP 405 for properly-signed requests; verified 2026-09-19 against
autoglm-api.autoglm.ai — signature middleware accepts, router 405s).

What this module does
  - Ingests credential files pasted via the import API or dropped into a
    watch directory (ACLAW_IMPORT_DIR), in three formats (auto-detected):
      autoclaw2api  — {email, access_token, refresh_token, user_id,
                       device_id, source_id, last_refreshed}
      desktop-ls    — raw AutoClaw desktop localStorage dump
                      {access_token, refresh_token, ...} (email from JWT)
      tokens-json   — a fragment shaped like our own tokens.json
                      {accounts: [...]} or a bare account dict
  - Validates the JWT (expiry; jti claim carries the email — same logic
    the OAuth callback uses), preserves the DESKTOP device_id when present
    (refresh_tokens may be device-bound; a fresh uuid4 risks rejection).
  - Merges into the encrypted tokens.json store via auth.add_token
    (Fernet-at-rest + wipe-guard semantics inherited).
  - Never raises into the request path: every import returns a per-record
    result; counters + last_error feed /health (fail-safe contract).

What this module is NOT
  - Not a Google-SSO HTTP client. Google login needs a real browser
    (reCAPTCHA/consent/device checks — research 8-a Section D); harvesting
    stays browser-based, just ELSEWHERE (desktop app / CloakBrowser box).
  - Not an app-login/oneclick automation (unknown attestation; the
    ACLAW_APPLOGIN_PROBE knob only REPORTS reachability).

Env knobs (config.py mirrors these; see deploy/env.template):
  ACLAW_NO_BROWSER        1|0    master switch: refuse/flag browser flows
  ACLAW_OAUTH_MODE        auto|browser|import|none   governs /api/login-url
  ACLAW_IMPORT_DIR        path   watch-dir ingested at boot + via API
  ACLAW_IMPORT_ON_START   1|0    ingest the dir at boot (default 1)
  ACLAW_IMPORT_FORMAT     auto|autoclaw2api|desktop-ls|tokens-json
  ACLAW_IMPORT_DEDUPE     email|refresh_token   merge key (default email)
"""

import glob
import hashlib
import json
import logging
import os
import threading
import time
import uuid

logger = logging.getLogger("autoclaw.token_import")

_LOCK = threading.Lock()
_STATS = {
    "files_seen": 0, "records_seen": 0, "imported": 0, "updated": 0,
    "rejected": 0, "last_import_ts": None, "last_error": None,
    "oauth_upstream": "unknown",  # live|405|unknown — set by the login-url gate
}


def _env(key, default):
    return os.environ.get(key, default).strip()


def _truthy(val):
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def no_browser():
    """True when ACLAW_NO_BROWSER=1 — proxy must not offer browser flows."""
    return _truthy(_env("ACLAW_NO_BROWSER", "0"))


def oauth_mode():
    """Resolved OAuth mode: auto | browser | import | none.

    auto  — browser flows stay available (operator's call); if the upstream
            probe has reported 405, auto degrades to import with a hint.
    import/none — browser flows are refused with an actionable error.
    """
    mode = _env("ACLAW_OAUTH_MODE", "auto").lower()
    return mode if mode in ("auto", "browser", "import", "none") else "auto"


# ── JWT helpers (mirror the OAuth callback's jti-email extraction) ────────

def _jwt_payload(access_token):
    """Decode the JWT payload section. Returns dict or None."""
    if not isinstance(access_token, str) or access_token.count(".") < 2:
        return None
    try:
        import base64
        part = access_token.replace("Bearer ", "").split(".")[1]
        part += "=" * (4 - len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return None


def _jwt_email(access_token):
    payload = _jwt_payload(access_token)
    if isinstance(payload, dict) and payload.get("jti"):
        return str(payload["jti"])
    return None


def _jwt_expired(access_token):
    payload = _jwt_payload(access_token)
    if not isinstance(payload, dict):
        return None  # unknown — token may be opaque; let refresh decide
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return time.time() >= float(exp)
    return None


# ── Format detection + normalization ─────────────────────────────────────

def _looks_like_account(obj):
    return (isinstance(obj, dict) and isinstance(obj.get("access_token"), str)
            and obj.get("access_token"))


def detect_format(obj):
    """Classify one record: autoclaw2api | desktop-ls | tokens-json | None."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("accounts"), list):
        return "tokens-json"
    if _looks_like_account(obj):
        if obj.get("source_id") or (obj.get("email") and obj.get("device_id")):
            return "autoclaw2api"
        return "desktop-ls"
    return None


def _normalize(obj, fmt):
    """One record → normalized account dict (minus email) + warnings."""
    warnings = []
    if fmt == "tokens-json" and isinstance(obj.get("accounts"), list):
        return None, ["tokens-json fragment must be expanded by caller"]
    if not _looks_like_account(obj):
        return None, ["missing access_token"]
    access = obj["access_token"].strip()
    refresh = (obj.get("refresh_token") or "").strip()
    if not refresh:
        warnings.append("missing refresh_token — account cannot self-refresh")
    device_id = (obj.get("device_id") or "").strip()
    if not device_id:
        device_id = str(uuid.uuid4())
        warnings.append(
            "missing device_id — minted a fresh uuid4; desktop refresh_tokens "
            "may be device-bound, verify with a live refresh")
    else:
        warnings.append("preserved desktop device_id")
    exp = _jwt_expired(access)
    if exp is True:
        warnings.append("access_token already expired — refresh on first use")
    elif exp is None:
        warnings.append("access_token not JWT-parseable (opaque token?)")
    acct = {
        "access_token": access,
        "refresh_token": refresh,
        "user_id": obj.get("user_id") or obj.get("userId") or "",
        "device_id": device_id,
        "source_id": obj.get("source_id") or "import",
    }
    for k in ("last_refreshed", "added_at"):
        if isinstance(obj.get(k), (int, float)):
            acct[k] = int(obj[k])
    return acct, warnings


def _derive_email(obj, fmt):
    email = obj.get("email") or obj.get("user_name") or _jwt_email(
        obj.get("access_token") or "")
    if not email and obj.get("user_id"):
        email = f"user_{obj['user_id']}"
    return (str(email).strip().lower() if email else None)


# ── Core import ───────────────────────────────────────────────────────────

def import_record(obj, source="api"):
    """Import one credential record. Returns a result dict, NEVER raises.

    {ok, email, action: imported|updated|rejected, warnings, error}
    Merge/dedupe honours ACLAW_IMPORT_DEDUPE (email | refresh_token).
    """
    result = {"ok": False, "email": None, "action": "rejected",
              "warnings": [], "error": None, "source": source}
    try:
        fmt = _env("ACLAW_IMPORT_FORMAT", "auto").lower()
        if fmt not in ("auto", "autoclaw2api", "desktop-ls", "tokens-json"):
            fmt = "auto"
        detected = detect_format(obj) if fmt == "auto" else (fmt if _looks_like_account(obj) else None)
        if detected is None:
            result["error"] = "unrecognized record format (need access_token)"
            return _finish(result)
        acct, warnings = _normalize(obj, detected)
        if acct is None:
            result["error"] = warnings[0] if warnings else "normalization failed"
            return _finish(result)
        dedupe = _env("ACLAW_IMPORT_DEDUPE", "email").lower()
        email = _derive_email(obj, detected)
        if dedupe == "email" and not email:
            # Hash the refresh_token so the record is still addressable.
            email = ("imported_" + hashlib.sha256(
                (acct["refresh_token"] or acct["access_token"]).encode()
            ).hexdigest()[:10])
            warnings.append("no email derivable — assigned stable pseudonym "
                            f"{email}")
        elif not email:
            email = "imported_" + hashlib.sha256(
                (acct["refresh_token"] or acct["access_token"]).encode()
            ).hexdigest()[:10]
            warnings.append("dedupe=refresh_token — pseudonym assigned")
        result["email"] = email
        action = "imported"
        try:
            from auth import load_tokens, add_token
            existing = [a.get("email") for a in
                        (load_tokens().get("accounts") or [])]
            if email in existing:
                action = "updated"
            add_token(email=email,
                      access_token=acct["access_token"],
                      refresh_token=acct["refresh_token"],
                      user_id=acct["user_id"],
                      device_id=acct["device_id"],
                      source_id=acct["source_id"])
        except Exception as exc:
            result["error"] = f"store write failed: {exc}"
            return _finish(result)
        result["ok"] = True
        result["action"] = action
        result["warnings"] = warnings
        with _LOCK:
            _STATS["imported" if action == "imported" else "updated"] += 1
            _STATS["last_import_ts"] = time.time()
        logger.info("token import %s: %s (%s, %d warning(s))",
                    action, email, detected, len(warnings))
        return result
    except Exception as exc:  # defensive — import must never break serving
        result["error"] = f"unexpected: {exc}"
        return _finish(result)


def _finish(result):
    try:
        with _LOCK:
            if not result.get("ok"):
                _STATS["rejected"] += 1
                _STATS["last_error"] = result.get("error")
            _STATS["records_seen"] += 1
    except Exception:
        pass
    return result


def import_payload(payload, source="api"):
    """Import any supported payload shape (single record, list, tokens.json
    fragment). Returns {results, summary}. Never raises."""
    records = []
    if isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
        records = [a for a in payload["accounts"] if isinstance(a, dict)]
    elif isinstance(payload, list):
        records = [a for a in payload if isinstance(a, dict)]
    elif isinstance(payload, dict):
        records = [payload]
    summary = {"seen": len(records), "imported": 0, "updated": 0,
               "rejected": 0, "results": []}
    for rec in records:
        r = import_record(rec, source=source)
        summary["results"].append({
            "email": r["email"], "ok": r["ok"], "action": r["action"],
            "error": r["error"], "warnings": r["warnings"]})
        if r["ok"]:
            summary[r["action"]] += 1
        else:
            summary["rejected"] += 1
    return summary


def import_directory(path=None):
    """Ingest every *.json file in the import dir (boot hook + API trigger).
    Returns a per-file summary. Never raises."""
    d = path or _env("ACLAW_IMPORT_DIR",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  ".autoclaw_imports"))
    out = {"dir": d, "files": 0, "imported": 0, "updated": 0,
           "rejected": 0, "errors": []}
    try:
        if not d or not os.path.isdir(d):
            return out
        for fp in sorted(glob.glob(os.path.join(d, "*.json"))):
            out["files"] += 1
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                s = import_payload(payload, source=f"file:{os.path.basename(fp)}")
                with _LOCK:
                    _STATS["files_seen"] += 1
                out["imported"] += s["imported"]
                out["updated"] += s["updated"]
                out["rejected"] += s["rejected"]
                if s["rejected"]:
                    out["errors"].append(f"{os.path.basename(fp)}: "
                                         f"{s['rejected']} record(s) rejected")
            except Exception as exc:
                out["errors"].append(f"{os.path.basename(fp)}: {exc}")
                with _LOCK:
                    _STATS["last_error"] = f"{os.path.basename(fp)}: {exc}"
        return out
    except Exception as exc:
        out["errors"].append(str(exc))
        return out


def note_oauth_upstream(status):
    """Record the live-probed upstream OAuth state (live|405)."""
    try:
        with _LOCK:
            _STATS["oauth_upstream"] = str(status)[:16]
    except Exception:
        pass


# Report-only reachability probe for the NEW upstream auth routes
# (/userapi/v1/app-login, /userapi/v1/oneclick-login — desktop v1.17.6+).
# NOT an automation: no signing, no credentials — a 400002 "Middleware sign
# error" envelope itself proves the route EXISTS (middleware runs before
# routing), while a 405 proves it doesn't. Cached 10 minutes.
_probe_cache = {"ts": 0.0, "result": None}
_PROBE_TTL_S = 600.0


def applogin_probe(force=False):
    """Probe new-upstream auth-route reachability. Never raises. Active only
    when ACLAW_APPLOGIN_PROBE=1 (off by default — zero ambient traffic)."""
    if not _truthy(_env("ACLAW_APPLOGIN_PROBE", "0")):
        return {"enabled": False}
    now = time.time()
    if (not force and _probe_cache["result"] is not None
            and now - _probe_cache["ts"] < _PROBE_TTL_S):
        return _probe_cache["result"]
    result = {"enabled": True, "checked_at": round(now, 3), "routes": {}}
    try:
        import requests as _rq
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        for route in ("/userapi/v1/app-login", "/userapi/v1/oneclick-login"):
            url = "https://autoglm-api.autoglm.ai" + route
            try:
                r = _rq.post(url, json={}, timeout=8, verify=False)
                # 200+sign-error envelope -> route exists; 405 -> gone
                body = {}
                try:
                    body = r.json()
                except Exception:
                    pass
                code = body.get("code")
                exists = bool(code == 400002)
                result["routes"][route] = {
                    "http_status": r.status_code, "upstream_code": code,
                    "reachable": exists,
                }
            except Exception as exc:
                result["routes"][route] = {"error": str(exc)[:120]}
    except Exception as exc:
        result["error"] = str(exc)[:120]
    _probe_cache["ts"] = now
    _probe_cache["result"] = result
    return result


def stats():
    """/health token_import block. Never raises."""
    try:
        with _LOCK:
            return {
                "mode": ("no-browser" if no_browser()
                         else f"oauth:{oauth_mode()}"),
                "import_dir": _env("ACLAW_IMPORT_DIR", "") or None,
                "oauth_upstream": _STATS["oauth_upstream"],
                "records_seen": _STATS["records_seen"],
                "files_seen": _STATS["files_seen"],
                "imported": _STATS["imported"],
                "updated": _STATS["updated"],
                "rejected": _STATS["rejected"],
                "last_import_ts": _STATS["last_import_ts"],
                "last_error": _STATS["last_error"],
            }
    except Exception as exc:  # pragma: no cover — defensive
        return {"error": str(exc)}


def reset():
    """Clear counters (offline tests only)."""
    with _LOCK:
        for k in _STATS:
            if k == "oauth_upstream":
                _STATS[k] = "unknown"
            elif k == "last_import_ts":
                _STATS[k] = None
            elif k == "last_error":
                _STATS[k] = None
            else:
                _STATS[k] = 0
