"""AutoClaw Auth — Token management, refresh, validation"""

import time
import json
import hashlib
import uuid
import threading
import logging
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# Synergy 6: OWL-AGENT proxy defense layer (owl-agent v5.3, hybrid backend)
import owl_bridge
from config import (
    APP_ID, APP_KEY, PRODUCT, VERSION, PLATFORM,
    USER_API_BASE, GOOGLE_OAUTH_URL, GOOGLE_OAUTH_LOGIN,
    REFRESH_URL, PROFILE_URL, WALLET_URL, LEDGER_URL,
    TOKENS_FILE, ACCESS_TOKEN_TTL, REFRESH_MARGIN,
    PROXY_LIST,
)

# ─── Structured Logging ──────────────────────────────────────────────
logger = logging.getLogger("autoclaw.auth")

# ─── Token Encryption (MEDIUM item: encrypt at rest) ─────────────────
try:
    from token_encryption import encrypt_tokens, decrypt_tokens, is_encryption_enabled
except ImportError:
    # Fallback if token_encryption.py not available
    def encrypt_tokens(data):
        return json.dumps(data, indent=2).encode('utf-8')
    def decrypt_tokens(raw):
        try:
            return json.loads(raw)
        except Exception:
            return {"accounts": []}
    def is_encryption_enabled():
        return False

_lock = threading.Lock()
_proxy_counter = 0

# ─── Memory Fix M-A1: In-memory token cache with TTL ───
# Avoids repeated JSON file reads on every request (12+ call sites in proxy.py).
_token_cache = None       # cached result of load_tokens()
_token_cache_ts = 0.0     # timestamp of cache fill
_token_cache_ttl = 5.0    # seconds before re-reading from disk

def _next_proxy():
    """Get next proxy in round-robin. Returns dict or None."""
    global _proxy_counter
    if not PROXY_LIST:
        return None
    proxy = PROXY_LIST[_proxy_counter % len(PROXY_LIST)]
    _proxy_counter += 1
    return proxy

def _proxies(proxy):
    """Convert proxy dict to requests format. Returns None if no proxy."""
    if not proxy:
        return None
    url = proxy["server"]
    if "username" in proxy:
        # Insert auth into URL
        url = url.replace("http://", f"http://{proxy['username']}:{proxy['password']}@")
    return {"http": url, "https": url}


def _sign_headers():
    """Generate forgeable app-signing headers."""
    ts = str(int(time.time()))
    sign = hashlib.md5(f"{APP_ID}&{ts}&{APP_KEY}".encode()).hexdigest()
    return {
        "X-Auth-Appid": APP_ID,
        "X-Auth-TimeStamp": ts,
        "X-Auth-Sign": sign,
        "X-Product": PRODUCT,
        "X-Version": VERSION,
        "X-Tm": PLATFORM,
        "X-Trace-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def _upstream_request(method, url, *, headers=None, json=None, params=None,
                      timeout=15, proxies=None):
    """Synergy 6: route upstream calls through the OWL proxy-defense layer
    (owl-agent v5.3) when enabled; transparent direct fallback.

    Explicit `proxies` (proxies.txt round-robin used by registration flows)
    always win — dedicated paid proxies outrank the free OWL pool; OWL only
    engages when no explicit proxy is available (proxies is None).

    Returns a requests-compatible response object (OwlResponse shim when
    routed via OWL, requests.Response otherwise).
    """
    if proxies is None and owl_bridge.owl_enabled():
        try:
            return owl_bridge.owl_request(
                method, url, headers=headers, json_body=json, timeout=timeout)
        except owl_bridge.OwlUnavailable as e:
            logger.debug(f"OWL unavailable → direct: {e}")
    return requests.request(method, url, headers=headers, json=json,
                            params=params, timeout=timeout, verify=False,
                            proxies=proxies)


def load_tokens():
    """Load tokens from file with encryption support and in-memory TTL cache (M-A1).

    Supports both encrypted (Fernet) and plaintext (JSON) formats for backwards
    compatibility. Caches the parsed data for _token_cache_ttl seconds.
    """
    global _token_cache, _token_cache_ts
    now = time.time()
    if _token_cache is not None and (now - _token_cache_ts) < _token_cache_ttl:
        return _token_cache
    try:
        with open(TOKENS_FILE, "rb") as f:
            raw = f.read()
        data = decrypt_tokens(raw)
    except FileNotFoundError:
        data = {"accounts": []}
    except Exception as e:
        logger.error(f"Failed to load tokens: {e}")
        data = {"accounts": []}
    with _lock:
        _token_cache = data
        _token_cache_ts = now
    return data


