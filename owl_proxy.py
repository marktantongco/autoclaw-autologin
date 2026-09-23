#!/usr/bin/env python3
"""
🦉 OWL-AGENT PROXY DEFENSE STACK v5.3-autoclaw (vendored, adapted)

Source: OWL-AGENT v5.3 unified installer (proxy_defense.py), integrated into
AutoClaw as the upstream network-resilience layer (Synergy: owl-agent v5.3).

Adaptations for AutoClaw (vs. upstream v5.3):
  1. Header-aware cache/dedup keys  — upstream keys omitted auth headers,
     which would collide across accounts. Keys now hash the full header map,
     and only GET requests are cached/deduplicated (POSTs always execute —
     token refreshes are state-changing and must never be coalesced).
  2. Streaming support — `stream_request()` races HEDGE_FANOUT proxies for
     *connection establishment* (time-to-headers) and then hands back a live
     stream reader, so SSE chat passthrough works through the proxy layer.
  3. Every constant is env-tunable (OWL_*) — no code edits needed to tune.
  4. Library-first: importable module (`from owl_proxy import ResilientClient`)
     while keeping the standalone CLI (`python owl_proxy.py stats`).
  5. aiofiles dropped — file IO goes through asyncio.to_thread (one less dep).
  6. Validation/seeding endpoints env-tunable, default gstatic 204 + proxifly.

Pipeline: Discovery → Dedup → Validation → Scoring → Pool → Health Monitor
- Multi-dimensional scoring (latency, reliability, throughput, stability, age)
- Hedged parallel racing: HEDGE_FANOUT proxies at PROXY_TIMEOUT each
- Single-strike ban (idempotent) + DIRECT FALLBACK
- SOCKS4/5 support via aiohttp-socks (optional)
- TLS impersonation via curl_cffi (optional, OWL_TLS_IMPERSONATE)
- Python 3.10–3.14 compatible; all optional imports guarded
"""

import asyncio
import hashlib
import json
import time
import logging
import os
import ssl  # noqa: F401  (kept for parity with upstream v5.3 / user ssl contexts)
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Set, Tuple, AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

# ─── Core deps ────────────────────────────────────────────────
import aiohttp
import httpx

# ─── Optional deps (catch EVERYTHING, incl. RuntimeError at import) ──
def _try_import(name):
    try:
        return __import__(name, fromlist=["*"])
    except Exception:
        return None

_aiohttp_socks = _try_import("aiohttp_socks")
if _aiohttp_socks is not None and hasattr(_aiohttp_socks, "ProxyConnector"):
    _SocksConnector = _aiohttp_socks.ProxyConnector
    AIOHTTP_SOCKS_AVAILABLE = True
else:
    _SocksConnector = None
    AIOHTTP_SOCKS_AVAILABLE = False

_psutil = _try_import("psutil")
PSUTIL_AVAILABLE = _psutil is not None

_curl = _try_import("curl_cffi.requests")
if _curl is not None and hasattr(_curl, "AsyncSession"):
    CurlAsyncSession = _curl.AsyncSession
    CURL_CFFI_AVAILABLE = True
else:
    CurlAsyncSession = None
    CURL_CFFI_AVAILABLE = False

REDIS_AVAILABLE = _try_import("redis.asyncio") is not None
SKLEARN_AVAILABLE = (_try_import("sklearn.linear_model") is not None
                     and _try_import("numpy") is not None)

# ─── Paths (env-overridable base dir; shared with external ~/.owl-agent) ──
BASE             = Path(os.environ.get("OWL_BASE_DIR", str(Path.home() / ".owl-agent")))
CACHE_DIR        = BASE / "cache" / "http"
CONFIG_DIR       = BASE / "config"
PROXY_CACHE_FILE = CONFIG_DIR / "proxy_cache.json"
PROXY_POOL_FILE  = CONFIG_DIR / "proxy_pool.json"
MODEL_DIR        = BASE / "models"
PLUGIN_DIR       = BASE / "plugins"

# ─── Constants (env-tunable) ──────────────────────────────────
def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

