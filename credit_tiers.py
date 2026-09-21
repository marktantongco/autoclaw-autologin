"""Claude credit-tier routing (Phase-2 Synergy 7).

Maps Claude model aliases to AutoClaw credit tiers so Anthropic clients
(Claude Code) transparently route to the right GLM model class:

    claude-opus-*   -> High tier   (openrouter_glm-5.2)
    claude-sonnet-* -> Medium tier (zai_auto)
    claude-haiku-*  -> Low tier    (zai_glm-5-turbo)

Tier targets are refreshed in the background from AutoClaw's remote
model-config endpoint; if unreachable, heuristic defaults are used
(static alias map below).

Synergy: eequaled/GLM_proxy lib/core.js (fetchRemoteModelConfig,
annotateCreditTiers, resolveTierTargets) — reimplemented in Python.
"""

import os
import json
import time
import logging
import threading

logger = logging.getLogger("autoclaw.credit_tiers")

# ─── Configuration ───────────────────────────────────────────────────

MODEL_CONFIG_URL = os.environ.get(
    "AUTOCLAW_MODEL_CONFIG_URL",
    "https://autoglm-api.autoglm.ai/autoclaw-proxy/proxy/autoclaw-model-config",
)
TIER_REFRESH_INTERVAL = int(os.environ.get("AUTOCLAW_TIER_REFRESH_S", "300"))  # 5 min

# ─── Static heuristic fallback (used until/unless remote config lands) ──

HEURISTIC_TIERS = {
    "high": "openrouter_glm-5.2",     # Best quality
    "medium": "zai_auto",             # Balanced
    "low": "zai_glm-5-turbo",         # Cheapest, always available
}

# Claude alias prefix -> tier
CLAUDE_ALIAS_TIERS = [
    ("claude-opus", "high"),
    ("claude-sonnet", "medium"),
    ("claude-haiku", "low"),
]