def save_tokens(data):
    """Save tokens to file (thread-safe, atomic, with wipe guard + encryption).
    Refuses to write if new account count < 50% of existing — prevents
    accidental wipe from race conditions or corrupted reads.
    When AUTOCLAW_TOKEN_KEY is set, writes Fernet-encrypted bytes."""
    with _lock:
        # Guard: refuse to write if we'd be wiping >50% of accounts
        try:
            with open(TOKENS_FILE, "rb") as f:
                existing_raw = f.read()
            existing = decrypt_tokens(existing_raw)
            existing_count = len(existing.get("accounts", []))
            new_count = len(data.get("accounts", []))
            if existing_count > 0 and new_count < existing_count * 0.5:
                logger.warning(f"BLOCKED save_tokens: {new_count} accounts would replace {existing_count} (wipe guard)")
                return False
        except FileNotFoundError:
            pass  # No existing file — allow write
        except Exception:
            pass  # Corrupt file — allow overwrite
        # Atomic write: write to temp file then rename
        import os
        tmp = TOKENS_FILE + ".tmp"
        encrypted = encrypt_tokens(data)
        mode = "wb" if isinstance(encrypted, bytes) else "w"
        with open(tmp, mode) as f:
            f.write(encrypted)
        os.replace(tmp, TOKENS_FILE)
        # Security: restrict file permissions to owner-only
        try:
            os.chmod(TOKENS_FILE, 0o600)
        except OSError:
            pass
        # Memory Fix M-A1: Invalidate cache on write so next load_tokens() re-reads
        global _token_cache, _token_cache_ts
        _token_cache = data  # Pre-fill cache with the data we just wrote
        _token_cache_ts = time.time()
        enc_status = "encrypted" if is_encryption_enabled() else "plaintext"
        logger.debug(f"Saved tokens ({enc_status}): {len(data.get('accounts', []))} accounts")
        return True


def add_token(email, access_token, refresh_token, user_id, device_id, source_id="autoclaw"):
    """Add or update a token entry."""
    data = load_tokens()
    # Remove existing entry for same email
    data["accounts"] = [a for a in data["accounts"] if a.get("email") != email]
    data["accounts"].append({
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_id": user_id,
        "device_id": device_id,
        "source_id": source_id,
        "added_at": int(time.time()),
        "last_refreshed": int(time.time()),
    })
    save_tokens(data)
    logger.info(f"Token saved: {email}")


def get_valid_token():
    """Get a valid (non-expired) access token. Refresh if needed.
    Returns: (access_token, account_dict) or (None, None)
    """
    data = load_tokens()
    if not data["accounts"]:
        return None, None

    for acc in data["accounts"]:
        # Check if token needs refresh (older than TTL - margin)
        last_refreshed = acc.get("last_refreshed", 0)
        age = time.time() - last_refreshed
        if age < ACCESS_TOKEN_TTL - REFRESH_MARGIN:
            return acc["access_token"], acc

        # Try refresh
        new_token = refresh_token(acc)
        if new_token:
            return new_token, acc

    return None, None


def get_valid_token_for_email(email):
    """Get valid token for specific email."""
    data = load_tokens()
    for acc in data["accounts"]:
        if acc.get("email") == email:
            last_refreshed = acc.get("last_refreshed", 0)
            age = time.time() - last_refreshed
            if age < ACCESS_TOKEN_TTL - REFRESH_MARGIN:
                return acc["access_token"], acc
            new_token = refresh_token(acc)
            if new_token:
                return new_token, acc
    return None, None