DEFAULT_TTL        = _env_int("OWL_CACHE_TTL", 300)
DEFAULT_RATE       = _env_float("OWL_BASE_RATE", 1.0)
MAX_PROXY_CACHE    = _env_int("OWL_MAX_PROXY_CACHE", 100)
DEFAULT_COUNTRIES  = ["US", "GB", "DE", "FR", "CA"]
QUALITY_DECAY      = _env_float("OWL_QUALITY_DECAY", 0.9)
ADAPTIVE_MIN_RATE  = _env_float("OWL_MIN_RATE", 0.1)
ADAPTIVE_MAX_RATE  = _env_float("OWL_MAX_RATE", 5.0)
PROXY_BAN_SECONDS  = _env_int("OWL_PROXY_BAN_SECONDS", 60)
PROXY_TIMEOUT      = _env_float("OWL_PROXY_TIMEOUT", 6)       # per-proxy attempt timeout (s)
DIRECT_TIMEOUT     = _env_float("OWL_DIRECT_TIMEOUT", 30)     # direct fallback connect timeout (s)
HEDGE_FANOUT       = max(1, _env_int("OWL_HEDGE_FANOUT", 3))  # proxies raced in parallel
SEED_URL           = os.environ.get(
    "OWL_SEED_URL",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json")
SEED_COUNT         = _env_int("OWL_SEED_COUNT", 100)
VALIDATE_URL       = os.environ.get("OWL_VALIDATE_URL", "https://www.gstatic.com/generate_204")
VALIDATE_STATUSES  = (200, 204)
BAD_PROXY_STATUSES = (407, 502, 503, 504)
TLS_IMPERSONATE    = os.environ.get("OWL_TLS_IMPERSONATE", "")  # e.g. chrome110

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger("autoclaw.owl")

VERSION = "5.3-autoclaw"

# ─── Enums & data ─────────────────────────────────────────────

class ProxyHealth(Enum):
    NEW = auto(); TESTING = auto(); HEALTHY = auto()
    DEGRADED = auto(); FAILED = auto(); RETEST = auto()


class ProxyEntry:
    __slots__ = ('url', 'protocol', 'country', 'state', 'health', 'last_check',
                 'fail_count', 'ban_until', 'success_count', 'avg_latency',
                 'tcp_latency', 'tls_latency', 'http_latency', 'throughput',
                 'stability', 'success_rate', 'uptime', 'last_validated', 'score',
                 '_recent_checks', 'source')

    def __init__(self, url, protocol="http", country=None, source="unknown"):
        self.url = url
        self.protocol = protocol
        self.country = country
        self.source = source
        self.state = ProxyHealth.NEW
        self.health = True
        self.last_check = 0.0
        self.fail_count = 0
        self.ban_until = 0.0
        self.success_count = 0
        self.avg_latency = 500.0
        self.tcp_latency = 0.0
        self.tls_latency = 0.0
        self.http_latency = 0.0
        self.throughput = 0.0
        self.stability = 1.0
        self.success_rate = 0.5
        self.uptime = 0.0
        self.last_validated = 0.0
        self.score = 0.0
        self._recent_checks = []

    def is_banned(self):
        return time.time() < self.ban_until

    def mark_failed(self):
        now = time.time()
        if now < self.ban_until:
            return  # idempotent: already banned
        self.fail_count += 1
        ban_secs = PROXY_BAN_SECONDS * min(self.fail_count, 10)
        self.ban_until = now + ban_secs
        self.health = False
        self.state = ProxyHealth.FAILED
        logger.warning(f"Proxy banned ({ban_secs}s): {self.url}")
        self._update_score()

    def update_check(self, success, latency, tcp=0, tls=0, http=0, throughput=0):
        self.last_check = time.time()
        self._recent_checks.append(success)
        if len(self._recent_checks) > 20:
            self._recent_checks.pop(0)
        if success:
            self.success_count += 1
            self.fail_count = 0
            self.avg_latency = self.avg_latency * 0.8 + latency * 0.2
            if tcp:        self.tcp_latency = self.tcp_latency * 0.8 + tcp * 0.2
            if tls:        self.tls_latency = self.tls_latency * 0.8 + tls * 0.2
            if http:       self.http_latency = self.http_latency * 0.8 + http * 0.2
            if throughput: self.throughput = self.throughput * 0.8 + throughput * 0.2
            self.health = True
            self.state = ProxyHealth.HEALTHY
        else:
            self.mark_failed()
        self._update_score()

    def _update_score(self):
        latency_score    = max(0, 1 - (self.avg_latency / 2000))
        success_rate     = (sum(self._recent_checks) / len(self._recent_checks)
                            if self._recent_checks else 0.5)
        throughput_score = min(1, self.throughput / 1000)
        age_score        = (min(1, (time.time() - self.last_validated) / 3600)
                            if self.last_validated else 1)
        self.score = (0.3 * latency_score + 0.3 * success_rate +
                      0.2 * throughput_score + 0.1 * self.stability +
                      0.1 * age_score)


@dataclass
class CachedResponse:
    status: int
    content: bytes
    headers: Dict[str, str]
    timestamp: float
    ttl: int
    protocol: str = "http/1.1"
    def is_fresh(self):
        return time.time() - self.timestamp < self.ttl


@dataclass
class StreamResponse:
    """Live SSE/byte stream routed through the winning proxy.

    `aiter_raw()` yields raw byte chunks; the bridge layer reassembles
    SSE lines. The response is released on `aclose()`."""
    status: int
    headers: Dict[str, str]
    proxy_url: Optional[str]           # None → direct connection
    _iter: AsyncIterator[bytes] = field(repr=False, default=None)
    _closer: Any = field(repr=False, default=None)

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        async for chunk in self._iter:
            yield chunk

    async def read_error(self, limit: int = 4096) -> str:
        """Drain up to `limit` bytes (for error bodies when status != 200)."""
        buf = b""
        try:
            async for chunk in self._iter:
                buf += chunk
                if len(buf) >= limit:
                    break
        except Exception:
            pass
        finally:
            await self.aclose()
        return buf[:limit].decode("utf-8", errors="replace")

    async def aclose(self):
        try:
            if self._closer:
                await self._closer()
        except Exception:
            pass


# ─── Cache / dedup / limiter / scorer / circuit breaker ───────

class HTTPCache:
    """GET-only HTTP cache (memory + disk). Keys are header-aware.

    Adaptation vs upstream v5.3: the key now includes a hash of the
    request headers, so two accounts' authenticated GETs can never
    collide. POST/PUT/DELETE are never cached (state-changing)."""

    def __init__(self, ttl=DEFAULT_TTL):
        self.ttl = ttl
        self._memory: Dict[str, CachedResponse] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(method, url, params=None, headers=None, protocol="http/1.1"):
        h = json.dumps(headers or {}, sort_keys=True, default=str)
        return hashlib.sha256(
            f"{method}:{url}:{json.dumps(params or {}, sort_keys=True, default=str)}:{h}:{protocol}".encode()
        ).hexdigest()

    async def get(self, method, url, params=None, headers=None, protocol="http/1.1"):
        if method.upper() != "GET" or self.ttl <= 0:
            return None
        key = self._key(method, url, params, headers, protocol)
        if key in self._memory and self._memory[key].is_fresh():
            return self._memory[key]
        path = CACHE_DIR / f"{key}.json"
        if path.exists():
            try:
                def _read():
                    with open(path) as f:
                        return json.loads(f.read())
                data = await asyncio.to_thread(_read)
                cached = CachedResponse(
                    status=data["status"],
                    content=data["content"].encode('utf-8', errors='replace'),
                    headers=data["headers"],
                    timestamp=data["timestamp"],
                    ttl=data["ttl"],
                    protocol=data.get("protocol", "http/1.1"),
                )
                if cached.is_fresh():
                    async with self._lock:
                        self._memory[key] = cached
                    return cached
                await asyncio.to_thread(path.unlink, True)
            except Exception:
                pass
        return None

    async def set(self, method, url, response, params=None, headers=None):
        if method.upper() != "GET" or self.ttl <= 0:
            return
        key = self._key(method, url, params, headers, response.protocol)
        async with self._lock:
            self._memory[key] = response

        def _write():
            path = CACHE_DIR / f"{key}.json"
            data = {
                "status": response.status,
                "content": response.content.decode('utf-8', errors='replace'),
                "headers": response.headers,
                "timestamp": response.timestamp,
                "ttl": response.ttl,
                "protocol": response.protocol,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                f.write(json.dumps(data))
        try:
            await asyncio.to_thread(_write)
        except Exception as e:
            logger.debug(f"cache write failed: {e}")

    async def start_cleaner(self):
        async def _clean():
            while True:
                await asyncio.sleep(60)
                async with self._lock:
                    for k in [k for k, v in self._memory.items() if not v.is_fresh()]:
                        del self._memory[k]
        asyncio.create_task(_clean())


class RequestDeduplicator:
    """Coalesces concurrent identical GETs. Header-aware keys; non-GET
    requests always execute (refresh/profile POSTs must never be merged)."""

    def __init__(self):
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(method, url, params=None, headers=None, protocol="http/1.1"):
        return HTTPCache._key(method, url, params, headers, protocol)

    async def execute(self, method, url, params, headers, protocol, factory):
        if method.upper() != "GET":
            return await factory()
        key = self._key(method, url, params, headers, protocol)
        async with self._lock:
            if key in self._in_flight:
                return await self._in_flight[key]
            future = asyncio.get_event_loop().create_future()
            self._in_flight[key] = future
        try:
            result = await factory()
            if not future.done():
                future.set_result(result)
            return result
        except Exception as e:
            if not future.done():
                future.set_exception(e)
                future.add_done_callback(lambda f: f.exception())
            raise
        finally:
            async with self._lock:
                self._in_flight.pop(key, None)


@dataclass
class TokenBucket:
    rate: float
    capacity: float
    tokens: float = 0.0
    last_update: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def _replenish(self):
        now = time.time()
        async with self.lock:
            self.tokens = min(self.capacity, self.tokens + (now - self.last_update) * self.rate)
            self.last_update = now

    async def acquire(self, tokens=1.0):
        await self._replenish()
        async with self.lock:
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
        wait = (tokens - self.tokens) / max(self.rate, 0.01)
        await asyncio.sleep(wait)
        return await self.acquire(tokens)


class AdaptiveRateLimiter:
    def __init__(self, base_rate=DEFAULT_RATE):
        self.base_rate = base_rate
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def _get_bucket(self, domain):
        async with self._lock:
            if domain not in self._buckets:
                self._buckets[domain] = TokenBucket(rate=self.base_rate, capacity=5.0, tokens=5.0)
            return self._buckets[domain]

    async def acquire(self, url, tokens=1.0):
        bucket = await self._get_bucket(urlparse(url).netloc or url)
        await bucket.acquire(tokens)

    async def adjust(self, domain, status):
        bucket = await self._get_bucket(domain)
        if status == 429:
            bucket.rate = max(ADAPTIVE_MIN_RATE, bucket.rate * 0.5)
        elif status < 400:
            bucket.rate = min(ADAPTIVE_MAX_RATE, bucket.rate * 1.05)

    async def get_all_rates(self):
        return {d: b.rate for d, b in self._buckets.items()}


class QualityScorer:
    def __init__(self):
        self._scores: Dict[str, float] = {}

    def update(self, proxy_url, success, latency_ms=0):
        cur = self._scores.get(proxy_url, 0.5)
        self._scores[proxy_url] = cur * QUALITY_DECAY + (1 - QUALITY_DECAY) * (1.0 if success else 0.0)

    def get(self, proxy_url):
        return self._scores.get(proxy_url, 0.5)

    def get_all_scores(self):
        return dict(self._scores)


class AsyncCircuitBreaker:
    """Per-domain breaker on NETWORK failures only (connection errors,
    timeouts). HTTP 4xx/5xx application responses never trip it."""

    def __init__(self, failure_threshold=5, recovery_timeout=30):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure = 0
        self.state = "CLOSED"
        self._lock = asyncio.Lock()

    async def can_execute(self):
        async with self._lock:
            if self.state == "OPEN":
                if time.time() - self.last_failure > self.recovery_timeout:
                    self.state = "HALF_OPEN"
                    return True
                return False
            return True

    async def success(self):
        async with self._lock:
            self.failures = 0
            self.state = "CLOSED"

    async def failure(self):
        async with self._lock:
            self.failures += 1
            self.last_failure = time.time()
            if self.failures >= self.failure_threshold:
                self.state = "OPEN"


# ─── ProxyPoolManager ─────────────────────────────────────────

class ProxyPoolManager:
    def __init__(self, cache_max=MAX_PROXY_CACHE, countries=None):
        self._running = False
        self._cache_file = PROXY_CACHE_FILE
        self._cache_max = cache_max
        self._proxies: List[ProxyEntry] = []
        self._url_set: Set[str] = set()
        self.countries = countries or DEFAULT_COUNTRIES
        self._validation_queue: asyncio.Queue = asyncio.Queue()
        self._seed_ready = asyncio.Event()

    async def start(self):
        for d in (CACHE_DIR, CONFIG_DIR):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        self._running = True
        self._load_cache()
        asyncio.create_task(self._seed_from_github())
        asyncio.create_task(self._validation_worker())

    async def wait_ready(self, timeout: float = 15.0):
        try:
            await asyncio.wait_for(self._seed_ready.wait(), timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Seeding did not finish within {timeout}s")

    async def _seed_from_github(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(SEED_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        logger.warning(f"GitHub seeding HTTP {resp.status}")
                        return
                    data = await resp.json()
                    items = data if isinstance(data, list) else data.get("data", [])
                    count = 0
                    for item in items[:SEED_COUNT]:
                        ip   = item.get("ip")   or item.get("host") or ""
                        port = item.get("port") or ""
                        proto = (item.get("protocol") or "http").lower()
                        if ip and port:
                            p_url = f"{proto}://{ip}:{port}"
                            if p_url not in self._url_set:
                                self._proxies.append(ProxyEntry(p_url, protocol=proto, source="github"))
                                self._url_set.add(p_url)
                                await self._validation_queue.put(p_url)
                                count += 1
                    logger.info(f"Seeded {count} proxies from GitHub")
        except Exception as e:
            logger.warning(f"GitHub seeding failed: {e}")
        finally:
            self._seed_ready.set()

    async def _validation_worker(self):
        while self._running:
            try:
                url = await asyncio.wait_for(self._validation_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            entry = self.get_entry(url)
            if not entry:
                continue
            try:
                ok, latency = await self._quick_validate(url)
                if ok:
                    entry.update_check(True, latency=latency)
                    entry.last_validated = time.time()
                else:
                    entry.mark_failed()
            except Exception:
                entry.mark_failed()

    async def _quick_validate(self, proxy_url, timeout=None):
        timeout = timeout or max(PROXY_TIMEOUT, 8.0)
        start = time.time()
        try:
            scheme = proxy_url.split("://", 1)[0].lower()
            if scheme in ("socks4", "socks5", "socks5h"):
                if not AIOHTTP_SOCKS_AVAILABLE:
                    return False, 0.0
                connector = _SocksConnector.from_url(proxy_url)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(VALIDATE_URL,
                                           timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                        await resp.read()
                        if resp.status in VALIDATE_STATUSES:
                            return True, (time.time() - start) * 1000
            else:
                async with aiohttp.ClientSession() as session:
                    async with session.get(VALIDATE_URL,
                                           proxy=proxy_url,
                                           timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                        await resp.read()
                        if resp.status in VALIDATE_STATUSES:
                            return True, (time.time() - start) * 1000
        except Exception:
            pass
        return False, 0.0

    def _load_cache(self):
        if not self._cache_file.exists():
            return
        try:
            with open(self._cache_file) as f:
                data = json.load(f)
            for item in data.get("proxies", []):
                p = ProxyEntry(item["url"],
                               protocol=item.get("protocol", "http"),
                               country=item.get("country"),
                               source=item.get("source", "cache"))
                p.health       = item.get("healthy", True)
                p.ban_until    = item.get("ban_until", 0.0)
                p.fail_count   = item.get("fail_count", 0)
                p.avg_latency  = item.get("avg_latency", 500.0)
                p.score        = item.get("score", 0.0)
                p.state        = ProxyHealth.HEALTHY if p.health else ProxyHealth.FAILED
                p._recent_checks = item.get("recent_checks", [])
                self._proxies.append(p)
                self._url_set.add(p.url)
            logger.info(f"Loaded {len(self._proxies)} proxies from cache")
        except Exception as e:
            logger.warning(f"Cache load failed: {e}")

    def _save_cache(self):
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"proxies": [{
                "url": p.url, "protocol": p.protocol, "country": p.country,
                "source": p.source, "healthy": p.health, "ban_until": p.ban_until,
                "fail_count": p.fail_count, "success_count": p.success_count,
                "avg_latency": p.avg_latency, "score": p.score,
                "recent_checks": p._recent_checks,
            } for p in self._proxies]}
            with open(self._cache_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Cache save failed: {e}")

    def get_top_proxies(self, n):
        sorted_p = sorted(
            [p for p in self._proxies if p.health and not p.is_banned()],
            key=lambda p: p.score, reverse=True,
        )
        return [p.url for p in sorted_p[:n]]

    async def get_best_proxy(self, domain=None):
        top = self.get_top_proxies(1)
        return top[0] if top else None

    def get_entry(self, url):
        for p in self._proxies:
            if p.url == url:
                return p
        return None

    def get_all_urls(self):
        return [p.url for p in self._proxies if p.health and not p.is_banned()]

    def stop(self):
        self._running = False
        self._save_cache()


# ─── Request-execution internals (shared by buffered + stream) ──

def _aiohttp_conn_timeout(timeout):
    """Connect-capped, stream-safe timeout: no total limit so long-lived
    SSE responses (600s LLM streams) survive once headers arrive."""
    return aiohttp.ClientTimeout(total=None, connect=timeout, sock_connect=timeout)


class _AiohttpStreamed:
    """Owns an aiohttp session+response opened through a proxy until closed."""

    def __init__(self, resp, session):
        self._resp = resp
        self._session = session

    async def chunks(self):
        async for chunk in self._resp.content.iter_any():
            yield chunk

    async def close(self):
        try:
            self._resp.release()
        except Exception:
            pass
        try:
            await self._session.close()
        except Exception:
            pass


class _HttpxStreamed:
    def __init__(self, client, resp):
        self._client = client
        self._resp = resp

    async def chunks(self):
        async for chunk in self._resp.aiter_raw():
            yield chunk

    async def close(self):
        try:
            await self._resp.aclose()
        except Exception:
            pass
        try:
            await self._client.aclose()
        except Exception:
            pass


class _CurlStreamed:
    def __init__(self, resp):
        self._resp = resp

    async def chunks(self):
        async for chunk in self._resp.aiter_content():
            yield chunk

    async def close(self):
        try:
            await self._resp.astream_close()
        except Exception:
            pass


async def _open_via_aiohttp(method, url, params, headers, proxy_url, connect_timeout,
                            json_body=None, content=None, request_kwargs=None):
    """Open a proxied aiohttp response; returns _AiohttpStreamed once HEADERS
    have arrived (raises on connect failure). Body is streamed afterwards."""
    scheme = proxy_url.split("://", 1)[0].lower() if proxy_url else None
    kwargs = dict(request_kwargs or {})
    if json_body is not None:
        kwargs["json"] = json_body
    if content is not None:
        kwargs["data"] = content
    if proxy_url and scheme in ("socks4", "socks5", "socks5h"):
        if not AIOHTTP_SOCKS_AVAILABLE:
            raise RuntimeError("SOCKS requires aiohttp-socks")
        connector = _SocksConnector.from_url(proxy_url)
        session = aiohttp.ClientSession(connector=connector)
    else:
        session = aiohttp.ClientSession()
    try:
        resp = await asyncio.wait_for(
            session.request(method, url, params=params, headers=headers,
                            proxy=proxy_url, timeout=_aiohttp_conn_timeout(connect_timeout),
                            **kwargs),
            connect_timeout,
        )
        return _AiohttpStreamed(resp, session)
    except Exception:
        try:
            await session.close()
        except Exception:
            pass
        raise


async def _open_direct(method, url, params, headers, connect_timeout,
                       json_body=None, content=None, request_kwargs=None,
                       impersonate=None):
    """Open a direct (non-proxied) streaming response."""
    kwargs = dict(request_kwargs or {})
    if json_body is not None:
        kwargs["json"] = json_body
    if content is not None:
        kwargs["content"] = content
    if impersonate and CURL_CFFI_AVAILABLE:
        session = CurlAsyncSession(impersonate=impersonate)
        try:
            resp = await asyncio.wait_for(
                session.request(method, url, params=params, headers=headers,
                                timeout=connect_timeout, **kwargs),
                connect_timeout,
            )
            return _CurlStreamed(resp)
        except Exception:
            try:
                await session.close()
            except Exception:
                pass
            raise
    timeout = httpx.Timeout(connect=connect_timeout, read=None, write=None, pool=None)
    client = httpx.AsyncClient(http2=True, verify=False, timeout=timeout)
    try:
        req = client.build_request(method, url, params=params, headers=headers, **kwargs)
        resp = await asyncio.wait_for(client.send(req, stream=True), connect_timeout)
        return _HttpxStreamed(client, resp)
    except Exception:
        try:
            await client.aclose()
        except Exception:
            pass
        raise


# ─── ResilientClient (v5.3-autoclaw) ──────────────────────────

class ResilientClient:
    """Proxy-first resilient HTTP client with hedged racing, direct
    fallback, buffered + streaming request modes.

    Buffering semantics: GET responses may be cached (header-aware) when
    cache_ttl > 0; POST/PUT/DELETE always execute and are never cached.
    """

    def __init__(self,
                 cache_ttl=None,
                 rate_limit=DEFAULT_RATE,
                 use_curl_cffi=None,
                 countries=None,
                 startup_timeout=15.0,
                 enable_cache=None):
        self.cache = HTTPCache(
            DEFAULT_TTL if cache_ttl is None else cache_ttl
            if enable_cache is None else (cache_ttl or 0))
        self.dedup = RequestDeduplicator()
        self.limiter = AdaptiveRateLimiter(base_rate=rate_limit)
        self.pool_manager = ProxyPoolManager(countries=countries)
        self.circuit_breakers: Dict[str, AsyncCircuitBreaker] = {}
        self.scorer = QualityScorer()
        if use_curl_cffi is None:
            use_curl_cffi = bool(TLS_IMPERSONATE)
        self.use_curl_cffi = use_curl_cffi and CURL_CFFI_AVAILABLE
        self.impersonate = TLS_IMPERSONATE if self.use_curl_cffi else None
        self._startup_timeout = startup_timeout
        self._started = False

        logger.info(
            f"OWL-AGENT v{VERSION} initialized | curl_cffi={self.use_curl_cffi} | "
            f"socks={AIOHTTP_SOCKS_AVAILABLE} | psutil={PSUTIL_AVAILABLE}"
        )

    async def start(self):
        if self._started:
            return
        await self.pool_manager.start()
        await self.pool_manager.wait_ready(timeout=self._startup_timeout)
        await self.cache.start_cleaner()
        self._started = True

    async def close(self):
        self.pool_manager.stop()
        self._started = False

    # context-manager sugar (parity with upstream v5.3)
    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # staticmethod alias so tests can monkeypatch direct-dial per instance
    _open_direct = staticmethod(_open_direct)

    # ── public API ────────────────────────────────────────────
    async def request(self, method, url, params=None, headers=None, **kwargs):
        """Buffered request. Returns CachedResponse."""
        method = method.upper()
        cached = await self.cache.get(method, url, params, headers)
        if cached:
            return cached

        async def factory():
            return await self._execute_buffered(method, url, params, headers, **kwargs)

        return await self.dedup.execute(method, url, params, headers, "http/1.1", factory)

    async def stream_request(self, method, url, params=None, headers=None,
                             json_body=None, content=None, timeout=None, **kwargs):
        """Streaming request (SSE-safe). Races HEDGE_FANOUT proxies for
        connection establishment; first proxy to return headers wins and
        its live stream is returned. Falls back to direct on total failure.

        Returns StreamResponse — caller MUST consume or aclose() it.
        """
        method = method.upper()
        timeout = timeout if timeout is not None else DIRECT_TIMEOUT
        proxy_timeout = min(timeout, PROXY_TIMEOUT)
        domain = urlparse(url).netloc
        breaker = self._breaker_for(domain)

        if not await breaker.can_execute():
            raise RuntimeError(f"Circuit breaker open for {domain}")

        candidates = self.pool_manager.get_top_proxies(HEDGE_FANOUT)
        if candidates:
            winner = await self._race_streams(
                candidates, method, url, params, headers,
                json_body=json_body, content=content,
                connect_timeout=proxy_timeout, **kwargs)
            if winner is not None:
                status, resp_headers, streamed, proxy_url = winner
                if status in BAD_PROXY_STATUSES:
                    await streamed.close()
                    entry = self.pool_manager.get_entry(proxy_url)
                    if entry:
                        entry.mark_failed()
                else:
                    self.scorer.update(proxy_url, success=True)
                    entry = self.pool_manager.get_entry(proxy_url)
                    if entry:
                        entry.update_check(True, latency=0)
                        entry.last_validated = time.time()
                    await breaker.success()
                    return StreamResponse(
                        status=status, headers=resp_headers, proxy_url=proxy_url,
                        _iter=streamed.chunks(), _closer=streamed.close)

            for px in candidates:
                self.scorer.update(px, success=False)
                entry = self.pool_manager.get_entry(px)
                if entry:
                    entry.mark_failed()
            logger.warning(f"All {len(candidates)} hedged proxies failed (stream)")

        # ── Direct streaming fallback ──────────────────────────
        logger.info("All proxies exhausted (stream), attempting direct connection...")
        try:
            streamed = await self._open_direct(method, url, params, headers, proxy_timeout,
                                          json_body=json_body, content=content,
                                          request_kwargs=kwargs,
                                          impersonate=self.impersonate)
            status = streamed._resp.status
            resp_headers = dict(getattr(streamed._resp, "headers", {}) or {})
            if hasattr(resp_headers, "multi_items"):
                resp_headers = dict(resp_headers.multi_items())
            await breaker.success()
            logger.info(f"✅ Direct stream fallback succeeded for {url}")
            return StreamResponse(status=status, headers=resp_headers,
                                  proxy_url=None, _iter=streamed.chunks(),
                                  _closer=streamed.close)
        except Exception as e:
            await breaker.failure()
            raise RuntimeError(f"Direct stream also failed: {type(e).__name__}: {e!r}")

    # ── internals ─────────────────────────────────────────────
    def _breaker_for(self, domain):
        if domain not in self.circuit_breakers:
            self.circuit_breakers[domain] = AsyncCircuitBreaker()
        return self.circuit_breakers[domain]

    async def _execute_buffered(self, method, url, params, headers, **kwargs):
        domain = urlparse(url).netloc
        breaker = self._breaker_for(domain)
        if not await breaker.can_execute():
            raise RuntimeError(f"Circuit breaker open for {domain}")

        timeout       = kwargs.pop("timeout", DIRECT_TIMEOUT)
        proxy_timeout = min(timeout, PROXY_TIMEOUT)

        # ── Hedged parallel proxy race ────────────────────────
        candidates = self.pool_manager.get_top_proxies(HEDGE_FANOUT)
        if candidates:
            winner = await self._race_proxies(
                candidates, method, url, params, headers, proxy_timeout, **kwargs
            )
            if winner is not None:
                status, content, resp_headers, latency, proxy_url = winner
                cached = CachedResponse(
                    status=status, content=content, headers=resp_headers,
                    timestamp=time.time(), ttl=self.cache.ttl,
                )
                self.scorer.update(proxy_url, success=True, latency_ms=latency)
                entry = self.pool_manager.get_entry(proxy_url)
                if entry:
                    entry.update_check(True, latency=latency)
                    entry.last_validated = time.time()
                await self.limiter.adjust(domain, status)
                await breaker.success()
                await self.cache.set(method, url, cached, params, headers)
                return cached

            # All hedged proxies failed — ban them
            for px in candidates:
                self.scorer.update(px, success=False)
                entry = self.pool_manager.get_entry(px)
                if entry:
                    entry.mark_failed()
            await self.limiter.adjust(domain, 500)
            await breaker.failure()
            logger.warning(f"All {len(candidates)} hedged proxies failed")

        # ── Direct fallback ───────────────────────────────────
        logger.info("All proxies exhausted, attempting direct connection...")
        try:
            await self.limiter.acquire(url)
            if self.use_curl_cffi:
                response = await self._curl_request(
                    method, url, params=params, headers=headers,
                    timeout=timeout, **kwargs)
            else:
                response = await self._httpx_request(
                    method, url, params=params, headers=headers,
                    timeout=timeout, **kwargs)
            content, status, resp_headers = response
            await self.limiter.adjust(domain, status)
            await breaker.success()
            cached = CachedResponse(
                status=status, content=content, headers=resp_headers,
                timestamp=time.time(), ttl=self.cache.ttl,
            )
            await self.cache.set(method, url, cached, params, headers)
            logger.info(f"✅ Direct fallback succeeded for {url}")
            return cached
        except Exception as e:
            await breaker.failure()
            raise RuntimeError(f"Direct connection also failed: {type(e).__name__}: {e!r}")

    async def _httpx_request(self, method, url, params=None, headers=None,
                             timeout=DIRECT_TIMEOUT, **kwargs):
        async with httpx.AsyncClient(http2=True, verify=False,
                                     timeout=timeout) as client:
            resp = await client.request(method, url, params=params,
                                        headers=headers, **kwargs)
            content = await resp.aread()
            return content, resp.status_code, dict(resp.headers)

    async def _curl_request(self, method, url, params=None, headers=None,
                            timeout=DIRECT_TIMEOUT, **kwargs):
        async with CurlAsyncSession(impersonate=self.impersonate or "chrome110") as session:
            resp = await session.request(method, url, params=params,
                                         headers=headers, timeout=timeout, **kwargs)
            content = resp.content if isinstance(resp.content, bytes) else bytes(resp.content)
            return content, resp.status_code, dict(resp.headers)

    async def _race_proxies(self, candidates, method, url, params, headers,
                            timeout, **kwargs):
        """Fire N proxy attempts in parallel; return first successful tuple."""
        async def attempt(proxy_url):
            start = time.time()
            scheme = proxy_url.split("://", 1)[0].lower()
            if scheme in ("socks4", "socks5", "socks5h"):
                if not AIOHTTP_SOCKS_AVAILABLE:
                    raise RuntimeError("SOCKS requires aiohttp-socks")
                connector = _SocksConnector.from_url(proxy_url)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.request(
                        method, url, params=params, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                        **kwargs,
                    ) as resp:
                        content = await resp.read()
                        status = resp.status
                        resp_headers = dict(resp.headers)
            else:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method, url, params=params, headers=headers,
                        proxy=proxy_url,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                        **kwargs,
                    ) as resp:
                        content = await resp.read()
                        status = resp.status
                        resp_headers = dict(resp.headers)

            if status in BAD_PROXY_STATUSES:
                raise RuntimeError(f"bad proxy status {status}")
            latency = (time.time() - start) * 1000
            return (status, content, resp_headers, latency, proxy_url)

        return await self._race_tasks(candidates, attempt)

    async def _race_streams(self, candidates, method, url, params, headers,
                            connect_timeout, json_body=None, content=None, **kwargs):
        """Race N proxies for connection establishment (time-to-headers).
        First proxy to deliver headers wins; losers are cancelled and their
        sockets closed. Returns (status, headers, streamed, proxy_url)."""
        async def attempt(proxy_url):
            streamed = await _open_via_aiohttp(
                method, url, params, headers, proxy_url, connect_timeout,
                json_body=json_body, content=content, request_kwargs=kwargs)
            return (streamed._resp.status, dict(streamed._resp.headers),
                    streamed, proxy_url)

        return await self._race_tasks(candidates, attempt)

    async def _race_tasks(self, candidates, attempt_fn):
        tasks = {asyncio.create_task(attempt_fn(px)): px for px in candidates}
        try:
            for fut in asyncio.as_completed(list(tasks.keys())):
                try:
                    return await fut
                except Exception as e:
                    logger.debug(f"hedge attempt failed: {e}")
            return None
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

    async def get_stats(self):
        total = len(self.pool_manager._proxies)
        healthy = sum(1 for p in self.pool_manager._proxies
                      if p.health and not p.is_banned())
        return {
            "version": VERSION,
            "enabled": True,
            "proxies_total": total,
            "proxies_healthy": healthy,
            "socks_available": AIOHTTP_SOCKS_AVAILABLE,
            "curl_cffi_available": CURL_CFFI_AVAILABLE,
            "curl_cffi_enabled": self.use_curl_cffi,
            "psutil_available": PSUTIL_AVAILABLE,
            "backend": "vendored",
            "scores": self.scorer.get_all_scores(),
            "rates": await self.limiter.get_all_rates(),
        }


# ─── CLI (parity with upstream v5.3) ──────────────────────────

async def main():
    import argparse
    parser = argparse.ArgumentParser(description=f"OWL-AGENT v{VERSION} (AutoClaw vendored)")
    parser.add_argument("command", choices=["fetch", "stats", "benchmark"])
    parser.add_argument("url", nargs="?", help="URL to fetch")
    parser.add_argument("--curl-cffi", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    client = ResilientClient(use_curl_cffi=args.curl_cffi)
    await client.start()
    try:
        if args.command == "fetch":
            if not args.url:
                print("Please provide a URL")
                return
            resp = await client.request("GET", args.url)
            if args.json:
                print(json.dumps({
                    "status": resp.status,
                    "headers": resp.headers,
                    "body": resp.content.decode('utf-8', errors='replace')[:2000],
                }, indent=2, default=str))
            else:
                print(f"Status: {resp.status}")
                print(resp.content.decode('utf-8', errors='replace')[:500])
        elif args.command == "stats":
            print(json.dumps(await client.get_stats(), indent=2, default=str))
        elif args.command == "benchmark":
            urls = [
                "https://api.github.com/zen",
                "https://www.google.com",
                "https://raw.githubusercontent.com/github/gitignore/main/Python.gitignore",
            ]
            results = []
            for url in urls:
                start = time.time()
                try:
                    resp = await client.request("GET", url)
                    results.append({"url": url, "status": resp.status,
                                    "time": round(time.time() - start, 3)})
                except Exception as e:
                    results.append({"url": url, "error": str(e)})
            print(json.dumps(results, indent=2, default=str))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