class CreditTierResolver:
    """Background-refreshed Claude alias -> upstream model resolver."""

    def __init__(self):
        self._tiers = dict(HEURISTIC_TIERS)  # tier_name -> upstream model
        self._last_refresh = 0.0
        self._lock = threading.Lock()
        self._refresh_thread = None
        self._source = "heuristic"

    # ── Public API ────────────────────────────────────────────────

    def resolve(self, client_model: str):
        """Resolve a client model name to an upstream model.

        Returns (upstream_model, is_claude_alias).
        Non-Claude models return (client_model, False) — caller falls
        through to the normal MODEL_MAP lookup.
        """
        lower = client_model.lower()
        for prefix, tier in CLAUDE_ALIAS_TIERS:
            if lower.startswith(prefix) or lower == prefix.split("-", 1)[0] + "-" + prefix.split("-", 1)[1]:
                with self._lock:
                    upstream = self._tiers.get(tier, HEURISTIC_TIERS[tier])
                return upstream, True
        return client_model, False

    def get_tiers(self) -> dict:
        """Current tier mapping (for /api/dashboard/overview)."""
        with self._lock:
            return {
                "tiers": dict(self._tiers),
                "source": self._source,
                "last_refresh": self._last_refresh,
            }

    def force_refresh(self):
        """Synchronous refresh (used by !router and dashboard)."""
        self._refresh_once()

    def start_background_refresh(self):
        """Start the daemon refresh thread (idempotent)."""
        if self._refresh_thread and self._refresh_thread.is_alive():
            return
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, daemon=True, name="credit-tier-refresh"
        )
        self._refresh_thread.start()

    # ── Internals ─────────────────────────────────────────────────

    def _refresh_loop(self):
        # Initial refresh shortly after startup
        time.sleep(3)
        self._refresh_once()
        while True:
            time.sleep(TIER_REFRESH_INTERVAL)
            self._refresh_once()

    def _refresh_once(self):
        """Fetch remote model-config; update tiers; degrade gracefully."""
        try:
            import requests
            from config import UPSTREAM_UA
            resp = requests.get(MODEL_CONFIG_URL, timeout=10, verify=False,
                                headers={"User-Agent": UPSTREAM_UA})
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            data = resp.json()
            tiers = self._extract_tiers(data)
            if not tiers:
                raise RuntimeError("no tiers in response")
            with self._lock:
                self._tiers.update(tiers)
                self._source = "remote"
                self._last_refresh = time.time()
            logger.info(f"Credit tiers refreshed (remote): {self._tiers}")
        except Exception as e:
            # Heuristic degradation — keep serving with current/defaults
            with self._lock:
                if self._source != "remote":
                    self._source = "heuristic"
                self._last_refresh = time.time()
            logger.debug(f"Credit-tier refresh degraded to heuristic: {e}")

    @staticmethod
    def _normalize_level(val) -> str:
        """Normalize a creditConsumptionLevel value to high|medium|low|''.

        Live-verified upstream vocabulary (probed 2026-09-21): "Low",
        "High", "中" (Medium). Defensive: also accept 高/低, full-width,
        any case, and surrounding whitespace.
        """
        if not isinstance(val, str):
            return ""
        v = val.strip().lower()
        if v in ("low", "低"):
            return "low"
        if v in ("high", "高"):
            return "high"
        if v in ("medium", "med", "mid", "中", "中等"):
            return "medium"
        return ""

    @classmethod
    def _extract_tiers(cls, data) -> dict:
        """Extract {high, medium, low} model names from remote config.

        The remote config shape is not strictly guaranteed; try common
        layouts and fall back to empty (heuristic continues to serve).

        Layout 2 (live-verified 2026-09-21): {"models": [{"id",
        "creditConsumptionLevel": "Low"|"High"|"中", ...}]}. The
        original implementation read m['credits']/m['tier'] — fields the
        real payload does not carry — so remote extraction silently
        degraded to heuristic forever. Fixed to read the real field.
        """
        tiers = {}
        if not isinstance(data, dict):
            return tiers

        # Layout 1: {"data": {"tiers": {"high": "...", ...}}}
        candidate = data.get("data", data)
        if isinstance(candidate, dict):
            tier_block = candidate.get("tiers", candidate)
            if isinstance(tier_block, dict):
                for tier in ("high", "medium", "low"):
                    val = tier_block.get(tier)
                    if isinstance(val, str) and val:
                        tiers[tier] = val
                    elif isinstance(val, dict) and val.get("model"):
                        tiers[tier] = val["model"]

        # Layout 2: model list with credit annotations.
        # Primary signal: creditConsumptionLevel (live-verified field).
        # Fallback signals: credits / tier fields, then name heuristics.
        models = data.get("models") or (candidate.get("models") if isinstance(candidate, dict) else None)
        if isinstance(models, list) and not tiers:
            high_picks, med_picks, low_picks = [], [], []
            for m in models:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id") or m.get("model") or ""
                if not mid:
                    continue
                level = cls._normalize_level(m.get("creditConsumptionLevel"))
                if not level:
                    level = cls._normalize_level(m.get("credits") or m.get("tier"))
                if not level:
                    # Legacy name-based classification (pre-config payloads)
                    ml = mid.lower()
                    if "opus" in ml:
                        level = "high"
                    elif "turbo" in ml or "haiku" in ml:
                        level = "low"
                    else:
                        level = "medium"
                if level == "high":
                    high_picks.append(mid)
                elif level == "low":
                    low_picks.append(mid)
                else:
                    med_picks.append(mid)
            if high_picks:
                tiers["high"] = high_picks[0]
            if med_picks:
                tiers["medium"] = med_picks[0]
            if low_picks:
                tiers["low"] = low_picks[0]

        return tiers


# Singleton used by proxy.py
_resolver = CreditTierResolver()


def resolve_claude_alias(client_model: str):
    """Module-level convenience: resolve Claude alias -> upstream model.

    Returns (upstream_model, is_claude_alias).
    """
    return _resolver.resolve(client_model)


def get_tier_status() -> dict:
    """Tier status for dashboards / !router."""
    return _resolver.get_tiers()


def start_background_refresh():
    """Start the background tier refresher (called at app startup)."""
    _resolver.start_background_refresh()


def force_refresh():
    """Force a synchronous tier refresh."""
    _resolver.force_refresh()


def reset():
    """Reset the singleton to heuristic defaults (test isolation)."""
    global _resolver
    _resolver = CreditTierResolver()