def refresh_token(account):
    """Refresh an access token using refresh_token.
    Returns: new access_token or None
    """
    try:
        headers = _sign_headers()
        body = {
            "source_id": account.get("source_id", "autoclaw"),
            "device_id": account["device_id"],
            "refresh_token": account["refresh_token"],
        }
        resp = _upstream_request("POST", REFRESH_URL, headers=headers, json=body, timeout=15)
        data = resp.json()

        if data.get("code") == 0 and "data" in data:
            new_access = data["data"].get("access_token")
            new_refresh = data["data"].get("refresh_token", account["refresh_token"])

            if new_access:
                # Update stored token
                all_data = load_tokens()
                for a in all_data["accounts"]:
                    if a.get("email") == account["email"]:
                        a["access_token"] = new_access
                        if new_refresh:
                            a["refresh_token"] = new_refresh
                        a["last_refreshed"] = int(time.time())
                        break
                save_tokens(all_data)
                logger.info(f"Refreshed token: {account['email']}")
                return new_access
        logger.warning(f"Refresh failed for {account['email']}: {data}")
        return None
    except Exception as e:
        logger.error(f"Refresh error for {account['email']}: {e}")
        return None


def refresh_all():
    """Refresh all tokens. Returns count of success/fail.
    Collects all updates in-memory, saves ONCE at end (not per-account).
    This prevents race conditions that can wipe tokens.json."""
    data = load_tokens()
    success = 0
    fail = 0
    now = int(time.time())
    for acc in data["accounts"]:
        # Build refresh request from current account data
        try:
            headers = _sign_headers()
            body = {
                "source_id": acc.get("source_id", "autoclaw"),
                "device_id": acc["device_id"],
                "refresh_token": acc["refresh_token"],
            }
            resp = _upstream_request("POST", REFRESH_URL, headers=headers, json=body, timeout=15)
            resp_data = resp.json()

            if resp_data.get("code") == 0 and "data" in resp_data:
                new_access = resp_data["data"].get("access_token")
                new_refresh = resp_data["data"].get("refresh_token", acc["refresh_token"])
                if new_access:
                    acc["access_token"] = new_access
                    if new_refresh:
                        acc["refresh_token"] = new_refresh
                    acc["last_refreshed"] = now
                    success += 1
                    logger.info(f"Refreshed: {acc['email']}")
                else:
                    fail += 1
                    logger.warning(f"Refresh failed (no access_token): {acc['email']}")
            else:
                fail += 1
                logger.warning(f"Refresh failed: {acc['email']}: {resp_data}")
        except Exception as e:
            fail += 1
            logger.error(f"Refresh error for {acc['email']}: {e}")

    # Save ONCE at the end (not per-account) — atomic, with wipe guard
    save_tokens(data)
    logger.info(f"Refresh all: {success} ok, {fail} fail")
    return success, fail


def check_profile(access_token):
    """Verify token validity via user-profile endpoint."""
    headers = _sign_headers()
    raw = access_token.replace("Bearer ", "")
    headers["X-Authorization"] = f"Bearer {raw}"
    try:
        resp = _upstream_request("POST", PROFILE_URL, headers=headers, json={}, timeout=15)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def check_wallet(access_token):
    """Check wallet balance (reward points)."""
    headers = _sign_headers()
    raw = access_token.replace("Bearer ", "")
    headers["authorization"] = f"Bearer {raw}"  # lowercase for assetmgr!
    try:
        resp = _upstream_request("GET", WALLET_URL, headers=headers, timeout=15)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def check_ledger(access_token):
    """Check billing ledger."""
    headers = _sign_headers()
    raw = access_token.replace("Bearer ", "")
    headers["authorization"] = f"Bearer {raw}"
    try:
        resp = _upstream_request("GET", LEDGER_URL, headers=headers, timeout=15)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def google_oauth_url(device_id=None, navigate_uri="http://localhost:18432/auth/callback-google",
                     max_retries=5, retry_delay=3, proxy=None):
    """Step 1: Get Google OAuth URL.
    Retries on 400005 rate limit (shared APP_ID 100003 — all users compete for same quota).
    Uses rotating proxy to bypass 630014 IP rate limit.
    Returns: (oauth_url, state, device_id, err_info)
      err_info = None on success, or {"code":N, "msg":"...", "retried":N} on failure.
    """
    headers = _sign_headers()
    if not device_id:
        device_id = str(uuid.uuid4())
    body = {
        "source_id": "autoclaw",
        "device_id": device_id,
        "navigate_uri": navigate_uri,
    }
    px = _proxies(proxy) if proxy else _proxies(_next_proxy())
    import time as _time
    retries_done = 0
    last_code = None
    last_msg = ""
    for attempt in range(max_retries):
        retries_done = attempt + 1
        resp = _upstream_request("POST", GOOGLE_OAUTH_URL, headers=headers, json=body, timeout=15, proxies=px)
        try:
            data = resp.json()
        except Exception:
            logger.warning(f"google_oauth_url bad response: HTTP {resp.status_code}, body={resp.text[:200]}")
            last_code = resp.status_code
            last_msg = f"Bad HTTP response (HTTP {resp.status_code})"
            if attempt < max_retries - 1:
                _time.sleep(retry_delay)
                continue
            return None, None, device_id, {"code": last_code, "msg": last_msg, "retried": retries_done}

        if data.get("code") == 0:
            return data["data"]["oauth_url"], data["data"]["state"], device_id, None

        code = data.get("code")
        msg = data.get("msg", "")
        last_code = code
        last_msg = msg
        if code == 400005:
            # Rate limit — shared APP_ID, all users compete. Retry with delay.
            logger.info(f"google_oauth_url rate-limited (400005), retry {attempt+1}/{max_retries} in {retry_delay}s...")
            if attempt < max_retries - 1:
                _time.sleep(retry_delay)
                continue
        if code == 630014:
            # IP rate limit — try switching proxy
            logger.info(f"google_oauth_url IP rate-limited (630014) via proxy, retry {attempt+1}/{max_retries}...")
            px = _proxies(_next_proxy())
            if attempt < max_retries - 1:
                _time.sleep(retry_delay)
                continue
        # Other errors — don't retry
        logger.error(f"google_oauth_url failed: code={code}, msg={msg}")
        return None, None, device_id, {"code": code, "msg": msg, "retried": retries_done}
    return None, None, device_id, {"code": last_code, "msg": last_msg, "retried": retries_done}


