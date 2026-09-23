"""Permanent Failure Negative Cache — Synergy: GLM_proxy.

Caches (model, account) pairs that have failed permanently (e.g. quota
exhausted, model not found, auth banned) for a short TTL to avoid
replaying doomed 30s+ cloud sequences on every subsequent request.

Without this cache, a single exhausted-quota account can burn 30+ seconds
on each retry attempt (full HTTPS handshake, JWT refresh, full chat
completion round-trip) before the failure surfaces. With the cache, the
second and subsequent requests return 429 immediately for 60 seconds,
giving the operator time to top up the account.

Source: eequaled/GLM_proxy lib/core.js (createPermanentFailureCache)
"""

import time
import threading


class PermanentFailureCache:
    """60s TTL negative cache for permanent upstream failures.

    Prevents replaying doomed cloud sequences when an upstream account is
    quota-exhausted or the model is unknown. Each entry caches a
    failure_class string for a configurable TTL (default 60s).

    Synergy: eequaled/GLM_proxy lib/core.js (createPermanentFailureCache)

    Thread-safety: all read/write operations are guarded by an internal
    threading.Lock — safe to share across Flask worker threads.
    """

    def __init__(self, ttl: int = 60):
        """Initialize cache.

        Args:
            ttl: Seconds to cache a permanent failure (default 60s). 60s
                is long enough to absorb a transient outage but short
                enough that the system self-heals after a token refresh
                or top-up.
        """
        self._cache = {}  # key -> (failure_class: str, expiry_ts: float)
        self._ttl = ttl
        self._lock = threading.Lock()

    def check(self, key: str):
        """Check if `key` has a cached permanent failure.

        Args:
            key: Cache key, typically "model:account_email".

        Returns:
            failure_class string if the entry exists AND is still within
            TTL, None otherwise. Expired entries are evicted on access
            (lazy expiration).
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() < entry[1]:
                return entry[0]
            if entry:
                # Expired — remove to keep dict bounded
                del self._cache[key]
            return None

    def mark(self, key: str, failure_class: str):
        """Cache a permanent failure for `ttl` seconds.

        Args:
            key: Cache key, typically "model:account_email".
            failure_class: Short string classifying the failure
                (e.g. "quota_exhausted", "model_not_found",
                "auth_failed"). Used in logs and surfaced to client in
                the 429 response body for debuggability.
        """
        with self._lock:
            self._cache[key] = (failure_class, time.time() + self._ttl)

    def clear(self):
        """Clear all cached failures.

        Called after a successful token refresh or manual operator
        intervention (e.g. !router refresh-all) so the system immediately
        re-attempts the upstream rather than waiting up to 60s for TTL.
        """
        with self._lock:
            self._cache.clear()


# Singleton — shared across all Flask worker threads. Every chat
# completion request consults this cache before forwarding upstream.
_permanent_failure_cache = PermanentFailureCache()


def is_permanent_failure_cached(model: str, account_email: str = None) -> bool:
    """Check if (model, account) has a cached permanent failure.

    Synergy: eequaled/GLM_proxy lib/core.js

    Args:
        model: Upstream model name (e.g. "openrouter_glm-5.2",
            "zai_glm-5-turbo"). Use the *upstream* name (from
            MODEL_MAP.values()), not the client-facing alias.
        account_email: Account email from tokens.json. If None, the
            cache key falls back to "any" — useful for model-level
            failures (e.g. unknown model) that affect all accounts.

    Returns:
        True if a cached permanent failure exists for this (model,
        account) pair and is still within TTL. False otherwise.
    """
    key = f"{model}:{account_email or 'any'}"
    return _permanent_failure_cache.check(key) is not None


def mark_permanent_failure(model: str, failure_class: str, account_email: str = None):
    """Mark a (model, account) as permanently failed for 60s.

    Synergy: eequaled/GLM_proxy lib/core.js

    Args:
        model: Upstream model name (e.g. "openrouter_glm-5.2").
        failure_class: Short classification string — see
            PermanentFailureCache.mark() for examples.
        account_email: Account email, or None for "any account".
    """
    key = f"{model}:{account_email or 'any'}"
    _permanent_failure_cache.mark(key, failure_class)


def clear_permanent_failures():
    """Clear all cached permanent failures.

    Called by the !router refresh-all command after a successful token
    refresh so the proxy immediately re-attempts the upstream rather
    than waiting for the 60s TTL to expire.
    """
    _permanent_failure_cache.clear()