def google_oauth_login(code, state, device_id, navigate_uri="http://localhost:18432/auth/callback-google", proxy=None):
    """Step 2: Exchange Google OAuth code for AutoClaw tokens.
    Retries on 630014 (IP rate limit) by switching proxy.
    Reuses same proxy from URL generation if provided.
    """
    headers = _sign_headers()
    body = {
        "code": code,
        "state": state,
        "navigate_uri": navigate_uri,
        "device_id": device_id,
        "source_id": "autoclaw",
    }
    # Use provided proxy (same as URL gen), or get next from round-robin
    if proxy:
        px = _proxies(proxy)
    else:
        px = _proxies(_next_proxy())

    # Retry on 630014 (IP rate limit) — try switching proxy
    import time as _time
    for attempt in range(3):
        resp = _upstream_request("POST", GOOGLE_OAUTH_LOGIN, headers=headers, json=body, timeout=15, proxies=px)
        try:
            data = resp.json()
        except Exception:
            logger.warning(f"OAuth login bad response: HTTP {resp.status_code}, body={resp.text[:200]}")
            if attempt < 2:
                _time.sleep(1)
                # Switch proxy for retry
                px = _proxies(_next_proxy())
                continue
            return None

        if data.get("code") == 0 and "data" in data:
            d = data["data"]
            return {
                "access_token": d.get("access_token"),
                "refresh_token": d.get("refresh_token"),
                "user_id": d.get("user_id"),
                "user_name": d.get("user_name"),
                "first_login": d.get("first_login"),
                "device_id": device_id,
            }

        code_val = data.get("code")
        msg = data.get("msg", "")
        logger.error(f"OAuth login failed: HTTP {resp.status_code}, code={code_val}, msg={msg}, full={data}")

        if code_val == 630014 and attempt < 2:
            # IP rate limit — switch proxy and retry
            logger.info(f"OAuth login IP rate-limited (630014), retry {attempt+1}/3 with different proxy...")
            px = _proxies(_next_proxy())
            _time.sleep(1)
            continue
        # Non-retryable error (400001, 631001, etc.) — fail immediately
        return None

    return None


def list_accounts():
    """Print all stored accounts."""
    data = load_tokens()
    logger.info(f"AutoClaw Accounts: {len(data['accounts'])}")
    for i, acc in enumerate(data["accounts"]):
        age = time.time() - acc.get("last_refreshed", 0)
        hours_left = max(0, (ACCESS_TOKEN_TTL - age) / 3600)
        logger.info(f"  {i+1}. {acc['email']} | user={acc.get('user_id','?')} | "
              f"token_age={age/3600:.1f}h | expires_in={hours_left:.1f}h")
    if not data["accounts"]:
        logger.info("  (empty — add token first)")
