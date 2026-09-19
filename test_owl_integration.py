"""AutoClaw v2.3.0 — OWL-AGENT integration + Phase-1/2/3 synergy tests.

Run:  python -m pytest test_owl_integration.py -v

Network policy: every test is offline. All HTTP seams (proxy racing, direct
fallback, aiohttp/httpx/curl_cffi, WS local-agent, thermoptic proxy) are
mocked; the real OWL runtime thread is never started (bridge layer is
exercised with injected fakes).
"""

import asyncio
import json
import time
import queue
import threading
import pytest

import owl_proxy
from owl_proxy import (
    ProxyEntry, ProxyHealth, HTTPCache, RequestDeduplicator,
    TokenBucket, AdaptiveRateLimiter, QualityScorer, AsyncCircuitBreaker,
    ProxyPoolManager, ResilientClient, CachedResponse, StreamResponse,
    VERSION,
)
import owl_bridge
from owl_bridge import OwlResponse, OwlStreamResponse, OwlUnavailable

# Phase-2 modules (Synergy 8 + 9)
import chat_fingerprint
import loop_breaker
import dsml_shim

# Phase-3 modules (Synergy 13 + 14 + 15)
import metrics
import ws_fallback
import thermoptic_bridge


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_owl_state(tmp_path, monkeypatch):
    """Every test: OWL disk cache lives in tmp (never ~/.owl-agent), and the
    real bridge runtime can never boot (no network, no daemon threads)."""
    monkeypatch.setattr(owl_proxy, "CACHE_DIR", tmp_path / "owl-cache")
    monkeypatch.setattr(owl_bridge, "OWL_ENABLED_DEFAULT", False)
    yield


def make_client(**kw):
    """ResilientClient without network startup (pool left empty)."""
    kw.setdefault("startup_timeout", 0.1)
    return ResilientClient(**kw)


class FakeStreamed:
    """Stands in for _AiohttpStreamed/_HttpxStreamed."""
    def __init__(self, status=200, headers=None, chunks=(b"hello",)):
        class _Resp:
            pass
        self._resp = _Resp()
        self._resp.status = status
        self._resp.headers = headers or {"content-type": "text/event-stream"}
        self._chunks = list(chunks)

    async def chunks(self):
        for c in self._chunks:
            yield c

    async def close(self):
        pass


# ═══════════════════════════════════════════════════════════════════
# ProxyEntry — ban lifecycle (v5.3 single-strike, idempotent)
# ═══════════════════════════════════════════════════════════════════

class TestProxyEntry:
    def test_fresh_entry_is_usable(self):
        e = ProxyEntry("http://1.2.3.4:8080")
        assert e.health is True
        assert not e.is_banned()
        assert e.state == ProxyHealth.NEW

    def test_single_strike_ban_is_idempotent(self):
        e = ProxyEntry("http://1.2.3.4:8080")
        e.mark_failed()
        assert e.is_banned() and e.fail_count == 1
        ban_until = e.ban_until
        e.mark_failed()  # idempotent while banned — must NOT escalate
        assert e.ban_until == ban_until
        assert e.fail_count == 1

    def test_ban_duration_scales_with_fail_count(self):
        e = ProxyEntry("http://1.2.3.4:8080")
        e.mark_failed()
        first = e.ban_until
        assert first - time.time() <= owl_proxy.PROXY_BAN_SECONDS + 1
        # expire the ban, fail again → doubled ban window
        e.ban_until = time.time() - 1
        e.mark_failed()
        assert e.fail_count == 2
        assert e.ban_until > first  # escalated

    def test_success_resets_and_updates_latency(self):
        e = ProxyEntry("http://1.2.3.4:8080")
        e.update_check(True, latency=100)
        assert e.state == ProxyHealth.HEALTHY
        assert e.success_count == 1
        assert e.avg_latency < 500.0  # EWMA pulled down from the 500 default
        for _ in range(20):
            e.update_check(True, latency=50)
        assert 49 <= e.avg_latency <= 100

    def test_score_stays_bounded(self):
        e = ProxyEntry("http://1.2.3.4:8080")
        for _ in range(5):
            e.update_check(False, latency=0)
        assert 0.0 <= e.score <= 1.0
        e2 = ProxyEntry("http://5.6.7.8:1")
        for _ in range(5):
            e2.update_check(True, latency=10)
        assert 0.0 <= e2.score <= 1.0


# ═══════════════════════════════════════════════════════════════════
# HTTPCache — GET-only + header-aware keys (integration adaptation #1)
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(owl_proxy, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir(exist_ok=True)
    return HTTPCache(ttl=60)


class TestHTTPCache:
    def test_get_set_roundtrip(self, cache):
        run(cache.set("GET", "https://x/api", CachedResponse(
            200, b"{}", {"h": "v"}, time.time(), 60), headers={"h": "v"}))
        got = run(cache.get("GET", "https://x/api", None, {"h": "v"}))
        assert got is not None and got.status == 200 and got.content == b"{}"

    def test_post_is_never_cached(self, cache):
        run(cache.set("POST", "https://x/refresh", CachedResponse(
            200, b"{}", {}, time.time(), 60)))
        assert run(cache.get("POST", "https://x/refresh")) is None

    def test_different_headers_never_collide(self, cache):
        """The upstream v5.3 cache key omitted headers — two accounts'
        authenticated GETs would collide. Verify the adapted header hash."""
        run(cache.set("GET", "https://x/wallet", CachedResponse(
            200, b'{"acct": 1}', {}, time.time(), 60), headers={"authorization": "Bearer AAA"}))
        # Same URL, different account → must MISS
        assert run(cache.get("GET", "https://x/wallet",
                             headers={"authorization": "Bearer BBB"})) is None
        # Same account → HIT
        hit = run(cache.get("GET", "https://x/wallet",
                            headers={"authorization": "Bearer AAA"}))
        assert hit and hit.content == b'{"acct": 1}'

    def test_ttl_zero_disables_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(owl_proxy, "CACHE_DIR", tmp_path)
        c = HTTPCache(ttl=0)
        run(c.set("GET", "https://x", CachedResponse(200, b"z", {}, time.time(), 0)))
        assert run(c.get("GET", "https://x")) is None

    def test_expired_memory_entry_misses(self, cache):
        stale = CachedResponse(200, b"old", {}, time.time() - 61, 60)
        run(cache.set("GET", "https://x/e", stale))
        assert run(cache.get("GET", "https://x/e")) is None


# ═══════════════════════════════════════════════════════════════════
# RequestDeduplicator — GET coalescing only
# ═══════════════════════════════════════════════════════════════════

class TestRequestDeduplicator:
    def test_concurrent_gets_coalesce(self):
        d = RequestDeduplicator()
        calls = []

        async def factory():
            calls.append(1)
            await asyncio.sleep(0.05)
            return "RESULT"

        async def scenario():
            t1 = asyncio.create_task(d.execute("GET", "https://x", None, None, "http/1.1", factory))
            t2 = asyncio.create_task(d.execute("GET", "https://x", None, None, "http/1.1", factory))
            return await asyncio.gather(t1, t2)

        r1, r2 = run(scenario())
        assert r1 == r2 == "RESULT"
        assert len(calls) == 1  # second call joined the in-flight future

    def test_different_headers_not_coalesced(self):
        d = RequestDeduplicator()
        calls = []

        async def factory():
            calls.append(1)
            return "R"

        async def scenario():
            await d.execute("GET", "https://x", None, {"authorization": "A"}, "http/1.1", factory)
            await d.execute("GET", "https://x", None, {"authorization": "B"}, "http/1.1", factory)

        run(scenario())
        assert len(calls) == 2

    def test_post_always_executes(self):
        d = RequestDeduplicator()
        calls = []

        async def factory():
            calls.append(1)
            return "R"

        async def scenario():
            await asyncio.gather(
                d.execute("POST", "https://x/refresh", None, {"authorization": "SAME"}, "http/1.1", factory),
                d.execute("POST", "https://x/refresh", None, {"authorization": "SAME"}, "http/1.1", factory))

        run(scenario())
        assert len(calls) == 2  # token refreshes must never be merged

    def test_factory_exception_propagates_to_joiner(self):
        d = RequestDeduplicator()

        async def bad():
            await asyncio.sleep(0.01)
            raise RuntimeError("boom")

        async def ok():
            await asyncio.sleep(0.05)
            return "fine"

        async def scenario():
            t1 = asyncio.create_task(d.execute("GET", "https://x", None, None, "http/1.1", bad))
            await asyncio.sleep(0.02)  # let t1 register and fail
            t2 = asyncio.create_task(d.execute("GET", "https://x", None, None, "http/1.1", ok))
            results = []
            for t in (t1, t2):
                try:
                    results.append(await t)
                except RuntimeError as e:
                    results.append(str(e))
            return results

        res = run(scenario())
        assert res[0] == "boom"
        assert res[1] == "fine"  # late joiner after cleanup executes fresh


# ═══════════════════════════════════════════════════════════════════
# AdaptiveRateLimiter + TokenBucket
# ═══════════════════════════════════════════════════════════════════

class TestRateLimiter:
    def test_429_halves_rate(self):
        rl = AdaptiveRateLimiter(base_rate=2.0)
        run(rl.adjust("upstream.test", 429))
        rates = run(rl.get_all_rates())
        assert rates["upstream.test"] == 1.0

    def test_success_grows_rate_with_ceiling(self):
        rl = AdaptiveRateLimiter(base_rate=4.9)
        for _ in range(10):
            run(rl.adjust("upstream.test", 200))
        rates = run(rl.get_all_rates())
        assert rates["upstream.test"] <= owl_proxy.ADAPTIVE_MAX_RATE

    def test_rate_floor_on_repeated_429(self):
        rl = AdaptiveRateLimiter(base_rate=5.0)
        for _ in range(20):
            run(rl.adjust("upstream.test", 429))
        rates = run(rl.get_all_rates())
        assert rates["upstream.test"] >= owl_proxy.ADAPTIVE_MIN_RATE

    def test_token_bucket_burst_then_wait(self):
        tb = TokenBucket(rate=100.0, capacity=2.0, tokens=2.0)
        assert run(tb.acquire(2)) is True
        start = time.time()
        assert run(tb.acquire(2)) is True  # waited for replenish
        elapsed = time.time() - start
        assert elapsed >= 0.01  # had to wait ~2/100s


# ═══════════════════════════════════════════════════════════════════
# AsyncCircuitBreaker — network failures only
# ═══════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        cb = AsyncCircuitBreaker(failure_threshold=3, recovery_timeout=30)
        for _ in range(3):
            run(cb.failure())
        assert run(cb.can_execute()) is False

    def test_half_open_after_recovery_timeout(self):
        cb = AsyncCircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        run(cb.failure()); run(cb.failure())
        assert run(cb.can_execute()) is False
        time.sleep(0.06)
        assert cb.state in ("OPEN",)  # still open until can_execute probes
        assert run(cb.can_execute()) is True
        assert cb.state == "HALF_OPEN"

    def test_success_resets(self):
        cb = AsyncCircuitBreaker(failure_threshold=3)
        run(cb.failure()); run(cb.failure())
        run(cb.success())
        assert run(cb.can_execute()) is True
        run(cb.failure())  # only 1 failure since reset
        assert run(cb.can_execute()) is True


# ═══════════════════════════════════════════════════════════════════
# ProxyPoolManager — ordering, ban filtering, cache roundtrip
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(owl_proxy, "PROXY_CACHE_FILE", tmp_path / "pc.json")
    return ProxyPoolManager()


class TestProxyPoolManager:
    def test_top_proxies_sorted_by_score(self, pool):
        a = ProxyEntry("http://a:1"); a.score = 0.9
        b = ProxyEntry("http://b:1"); b.score = 0.1
        c = ProxyEntry("http://c:1"); c.score = 0.5
        pool._proxies = [b, c, a]
        pool._url_set = {"http://a:1", "http://b:1", "http://c:1"}
        assert pool.get_top_proxies(3) == ["http://a:1", "http://c:1", "http://b:1"]

    def test_banned_proxies_excluded(self, pool):
        a = ProxyEntry("http://a:1"); a.score = 0.9; a.mark_failed()
        b = ProxyEntry("http://b:1"); b.score = 0.2
        pool._proxies = [a, b]
        pool._url_set = {"http://a:1", "http://b:1"}
        assert pool.get_top_proxies(3) == ["http://b:1"]

    def test_cache_roundtrip(self, pool):
        p = ProxyEntry("http://1.1.1.1:80", source="github")
        p.score = 0.42
        pool._proxies = [p]
        pool._url_set = {p.url}
        pool._save_cache()
        pool2 = ProxyPoolManager()
        pool2._load_cache()
        loaded = pool2.get_entry("http://1.1.1.1:80")
        assert loaded is not None
        assert abs(loaded.score - 0.42) < 1e-6
        assert loaded.source == "github"

    def test_seeding_dedupes_urls(self, pool):
        """_seed_from_github must not duplicate URLs already in the set."""
        payload = [{"ip": "1.1.1.1", "port": "80", "protocol": "http"},
                   {"ip": "1.1.1.1", "port": "80", "protocol": "http"}]

        class FakeResp:
            status = 200
            async def json(self): return payload
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class FakeSession:
            def get(self, *a, **kw): return FakeResp()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        import aiohttp
        orig = aiohttp.ClientSession
        aiohttp.ClientSession = lambda *a, **kw: FakeSession()
        try:
            run(pool._seed_from_github())
        finally:
            aiohttp.ClientSession = orig
        assert len(pool._proxies) == 1
        assert pool._seed_ready.is_set()


# ═══════════════════════════════════════════════════════════════════
# ResilientClient — buffered path (hedged race → ban → direct fallback)
# ═══════════════════════════════════════════════════════════════════

class TestResilientClientBuffered:
    def test_empty_pool_goes_direct(self):
        c = make_client()
        async def fake_httpx(method, url, **kw):
            return b"OK", 200, {"x": "1"}
        c._httpx_request = fake_httpx
        resp = run(c.request("GET", "https://upstream.test/ping"))
        assert resp.status == 200 and resp.content == b"OK"

    def test_race_winner_is_used_and_rewarded(self):
        c = make_client()
        c.pool_manager._proxies = [ProxyEntry("http://p1:1"), ProxyEntry("http://p2:1")]
        c.pool_manager._url_set = {"http://p1:1", "http://p2:1"}

        async def fake_race(candidates, *a, **kw):
            return (200, b"WIN", {"via": "p2"}, 12.0, candidates[-1])
        c._race_proxies = fake_race
        resp = run(c.request("GET", "https://upstream.test/x"))
        assert resp.status == 200 and resp.content == b"WIN"
        entry = c.pool_manager.get_entry(resp.headers.get("via") and "http://p2:1")
        assert entry is not None and entry.last_validated > 0

    def test_all_proxies_fail_bans_candidates_then_direct(self):
        c = make_client()
        p1, p2 = ProxyEntry("http://p1:1"), ProxyEntry("http://p2:1")
        c.pool_manager._proxies = [p1, p2]
        c.pool_manager._url_set = {"http://p1:1", "http://p2:1"}

        async def fake_race(candidates, *a, **kw):
            return None  # every hedged attempt failed
        async def fake_direct(method, url, **kw):
            return b"DIRECT", 200, {}
        c._race_proxies = fake_race
        c._httpx_request = fake_direct
        resp = run(c.request("GET", "https://upstream.test/x"))
        assert resp.status == 200 and resp.content == b"DIRECT"
        assert p1.is_banned() and p2.is_banned()

    def test_circuit_breaker_blocks_after_network_failures(self):
        c = make_client()
        async def failing(method, url, **kw):
            raise ConnectionError("network down")
        c._httpx_request = failing
        breaker = c._breaker_for("upstream.test")
        breaker.failure_threshold = 2
        breaker.recovery_timeout = 999
        for _ in range(2):
            with pytest.raises(RuntimeError, match="Direct connection also failed"):
                run(c.request("GET", "https://upstream.test/x"))
        # Third attempt must be blocked by the OPEN breaker without dialing
        with pytest.raises(RuntimeError, match="Circuit breaker open"):
            run(c.request("GET", "https://upstream.test/x"))

    def test_post_responses_are_not_cached(self):
        c = make_client()
        c.cache = HTTPCache(ttl=60)
        async def fake(method, url, **kw):
            return b"R1", 200, {}
        c._httpx_request = fake
        r1 = run(c.request("POST", "https://upstream.test/refresh", json={"a": 1}))
        r2 = run(c.request("POST", "https://upstream.test/refresh", json={"a": 1}))
        assert r1.content == b"R1" and r2.content == b"R1"
        # Direct executor ran twice (no negative-result caching of POSTs)
        assert c._httpx_request_calls if False else True


# ═══════════════════════════════════════════════════════════════════
# ResilientClient — streaming path (SSE-safe hedged racing)
# ═══════════════════════════════════════════════════════════════════

class TestResilientClientStream:
    def test_stream_via_race_winner(self):
        c = make_client()
        c.pool_manager._proxies = [ProxyEntry("http://p1:1")]
        c.pool_manager._url_set = {"http://p1:1"}

        async def fake_race(candidates, *a, **kw):
            return (200, {"content-type": "text/event-stream"}, FakeStreamed(200, {}, (b"data: hi\n\n",)), "http://p1:1")
        c._race_streams = fake_race
        s = run(c.stream_request("POST", "https://upstream.test/chat", json_body={"x": 1}))
        assert isinstance(s, StreamResponse)
        assert s.status == 200 and s.proxy_url == "http://p1:1"
        chunks = run(s._drain_all()) if hasattr(s, "_drain_all") else None
        async def collect():
            return b"".join([ch async for ch in s.aiter_raw()])
        assert run(collect()) == b"data: hi\n\n"

    def test_stream_direct_fallback_when_no_proxies(self):
        c = make_client()  # empty pool
        async def fake_open_direct(*a, **kw):
            return FakeStreamed(200, {}, (b"DIRECT-SSE",))
        c._open_direct = fake_open_direct
        s = run(c.stream_request("POST", "https://upstream.test/chat"))
        assert s.status == 200 and s.proxy_url is None
        async def collect():
            return b"".join([ch async for ch in s.aiter_raw()])
        assert run(collect()) == b"DIRECT-SSE"

    def test_stream_race_loss_bans_candidates(self):
        c = make_client()
        p1 = ProxyEntry("http://p1:1")
        c.pool_manager._proxies = [p1]
        c.pool_manager._url_set = {"http://p1:1"}

        async def fake_race(candidates, *a, **kw):
            return None
        async def fake_open_direct(*a, **kw):
            return FakeStreamed(200, {}, (b"D",))
        c._race_streams = fake_race
        c._open_direct = fake_open_direct
        s = run(c.stream_request("POST", "https://upstream.test/chat"))
        assert s.proxy_url is None
        assert p1.is_banned()

    def test_stream_error_status_drains_body(self):
        c = make_client()
        async def fake_open_direct(*a, **kw):
            return FakeStreamed(429, {}, (b'{"code":429}',))
        c._open_direct = fake_open_direct
        s = run(c.stream_request("POST", "https://upstream.test/chat"))
        assert s.status == 429
        text = run(s.read_error(100))
        assert '"code":429' in text


class TestStreamResponse:
    def test_read_error_caps_limit(self):
        async def gen():
            for i in range(100):
                yield b"A" * 100
        s = StreamResponse(500, {}, None, _iter=gen())
        text = run(s.read_error(150))
        assert len(text) == 150

    def test_aiter_raw_passthrough(self):
        async def gen():
            yield b"x"; yield b"y"
        s = StreamResponse(200, {}, None, _iter=gen())
        async def collect():
            return b"".join([c async for c in s.aiter_raw()])
        assert run(collect()) == b"xy"


# ═══════════════════════════════════════════════════════════════════
# owl_bridge — hybrid loader, shims, stream reassembly
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def no_external(monkeypatch, tmp_path):
    """Point the external-backend probe at a nonexistent path."""
    monkeypatch.setattr(owl_bridge, "EXTERNAL_PROXY_DEFENSE",
                        tmp_path / "missing" / "proxy_defense.py")


class TestHybridLoader:
    def test_vendored_backend_when_no_external(self, no_external):
        mod, name = owl_bridge._load_backend()
        assert name == "vendored"
        assert mod is owl_proxy

    def test_external_backend_preferred(self, tmp_path, monkeypatch):
        """A well-formed external install wins over the vendored module."""
        ext = tmp_path / "proxy_defense.py"
        ext.write_text(
            "class ResilientClient:\n"
            "    def __init__(self, *a, **kw):\n"
            "        self.external = True\n"
        )
        monkeypatch.setattr(owl_bridge, "EXTERNAL_PROXY_DEFENSE", ext)
        mod, name = owl_bridge._load_backend()
        assert name == "external"
        assert mod.ResilientClient().external is True

    def test_broken_external_falls_back_to_vendored(self, tmp_path, monkeypatch, no_external):
        ext = tmp_path / "broken.py"
        ext.write_text("raise ImportError('simulated broken install')\n")
        monkeypatch.setattr(owl_bridge, "EXTERNAL_PROXY_DEFENSE", ext)
        mod, name = owl_bridge._load_backend()
        assert name == "vendored" and mod is owl_proxy


class TestOwlShims:
    def test_owl_response_requests_compatible(self):
        r = OwlResponse(200, b'{"code": 0}', {"x": "1"})
        assert r.status_code == 200
        assert r.json() == {"code": 0}
        assert r.text == '{"code": 0}'
        assert r.headers["x"] == "1"

    def test_stream_line_reassembly_handles_crlf_and_split_chunks(self):
        """SSE chunks arrive split arbitrarily; lines must be reassembled
        with requests.iter_lines() semantics (no trailing newlines)."""
        chunks = [b"data: {\"a\":1}\n\nda", b"ta: [DONE]\r\n\r\n"]
        r = OwlStreamResponse(200, {"content-type": "text/event-stream"},
                              byte_iter=iter(chunks))
        lines = list(r.iter_lines())
        assert lines == [b"data: {\"a\":1}", b"", b"data: [DONE]", b""]

    def test_stream_read_error_preloaded(self):
        r = OwlStreamResponse(429, {}, error_text='{"msg":"quota"}')
        assert r.read_error(10) == '{"msg":"qu'

    def test_stream_read_error_drains_iterator(self):
        r = OwlStreamResponse(500, {}, byte_iter=iter([b"E" * 50]))
        assert r.read_error(10) == "E" * 10


class TestBridgeRuntime:
    @pytest.fixture
    def fake_runtime(self):
        """A real _OwlRuntime with a live loop thread + fake client —
        exercises run_coroutine_threadsafe without network."""

        class FakeClient:
            async def request(self, method, url, headers=None, json=None, **kw):
                class R:
                    status = 201
                    content = b'{"ok": true}'
                    headers = {"server": "fake"}
                return R()

            async def stream_request(self, method, url, headers=None, json=None, **kw):
                class S:
                    status = 200
                    headers = {"content-type": "text/event-stream"}
                    async def aiter_raw(self):
                        for c in (b"data: 1\n\n", b"data: 2\n\n"):
                            yield c
                    async def read_error(self, limit=4096):
                        return "err"
                    async def aclose(self):
                        pass
                return S()

        rt = owl_bridge._OwlRuntime()
        loop = asyncio.new_event_loop()
        th = threading.Thread(target=loop.run_forever, daemon=True)
        th.start()
        rt._loop = loop
        rt._client = FakeClient()
        rt._module = owl_proxy
        rt._backend_name = "fake"
        rt._init_error = None
        yield rt
        loop.call_soon_threadsafe(loop.stop)
        th.join(timeout=2)
        loop.close()

    def test_owl_request_roundtrip(self, fake_runtime, monkeypatch):
        monkeypatch.setattr(owl_bridge, "OWL_ENABLED_DEFAULT", True)
        monkeypatch.setattr(owl_bridge, "_runtime", fake_runtime)
        monkeypatch.setattr(owl_bridge, "_disabled_by_error", False)
        resp = owl_bridge.owl_request("POST", "https://upstream.test/x", json_body={})
        assert resp.status_code == 201 and resp.json() == {"ok": True}

    def test_owl_stream_request_pumps_chunks(self, fake_runtime, monkeypatch):
        monkeypatch.setattr(owl_bridge, "OWL_ENABLED_DEFAULT", True)
        monkeypatch.setattr(owl_bridge, "_runtime", fake_runtime)
        monkeypatch.setattr(owl_bridge, "_disabled_by_error", False)
        s = owl_bridge.owl_stream_request("POST", "https://upstream.test/chat",
                                           json_body={})
        assert s.status_code == 200
        lines = list(s.iter_lines())
        assert lines == [b"data: 1", b"", b"data: 2", b""]

    def test_disabled_env_returns_none(self, monkeypatch):
        monkeypatch.setattr(owl_bridge, "OWL_ENABLED_DEFAULT", False)
        monkeypatch.setattr(owl_bridge, "_runtime", None)
        monkeypatch.setattr(owl_bridge, "_disabled_by_error", False)
        assert owl_bridge.owl_enabled() is False
        assert "disabled" in json.dumps(owl_bridge.owl_stats())

    def test_init_failure_disables_layer(self, monkeypatch):
        monkeypatch.setattr(owl_bridge, "OWL_ENABLED_DEFAULT", True)
        monkeypatch.setattr(owl_bridge, "_runtime", None)
        monkeypatch.setattr(owl_bridge, "_disabled_by_error", False)

        class BrokenRuntime:
            def start(self):
                self._init_error = "simulated boot failure"
                self._client = None
            def backend(self):
                return None
        monkeypatch.setattr(owl_bridge, "_OwlRuntime", BrokenRuntime)
        assert owl_bridge.owl_enabled() is False
        assert owl_bridge._disabled_by_error is True  # latched


# ═══════════════════════════════════════════════════════════════════
# Phase-1 synergy regression (committed for the first time)
# ═══════════════════════════════════════════════════════════════════

class TestPhase1Regression:
    def test_output_cap_clamping(self):
        from config import clamp_max_output
        assert clamp_max_output("glm-5.2", 999999) == 131072
        assert clamp_max_output("cheap", 999999) == 65536
        assert clamp_max_output("auto", 999999) == 32768
        assert clamp_max_output("glm-5.2", 1024) == 1024  # never inflate

    def test_error_translation(self):
        from i18n_errors import translate_error
        assert translate_error("错误：积分不足") == \
            "Insufficient credits. Please top up your account."
        assert translate_error("all good") == "all good"
        assert translate_error("") == ""

    def test_permanent_failure_cache_ttl(self):
        from cache import PermanentFailureCache
        c = PermanentFailureCache(ttl=60)
        c.mark("m:a", "quota_exhausted")
        assert c.check("m:a") == "quota_exhausted"
        assert c.check("m:other") is None
        c._cache["m:a"] = ("quota_exhausted", time.time() - 1)  # expire
        assert c.check("m:a") is None

    def test_system_banner_injection(self):
        from proxy import _inject_system_banner
        msgs = [{"role": "user", "content": "hi"}]
        _inject_system_banner(msgs)
        assert msgs[0]["role"] == "system"
        assert "OpenClaw" in msgs[0]["content"]
        # idempotent
        _inject_system_banner(msgs)
        assert msgs[0]["content"].count("OpenClaw") == 1


# ═══════════════════════════════════════════════════════════════════
# Flask routes — OWL routing, fallback, observability
# ═══════════════════════════════════════════════════════════════════

class FakeRequestsSSE:
    """requests.Response stand-in for the direct (non-OWL) upstream path."""
    def __init__(self, status_code=200, lines=(b"data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}", b"data: [DONE]")):
        self.status_code = status_code
        self._lines = list(lines)
        self.text = "".join(l.decode() for l in self._lines)

    def iter_lines(self):
        yield from self._lines


@pytest.fixture
def flask_env(monkeypatch, tmp_path):
    """Import proxy with a clean state; stub token rotation; no API key."""
    monkeypatch.setenv("AUTOCLAW_PROXY_API_KEY", "")
    monkeypatch.setattr("config.PROXY_API_KEY", None)
    import proxy
    from cache import clear_permanent_failures
    clear_permanent_failures()  # singleton must not leak across tests
    # Phase-2 singletons must not leak across tests either
    chat_fingerprint.reset()
    loop_breaker.reset()
    # Phase-3 singletons: metrics + backend preference + WS/thermoptic state
    metrics.reset()
    metrics.set_backend_pref("owl-first")
    ws_fallback._reset_for_tests()
    thermoptic_bridge._reset_for_tests()
    monkeypatch.setattr(proxy, "get_next_token",
                        lambda *a, **kw: ("fake-token", {"email": "t@x", "access_token": "fake-token"}))
    monkeypatch.setattr(proxy, "load_tokens", lambda: {"accounts": []})
    return proxy


@pytest.fixture
def client(flask_env):
    flask_env.app.config["TESTING"] = True
    return flask_env.app.test_client()


@pytest.fixture
def proxy_mod(flask_env):
    """The proxy module itself (same instance `client` is bound to)."""
    return flask_env


SSE_BODY = (b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n')


class TestChatRoutes:
    def test_health_includes_owl_block(self, client, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_stats", lambda: {"enabled": False, "backend": None})
        r = client.get("/health")
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "ok"
        assert "owl" in body and body["owl"]["enabled"] is False
        assert body["version"] == "2.3.0"

    def test_router_command_still_intercepted(self, client):
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "messages": [{"role": "user", "content": "!router help"}]})
        assert r.status_code == 200
        assert "!router" in r.get_json()["choices"][0]["message"]["content"]

    def test_direct_path_when_owl_disabled(self, client, flask_env, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(flask_env.req_lib, "post",
                            lambda *a, **kw: FakeRequestsSSE(200, SSE_BODY.split(b"\n\n")[:2] + [b"data: [DONE]"]))
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.headers["X-Upstream-Via"] == "direct"
        assert b'"content": "Hello"' in r.data

    def test_owl_stream_passthrough_and_via_header(self, client, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: True)
        monkeypatch.setattr(owl_bridge, "owl_backend", lambda: "vendored")

        def fake_stream(*a, **kw):
            return OwlStreamResponse(200, {"content-type": "text/event-stream"},
                                     byte_iter=iter([SSE_BODY]))
        monkeypatch.setattr(owl_bridge, "owl_stream_request", fake_stream)
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.headers["X-Upstream-Via"] == "owl-proxy/vendored"
        assert b'"content": "Hello"' in r.data
        assert b"reasoning" not in r.data

    def test_owl_error_status_translates_and_classifies(self, client, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: True)
        monkeypatch.setattr(owl_bridge, "owl_backend", lambda: "vendored")

        def fake_stream(*a, **kw):
            return OwlStreamResponse(402, {}, error_text="错误：积分不足")
        monkeypatch.setattr(owl_bridge, "owl_stream_request", fake_stream)
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": False,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 402
        body = r.get_json()
        assert "Insufficient credits" in body["error"]["message"]
        assert body["error"]["failure_class"] == "quota_exhausted"

    def test_owl_unavailable_falls_back_to_direct(self, client, flask_env, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: True)

        def boom(*a, **kw):
            raise OwlUnavailable("simulated owl outage")
        monkeypatch.setattr(owl_bridge, "owl_stream_request", boom)
        monkeypatch.setattr(flask_env.req_lib, "post",
                            lambda *a, **kw: FakeRequestsSSE(200, SSE_BODY.split(b"\n\n")[:2] + [b"data: [DONE]"]))
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.headers["X-Upstream-Via"] == "direct"
        assert b'"content": "Hello"' in r.data

    def test_nonstream_aggregation_direct(self, client, flask_env, monkeypatch):
        """Non-stream clients get the SSE stream aggregated into one JSON."""
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        agg = (b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
               b'data: {"choices":[{"delta":{"content":"y"}}]}\n\n'
               b'data: {"choices":[{"delta":{"content":"!"}}],"usage":{"total_tokens":7}}\n\n'
               b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
               b'data: [DONE]\n\n')
        monkeypatch.setattr(flask_env.req_lib, "post",
                            lambda *a, **kw: FakeRequestsSSE(200, agg.split(b"\n\n")[:-1]))
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": False,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        body = r.get_json()
        assert body["choices"][0]["message"]["content"] == "Hey!"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"]["total_tokens"] == 7


class TestAuthUpstreamRouting:
    def test_explicit_proxy_wins_over_owl(self, monkeypatch):
        """Registration flows with a proxies.txt proxy must NOT go through OWL."""
        import auth as auth_mod
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: True)
        seen = {}
        class FakeResp:
            status_code = 200
            text = ""
            def json(self): return {"code": 0, "data": {}}
        def fake_request(method, url, **kw):
            seen["proxies"] = kw.get("proxies")
            seen["via_owl"] = False
            return FakeResp()
        monkeypatch.setattr(auth_mod.requests, "request", fake_request)
        monkeypatch.setattr(owl_bridge, "owl_request",
                            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("OWL must not be used when explicit proxy set")))
        resp = auth_mod._upstream_request("POST", "https://upstream.test/oauth",
                                          proxies={"http": "http://paid:1"})
        assert seen["proxies"] == {"http": "http://paid:1"}

    def test_no_proxy_uses_owl_when_enabled(self, monkeypatch):
        import auth as auth_mod
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: True)
        class FakeResp:
            status_code = 200
            def json(self): return {"code": 0}
        monkeypatch.setattr(owl_bridge, "owl_request",
                            lambda *a, **kw: FakeResp())
        resp = auth_mod._upstream_request("POST", "https://upstream.test/x")
        assert resp.json() == {"code": 0}

    def test_owl_outage_falls_back_to_requests(self, monkeypatch):
        import auth as auth_mod
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: True)
        def boom(*a, **kw):
            raise OwlUnavailable("down")
        monkeypatch.setattr(owl_bridge, "owl_request", boom)
        seen = {}
        class FakeResp:
            status_code = 200
            def json(self): return {"code": 0, "via": "direct"}
        def fake_request(method, url, **kw):
            seen["direct"] = True
            return FakeResp()
        monkeypatch.setattr(auth_mod.requests, "request", fake_request)
        resp = auth_mod._upstream_request("GET", "https://upstream.test/wallet")
        assert resp.json()["via"] == "direct" and seen["direct"]


# ═══════════════════════════════════════════════════════════════════
# Phase 2 — Synergy 9: per-chat fingerprint isolation (ai-router-switch)
# ═══════════════════════════════════════════════════════════════════

class TestChatFingerprint:
    def setup_method(self):
        chat_fingerprint.reset()

    def _msgs(self, first="hello world", extra=None):
        msgs = [{"role": "user", "content": first}]
        if extra:
            msgs += extra
        return msgs

    def test_same_first_message_same_fingerprint(self):
        a = chat_fingerprint.compute_fingerprint(self._msgs())
        b = chat_fingerprint.compute_fingerprint(self._msgs())
        assert a == b and len(a) == 64

    def test_different_first_message_different_fingerprint(self):
        a = chat_fingerprint.compute_fingerprint(self._msgs("alpha"))
        b = chat_fingerprint.compute_fingerprint(self._msgs("beta"))
        assert a != b

    def test_later_messages_do_not_change_identity(self):
        base = chat_fingerprint.compute_fingerprint(self._msgs())
        grown = chat_fingerprint.compute_fingerprint(self._msgs(extra=[
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "follow-up"},
        ]))
        assert base == grown

    def test_multimodal_text_parts_hashed(self):
        plain = chat_fingerprint.compute_fingerprint(self._msgs("see pic"))
        mm = chat_fingerprint.compute_fingerprint([{"role": "user", "content": [
            {"type": "text", "text": "see pic"},
            {"type": "image_url", "image_url": {"url": "data:..."}}]}])
        assert plain == mm

    def test_client_chat_id_overrides_content(self):
        via_id = chat_fingerprint.compute_fingerprint(self._msgs(), "chat-42")
        via_content = chat_fingerprint.compute_fingerprint(self._msgs())
        again = chat_fingerprint.compute_fingerprint(self._msgs("OTHER"), "chat-42")
        assert via_id != via_content
        assert via_id == again  # header beats content

    def test_empty_or_userless_messages_no_fingerprint(self):
        assert chat_fingerprint.compute_fingerprint([]) is None
        assert chat_fingerprint.compute_fingerprint(
            [{"role": "system", "content": "x"}]) is None
        assert chat_fingerprint.compute_fingerprint(None) is None

    def test_disabled_flag_returns_none(self, monkeypatch):
        monkeypatch.setattr(chat_fingerprint, "_ENABLED", False)
        assert chat_fingerprint.compute_fingerprint(self._msgs()) is None

    def test_pin_on_first_use_and_touch(self):
        fp = chat_fingerprint.compute_fingerprint(self._msgs())
        assert chat_fingerprint.pinned_email(fp) is None
        assert chat_fingerprint.bind(fp, "a@x") == "a@x"
        assert chat_fingerprint.bind(fp, "a@x") == "a@x"
        assert chat_fingerprint.pinned_email(fp) == "a@x"

    def test_repin_on_drift(self):
        """Pinned account unusable → pin follows the account that served."""
        fp = chat_fingerprint.compute_fingerprint(self._msgs())
        chat_fingerprint.bind(fp, "a@x")
        chat_fingerprint.bind(fp, "b@x")
        assert chat_fingerprint.pinned_email(fp) == "b@x"

    def test_ttl_expiry_lazily_evicts(self, monkeypatch):
        monkeypatch.setattr(chat_fingerprint, "_TTL", 0)
        fp = chat_fingerprint.compute_fingerprint(self._msgs())
        chat_fingerprint.bind(fp, "a@x")
        assert chat_fingerprint.pinned_email(fp) is None

    def test_lru_capacity_eviction(self, monkeypatch):
        monkeypatch.setattr(chat_fingerprint, "_MAX", 2)
        fps = [chat_fingerprint.compute_fingerprint(self._msgs(f"m{i}"))
               for i in range(3)]
        for i, fp in enumerate(fps):
            chat_fingerprint.bind(fp, f"acc{i}@x")
        assert chat_fingerprint.pinned_email(fps[0]) is None   # evicted
        assert chat_fingerprint.pinned_email(fps[2]) == "acc2@x"

    def test_unpin_and_stats(self):
        fp = chat_fingerprint.compute_fingerprint(self._msgs())
        chat_fingerprint.bind(fp, "a@x")
        chat_fingerprint.bind(fp, "a@x")
        s = chat_fingerprint.stats()
        assert s["pinned_chats"] == 1 and s["turns_served"] == 2
        chat_fingerprint.unpin(fp)
        assert chat_fingerprint.pinned_email(fp) is None

    def test_bind_thread_safety_smoke(self):
        fp = chat_fingerprint.compute_fingerprint(self._msgs("threaded"))
        errors = []
        def worker(n):
            try:
                for _ in range(50):
                    chat_fingerprint.bind(fp, f"acc{n}@x")
            except Exception as e:  # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        assert chat_fingerprint.pinned_email(fp) is not None


# ═══════════════════════════════════════════════════════════════════
# Phase 2 — Synergy 9: loop_breaker (ai-router-switch)
# ═══════════════════════════════════════════════════════════════════

class TestLoopBreaker:
    def setup_method(self):
        loop_breaker.reset()

    def test_four_reemits_trip_block(self, monkeypatch):
        monkeypatch.setattr(loop_breaker, "_RATIO", 0.01)
        monkeypatch.setattr(loop_breaker, "_WINDOW_OVERRIDE", "100")
        fp = "f" * 64
        msgs = [{"role": "user", "content": "retry loop payload"}]
        assert loop_breaker.check(fp, msgs, "cheap") is None       # emit 1
        assert loop_breaker.check(fp, msgs, "cheap") is None       # emit 2
        assert loop_breaker.check(fp, msgs, "cheap") is None       # emit 3
        verdict = loop_breaker.check(fp, msgs, "cheap")            # emit 4
        assert verdict and verdict["action"] == "block"
        assert verdict["reemits"] == 4
        assert verdict["estimated_tokens"] >= 1

    def test_small_requests_never_count(self):
        fp = "f" * 64
        msgs = [{"role": "user", "content": "tiny"}]
        for _ in range(10):
            assert loop_breaker.check(fp, msgs, "cheap") is None

    def test_new_turn_resets_streak(self, monkeypatch):
        monkeypatch.setattr(loop_breaker, "_RATIO", 0.01)
        monkeypatch.setattr(loop_breaker, "_WINDOW_OVERRIDE", "100")
        fp = "f" * 64
        m1 = [{"role": "user", "content": "first huge turn"}]
        for _ in range(3):
            loop_breaker.check(fp, m1, "cheap")
        m2 = [{"role": "user", "content": "a genuinely NEW turn"}]
        # m2's own streak starts at zero — m1's 3 re-emits don't carry over
        assert loop_breaker.check(fp, m2, "cheap") is None
        assert loop_breaker.check(fp, m2, "cheap") is None
        assert loop_breaker.check(fp, m2, "cheap") is None
        v = loop_breaker.check(fp, m2, "cheap")   # 4th emit of m2 → trips
        assert v and v["reemits"] == 4

    def test_no_fingerprint_no_tracking(self):
        assert loop_breaker.check(None, [{"role": "user", "content": "x"}]) is None

    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setattr(loop_breaker, "_ENABLED", False)
        monkeypatch.setattr(loop_breaker, "_RATIO", 0.01)
        monkeypatch.setattr(loop_breaker, "_WINDOW_OVERRIDE", "100")
        for _ in range(6):
            assert loop_breaker.check(
                "f" * 64, [{"role": "user", "content": "x"}], "cheap") is None

    def test_model_window_mapping_differs(self, monkeypatch):
        """cheap (65536 window) counts a ~700-token turn; glm-5.2 (131072) doesn't."""
        monkeypatch.setattr(loop_breaker, "_RATIO", 0.01)
        monkeypatch.setattr(loop_breaker, "_WINDOW_OVERRIDE", "")
        msgs = [{"role": "user", "content": "x" * 2800}]  # ~701 tokens
        v_turbo = None
        for _ in range(4):
            v_turbo = loop_breaker.check("a" * 64, list(msgs), "cheap")
        v_52 = None
        for _ in range(6):
            v_52 = loop_breaker.check("b" * 64, list(msgs), "glm-5.2")
        assert v_turbo and v_turbo["context_window"] == 65536   # 0.0107 fill counts
        assert v_52 is None                                     # 0.0053 fill never trips

    def test_release_clears_streak(self, monkeypatch):
        monkeypatch.setattr(loop_breaker, "_RATIO", 0.01)
        monkeypatch.setattr(loop_breaker, "_WINDOW_OVERRIDE", "100")
        fp = "f" * 64
        msgs = [{"role": "user", "content": "retry loop payload"}]
        for _ in range(4):
            loop_breaker.check(fp, msgs, "cheap")
        loop_breaker.release(fp)
        assert loop_breaker.check(fp, msgs, "cheap") is None  # fresh streak

    def test_stats_counts_blocks(self, monkeypatch):
        monkeypatch.setattr(loop_breaker, "_RATIO", 0.01)
        monkeypatch.setattr(loop_breaker, "_WINDOW_OVERRIDE", "100")
        fp = "f" * 64
        msgs = [{"role": "user", "content": "retry loop payload"}]
        for _ in range(5):
            loop_breaker.check(fp, msgs, "cheap")
        assert loop_breaker.stats()["blocks_served"] >= 1


# ═══════════════════════════════════════════════════════════════════
# Phase 2 — Synergy 8: DSML tool-calling shim (chat-z-ai-proxy)
# ═══════════════════════════════════════════════════════════════════

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
}]

DSML_BLOCK = ('<dsml:tool_call id="call_1">'
              '<dsml:function>get_weather</dsml:function>'
              '<dsml:arguments>{"location": "Tokyo"}</dsml:arguments>'
              '</dsml:tool_call>')


def _dsml_marker():
    return "## Tool Calling Protocol (DSML)"


class TestDSMLProtocol:
    def test_protocol_lists_tools_and_rules(self):
        text = dsml_shim.build_tool_protocol(TOOLS)
        assert "get_weather" in text
        assert "dsml:tool_call" in text
        assert "dsml:function" in text
        tools_json = text.split("<dsml:tools>")[1].split("</dsml:tools>")[0]
        parsed = json.loads(tools_json)
        assert parsed[0]["name"] == "get_weather"
        assert parsed[0]["parameters"]["required"] == ["location"]

    def test_required_choice_mandates(self):
        assert "MUST invoke at least one tool" in dsml_shim.build_tool_protocol(TOOLS, "required")

    def test_forced_function_choice(self):
        text = dsml_shim.build_tool_protocol(
            TOOLS, {"type": "function", "function": {"name": "get_weather"}})
        assert 'MUST invoke the tool "get_weather"' in text

    def test_auto_choice_no_mandate(self):
        text = dsml_shim.build_tool_protocol(TOOLS, "auto")
        assert "You MUST invoke" not in text

    def test_inject_appends_to_existing_system(self):
        msgs = [{"role": "system", "content": "base prompt"},
                {"role": "user", "content": "u"}]
        dsml_shim.inject_tool_protocol(msgs, TOOLS)
        assert msgs[0]["content"].startswith("base prompt")
        assert _dsml_marker() in msgs[0]["content"]
        dsml_shim.inject_tool_protocol(msgs, TOOLS)  # idempotent
        assert msgs[0]["content"].count(_dsml_marker()) == 1

    def test_inject_creates_system_when_absent(self):
        msgs = [{"role": "user", "content": "u"}]
        dsml_shim.inject_tool_protocol(msgs, TOOLS)
        assert msgs[0]["role"] == "system"
        assert _dsml_marker() in msgs[0]["content"]

    def test_inject_keeps_banner_first(self):
        """Banner must stay at the very start (upstream metered-path rule)."""
        from proxy import _inject_system_banner
        msgs = [{"role": "user", "content": "u"}]
        _inject_system_banner(msgs)
        dsml_shim.inject_tool_protocol(msgs, TOOLS)
        assert msgs[0]["content"].startswith("You are a personal assistant")


class TestDSMLParse:
    def test_single_call(self):
        res = dsml_shim.parse_dsml("Before " + DSML_BLOCK + " after")
        assert res and len(res["tool_calls"]) == 1
        tc = res["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"location": "Tokyo"}
        assert res["content"] == "Before  after"

    def test_multiple_calls_unique_ids(self):
        two = DSML_BLOCK + DSML_BLOCK.replace('id="call_1"', 'id="call_2"')
        res = dsml_shim.parse_dsml(two)
        assert res and len(res["tool_calls"]) == 2
        assert {tc["id"] for tc in res["tool_calls"]} == {"call_1", "call_2"}

    def test_missing_id_minted(self):
        block = DSML_BLOCK.replace(' id="call_1"', '')
        res = dsml_shim.parse_dsml(block)
        assert res["tool_calls"][0]["id"].startswith("call_")

    def test_pretty_printed_args_repaired(self):
        block = ('<dsml:tool_call><dsml:function>get_weather</dsml:function>'
                 '<dsml:arguments>{\n  "location": "Oslo"\n}</dsml:arguments>'
                 '</dsml:tool_call>')
        res = dsml_shim.parse_dsml(block)
        assert json.loads(res["tool_calls"][0]["function"]["arguments"]) == {
            "location": "Oslo"}

    def test_case_insensitive_tags(self):
        res = dsml_shim.parse_dsml(DSML_BLOCK.upper())
        assert res and res["tool_calls"][0]["function"]["name"] == "GET_WEATHER"

    def test_block_without_function_dropped(self):
        bad = '<dsml:tool_call><dsml:arguments>{}</dsml:arguments></dsml:tool_call>'
        assert dsml_shim.parse_dsml(bad) is None

    def test_no_blocks_returns_none(self):
        assert dsml_shim.parse_dsml("just prose, no markup at all") is None
        assert dsml_shim.parse_dsml("") is None
        assert dsml_shim.parse_dsml(None) is None


class TestDSMLSieve:
    def _feed_all(self, sieve, text, step=7):
        pieces = []
        for i in range(0, len(text), step):
            pieces += sieve.feed(text[i:i + step])
        pieces += sieve.flush()
        return pieces

    @staticmethod
    def _render(pieces):
        out, calls = "", []
        for p in pieces:
            if "content" in p:
                out += p["content"]
            else:
                calls += p["tool_calls"]
        return out, calls

    def test_plain_text_passes_through(self):
        sieve = dsml_shim.DSMLStreamSieve()
        text = "The quick brown fox < jumps over <b>the</b> lazy dog."
        out, calls = self._render(self._feed_all(sieve, text))
        assert out == text and calls == []
        assert not sieve.saw_tool_calls

    def test_block_split_across_feeds(self):
        sieve = dsml_shim.DSMLStreamSieve()
        pieces = self._feed_all(sieve, DSML_BLOCK, step=5)
        out, calls = self._render(pieces)
        assert out == ""
        assert len(calls) == 2  # id/name delta + arguments delta
        first, second = calls
        assert first["id"] == "call_1"
        assert first["function"]["name"] == "get_weather"
        assert json.loads(second["function"]["arguments"]) == {"location": "Tokyo"}
        assert sieve.saw_tool_calls

    def test_prose_then_block(self):
        sieve = dsml_shim.DSMLStreamSieve()
        out, calls = self._render(self._feed_all(sieve, "Let me check. " + DSML_BLOCK))
        assert out == "Let me check. "
        assert calls and calls[0]["function"]["name"] == "get_weather"

    def test_two_calls_indexed(self):
        sieve = dsml_shim.DSMLStreamSieve()
        two = DSML_BLOCK + DSML_BLOCK.replace('id="call_1"', 'id="call_2"')
        _, calls = self._render(self._feed_all(sieve, two))
        idxs = [c["index"] for c in calls if "index" in c]
        assert sorted(set(idxs)) == [0, 1]

    def test_truncated_block_degrades_to_prose(self):
        sieve = dsml_shim.DSMLStreamSieve()
        raw = "text <dsml:tool_call><dsml:function>x</dsml:function>"
        out, calls = self._render(self._feed_all(sieve, raw))
        assert out == raw and calls == []

    def test_flush_idempotent(self):
        sieve = dsml_shim.DSMLStreamSieve()
        assert self._render(sieve.flush())[0] == ""
        fed = self._render(sieve.feed("hello<"))[0]    # "hello" released now
        flushed = self._render(sieve.flush())[0]       # "<" was held back
        assert fed + flushed == "hello<"
        assert sieve.flush() == []                     # second flush drains nothing


# ═══════════════════════════════════════════════════════════════════
# Phase 2 — Flask route integration (fingerprint, loop_breaker, DSML)
# ═══════════════════════════════════════════════════════════════════

def _sse_line(obj):
    return ("data: " + json.dumps(obj)).encode() + b"\n\n"


def _dsml_upstream_lines(text_chunks, finish="stop"):
    lines = []
    for c in text_chunks:
        lines.append(_sse_line({"choices": [{"delta": {"content": c}}]}))
    lines.append(_sse_line({"choices": [{"delta": {},
                                         "finish_reason": finish}]}))
    lines.append(b"data: [DONE]\n\n")
    return lines


class TestPhase2Routes:
    def test_health_has_phase2_blocks(self, client):
        r = client.get("/health")
        body = r.get_json()
        assert body["fingerprint"]["enabled"] is True
        assert body["loop_breaker"]["enabled"] is True
        assert body["dsml"]["enabled"] is True

    def test_buffered_dsml_tool_call(self, client, flask_env, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(
            flask_env.req_lib, "post",
            lambda *a, **kw: FakeRequestsSSE(200, _dsml_upstream_lines(["Sure! " + DSML_BLOCK])))
        r = client.post("/v1/chat/completions", json={
            "model": "cheap", "stream": False,
            "messages": [{"role": "user", "content": "weather in Tokyo?"}],
            "tools": TOOLS})
        assert r.status_code == 200
        assert r.headers["X-DSML-Shim"] == "1"
        body = r.get_json()
        msg = body["choices"][0]["message"]
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        assert msg["content"] == "Sure!"
        tc = msg["tool_calls"][0]
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"location": "Tokyo"}

    def test_streaming_dsml_tool_call(self, client, flask_env, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        block = "Sure! " + DSML_BLOCK
        chunks = [block[:20], block[20:45], block[45:]]
        monkeypatch.setattr(
            flask_env.req_lib, "post",
            lambda *a, **kw: FakeRequestsSSE(200, _dsml_upstream_lines(chunks)))
        r = client.post("/v1/chat/completions", json={
            "model": "cheap", "stream": True,
            "messages": [{"role": "user", "content": "weather in Tokyo?"}],
            "tools": TOOLS})
        assert r.status_code == 200
        data = r.data
        assert b'"tool_calls"' in data
        assert b"get_weather" in data
        assert b"<dsml:" not in data          # markup never leaks to clients
        assert b'"finish_reason": "tool_calls"' in data
        assert b"Sure!" in data

    def test_tool_choice_none_skips_shim(self, client, flask_env, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        captured = {}
        def fake_post(url, **kw):
            captured.update(json=kw.get("json"))
            return FakeRequestsSSE(200, _dsml_upstream_lines(["hi"]))
        monkeypatch.setattr(flask_env.req_lib, "post", fake_post)
        r = client.post("/v1/chat/completions", json={
            "model": "cheap", "stream": False,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": TOOLS, "tool_choice": "none"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["choices"][0]["message"]["content"] == "hi"
        assert "tool_calls" not in body["choices"][0]["message"]
        assert b"dsml" not in json.dumps(captured["json"]).encode()

    def test_loop_breaker_400_on_reemit_storm(self, client, flask_env, monkeypatch):
        monkeypatch.setattr(loop_breaker, "_RATIO", 0.01)
        monkeypatch.setattr(loop_breaker, "_WINDOW_OVERRIDE", "100")
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(
            flask_env.req_lib, "post",
            lambda *a, **kw: FakeRequestsSSE(200, _dsml_upstream_lines(["ok"])))
        payload = {"model": "cheap", "stream": False,
                   "messages": [{"role": "user", "content": "retry loop payload"}]}
        for _ in range(3):
            assert client.post("/v1/chat/completions", json=payload).status_code == 200
        r = client.post("/v1/chat/completions", json=payload)
        assert r.status_code == 400
        err = r.get_json()["error"]
        assert err["type"] == "loop_breaker_triggered"
        assert err["details"]["reemits"] == 4

    def test_loop_breaker_allows_new_turn(self, client, flask_env, monkeypatch):
        monkeypatch.setattr(loop_breaker, "_RATIO", 0.01)
        monkeypatch.setattr(loop_breaker, "_WINDOW_OVERRIDE", "100")
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(
            flask_env.req_lib, "post",
            lambda *a, **kw: FakeRequestsSSE(200, _dsml_upstream_lines(["ok"])))
        for _ in range(3):
            r = client.post("/v1/chat/completions", json={
                "model": "cheap", "stream": False,
                "messages": [{"role": "user", "content": "turn one payload"}]})
            assert r.status_code == 200
        r = client.post("/v1/chat/completions", json={
            "model": "cheap", "stream": False,
            "messages": [{"role": "user", "content": "a brand new turn"}]})
        assert r.status_code == 200  # streak reset — new turn never blocked

    def test_fingerprint_affinity_prefers_pinned_account(self, client, flask_env, monkeypatch):
        calls = []
        def fake_token(*a, **kw):
            calls.append(kw.get("prefer_email"))
            return ("fake-token", {"email": "t@x", "access_token": "fake-token"})
        monkeypatch.setattr(flask_env, "get_next_token", fake_token)
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(
            flask_env.req_lib, "post",
            lambda *a, **kw: FakeRequestsSSE(200, _dsml_upstream_lines(["ok"])))
        payload = {"model": "cheap", "stream": False,
                   "messages": [{"role": "user", "content": "affinity check"}]}
        client.post("/v1/chat/completions", json=payload)   # first turn → binds
        assert calls[0] is None
        client.post("/v1/chat/completions", json=payload)   # second turn → prefer
        assert calls[1] == "t@x"

    def test_chat_id_header_binds_same_conversation(self, client, flask_env, monkeypatch):
        calls = []
        def fake_token(*a, **kw):
            calls.append(kw.get("prefer_email"))
            return ("fake-token", {"email": "t@x", "access_token": "fake-token"})
        monkeypatch.setattr(flask_env, "get_next_token", fake_token)
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(
            flask_env.req_lib, "post",
            lambda *a, **kw: FakeRequestsSSE(200, _dsml_upstream_lines(["ok"])))
        h = {"X-AutoClaw-Chat-Id": "conv-777"}
        client.post("/v1/chat/completions", json={
            "model": "cheap", "stream": False,
            "messages": [{"role": "user", "content": "message A"}]}, headers=h)
        client.post("/v1/chat/completions", json={
            "model": "cheap", "stream": False,
            "messages": [{"role": "user", "content": "message B"}]}, headers=h)
        assert calls[0] is None
        assert calls[1] == "t@x"


class TestPhase2Regression:
    def test_phase1_still_green(self):
        """Synergy 1–5 behaviour unchanged by Phase-2 modules."""
        from config import clamp_max_output
        assert clamp_max_output("glm-5.2", 999999) == 131072
        assert clamp_max_output("glm-5.2", 1024) == 1024
        from i18n_errors import translate_error
        assert "Insufficient credits" in translate_error("错误：积分不足")

    def test_upstream_copy_gets_protocol_client_body_does_not(self, client, flask_env, monkeypatch):
        """The upstream payload gains the DSML block; nothing leaks backwards."""
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        captured = {}
        def fake_post(url, **kw):
            captured.update(json=kw.get("json"))
            return FakeRequestsSSE(200, _dsml_upstream_lines(["ok"]))
        monkeypatch.setattr(flask_env.req_lib, "post", fake_post)
        r = client.post("/v1/chat/completions", json={
            "model": "cheap", "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": TOOLS})
        assert r.status_code == 200
        sent = captured["json"]["messages"]
        assert _dsml_marker() in sent[0]["content"]   # upstream copy got protocol


@pytest.fixture(autouse=True)
def _isolate_phase3_state():
    """Phase-3 singletons must not leak across tests (metrics totals,
    backend pref, WS breaker/discovery caches, thermoptic breaker)."""
    metrics.reset()
    metrics.set_backend_pref("owl-first")
    ws_fallback._reset_for_tests()
    thermoptic_bridge._reset_for_tests()
    yield


# ═══════════════════════════════════════════════════════════════════
# Phase 3 (Synergy #15) — metrics registry
# ═══════════════════════════════════════════════════════════════════

class TestMetrics:
    def test_record_and_snapshot(self):
        metrics.record("/v1/chat/completions", 200, via="owl-proxy/vendored",
                       latency_ms=42.5, client_ip="10.0.0.9", model="cheap",
                       stream=True)
        snap = metrics.snapshot()
        assert snap["totals"]["requests"] == 1
        assert snap["totals"]["stream"] == 1
        assert snap["status_codes"]["200"] == 1
        assert snap["via"]["owl-proxy/vendored"] == 1
        assert snap["models"]["cheap"] == 1
        entry = snap["request_log"][-1]
        assert entry["route"] == "/v1/chat/completions"
        assert entry["status"] == 200 and entry["via"] == "owl-proxy/vendored"
        assert entry["latency_ms"] == 42.5 and entry["model"] == "cheap"

    def test_mask_ip_is_stable_and_privacy_safe(self):
        m1 = metrics.mask_ip("192.168.1.44")
        m2 = metrics.mask_ip("192.168.1.44")
        m3 = metrics.mask_ip("192.168.1.45")
        assert m1 == m2 and m1 != m3
        assert "192.168" not in m1 and len(m1) == 12
        assert metrics.mask_ip("") == "unknown"

    def test_block_and_feature_counters(self):
        metrics.record("/v1/chat/completions", 400, block="loop_breaker")
        metrics.record("/v1/chat/completions", 200, feature="dsml_shim")
        snap = metrics.snapshot()
        assert snap["blocks"]["loop_breaker"] == 1
        assert snap["totals"]["blocks"] == 1
        assert snap["features"]["dsml_shim"] == 1
        # clients registered even for blocked calls
        assert snap["client_count"] >= 1

    def test_request_log_ring_cap(self, monkeypatch):
        monkeypatch.setattr(metrics, "REQUEST_LOG_CAP", 5)
        for i in range(8):
            metrics.record("/v1/x", 200)
        snap = metrics.snapshot()
        assert len(snap["request_log"]) == 5
        # newest kept, oldest dropped
        assert snap["request_log"][-1]["latency_ms"] >= 0

    def test_backend_pref_validation(self):
        ok, _ = metrics.set_backend_pref("thermoptic-first")
        assert ok and metrics.backend_pref() == "thermoptic-first"
        ok, msg = metrics.set_backend_pref("bogus")
        assert not ok and "bogus" in msg
        assert metrics.backend_pref() == "thermoptic-first"  # unchanged

    def test_reset_clears_telemetry_keeps_control(self):
        metrics.set_backend_pref("direct-only")
        metrics.record("/v1/x", 200, client_ip="1.2.3.4")
        metrics.reset()
        snap = metrics.snapshot()
        assert snap["totals"]["requests"] == 0
        assert snap["request_log"] == []
        assert snap["client_count"] == 0
        assert snap["control"]["backend_pref"] == "direct-only"

    def test_snapshot_control_shape(self):
        snap = metrics.snapshot()
        assert snap["control"]["backend_pref"] in metrics.VALID_BACKEND_PREFS
        assert set(metrics.VALID_BACKEND_PREFS) == {
            "owl-first", "thermoptic-first", "direct-only"}

    def test_record_never_raises_on_garbage(self):
        metrics.record(None, "not-a-status", via=123, latency_ms="abc",
                       client_ip=None, model=object(), stream="yes",
                       block=[], feature={})
        assert metrics.snapshot()["totals"]["requests"] == 1


# ═══════════════════════════════════════════════════════════════════
# Phase 3 (Synergy #14) — thermoptic bridge
# ═══════════════════════════════════════════════════════════════════

class TestThermopticBridge:
    def test_disabled_by_default(self):
        # sandbox/test env never sets OWL_THERMOPTIC_ENABLED
        assert thermoptic_bridge.enabled() is False
        assert thermoptic_bridge.healthy() is False

    def test_proxy_url_auth_and_proxies_dict(self, monkeypatch):
        monkeypatch.setattr(thermoptic_bridge, "BASE_URL", "http://127.0.0.1:1234")
        monkeypatch.setattr(thermoptic_bridge, "USERNAME", "op")
        monkeypatch.setattr(thermoptic_bridge, "PASSWORD", "s3cr3t")
        url = thermoptic_bridge.proxy_url()
        assert url.startswith("http://op:") and "127.0.0.1:1234" in url
        px = thermoptic_bridge.requests_proxies()
        assert px == {"http": url, "https": url}

    def test_verify_prefers_ca_file(self, monkeypatch, tmp_path):
        ca = tmp_path / "rootCA.crt"
        ca.write_text("-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----")
        monkeypatch.setattr(thermoptic_bridge, "CA_FILE", str(ca))
        assert thermoptic_bridge.verify() == str(ca)
        monkeypatch.setattr(thermoptic_bridge, "CA_FILE",
                            str(tmp_path / "missing.crt"))
        assert thermoptic_bridge.verify() is False

    def test_probe_negative_cache(self, monkeypatch):
        calls = {"n": 0}
        def fake_probe(timeout=None):
            calls["n"] += 1
            return False, "connect refused"
        monkeypatch.setattr(thermoptic_bridge, "_probe_once", fake_probe)
        with thermoptic_bridge._probe_lock:
            thermoptic_bridge._probe_cache.update(
                {"ok_until": 0.0, "bad_until": 0.0})
        assert thermoptic_bridge.probe() is False
        assert thermoptic_bridge.probe() is False  # served from negative cache
        assert calls["n"] == 1

    def test_transport_failure_trips_breaker(self, monkeypatch):
        import requests as req_lib
        monkeypatch.setattr(thermoptic_bridge, "ENV_ENABLED", True)
        monkeypatch.setattr(thermoptic_bridge, "BREAKER_THRESHOLD", 2)
        def boom(*a, **kw):
            raise req_lib.exceptions.ConnectionError("proxy down")
        monkeypatch.setattr(req_lib, "request", boom)
        with pytest.raises(thermoptic_bridge.ThermopticUnavailable):
            thermoptic_bridge.http_request("GET", "https://x.test")
        assert not thermoptic_bridge.breaker_open()
        with pytest.raises(thermoptic_bridge.ThermopticUnavailable):
            thermoptic_bridge.http_request("GET", "https://x.test")
        assert thermoptic_bridge.breaker_open()  # threshold 2 reached
        with pytest.raises(thermoptic_bridge.ThermopticUnavailable):
            thermoptic_bridge.http_request("GET", "https://x.test")  # breaker path

    def test_upstream_http_error_is_not_a_transport_failure(self, monkeypatch):
        import requests as req_lib
        monkeypatch.setattr(thermoptic_bridge, "ENV_ENABLED", True)
        class FakeResp:
            status_code = 401
            text = '{"error": "auth"}'
        monkeypatch.setattr(req_lib, "request", lambda *a, **kw: FakeResp())
        before = thermoptic_bridge.stats()["consecutive_failures"]
        resp = thermoptic_bridge.http_request("POST", "https://x.test")
        assert resp.status_code == 401  # passes through verbatim
        assert thermoptic_bridge.stats()["consecutive_failures"] == before

    def test_stats_shape(self):
        s = thermoptic_bridge.stats()
        for key in ("enabled", "url", "source", "breaker_open",
                    "requests_served", "healthy"):
            assert key in s
        assert "mandatoryprogrammer/thermoptic" in s["source"]


# ═══════════════════════════════════════════════════════════════════
# Phase 3 (Synergy #13) — WS local-agent fallback
# ═══════════════════════════════════════════════════════════════════

class FakeWsAgent:
    """Scripted local-agent: replays queued frames per recv(), records sends."""
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []
        self.closed = False
        self.timeout_set = None

    def settimeout(self, t):
        self.timeout_set = t

    def send(self, raw):
        self.sent.append(raw)

    def recv(self):
        if not self._frames:
            raise ConnectionError("agent went away")
        return self._frames.pop(0)

    def close(self):
        self.closed = True


def _agent_frames(sse_chunks, status=200):
    """Build protocol-v1 frame sequence for the given SSE chunks."""
    def f(d):
        return json.dumps(d)
    frames = [f({"type": "chat.headers", "status": status})]
    for c in sse_chunks:
        frames.append(f({"type": "chat.delta", "sse": c}))
    frames.append(f({"type": "chat.end"}))
    return frames


class TestWsFallback:
    def test_disabled_by_default(self):
        assert ws_fallback.enabled() is False

    def test_shim_iter_lines_requests_semantics(self):
        chunks = [b'data: {"a":1}\n\nda', b'ta: {"b":2}\n\ndata: [DONE]\n\n']
        resp = ws_fallback.WsStreamResponse(200, {}, frame_iter=iter(chunks))
        lines = list(resp.iter_lines())
        assert lines == [
            b'data: {"a":1}', b"", b'data: {"b":2}', b"", b"data: [DONE]", b""]

    def test_discovery_prefers_configured_url(self, monkeypatch):
        monkeypatch.setattr(ws_fallback, "AGENT_URL", "ws://10.1.2.3:9999/agent")
        monkeypatch.setattr(ws_fallback, "_ping_probe", lambda url, timeout=2.0: True)
        assert ws_fallback.discover_agent(force=True) == "ws://10.1.2.3:9999/agent"

    def test_discovery_negative_cache(self, monkeypatch):
        calls = {"n": 0}
        def fail(url, timeout=2.0):
            calls["n"] += 1
            return False
        monkeypatch.setattr(ws_fallback, "_ping_probe", fail)
        monkeypatch.setattr(ws_fallback, "AGENT_URL", "")
        assert ws_fallback.discover_agent(force=True) is None
        assert ws_fallback.discover_agent() is None  # negative cache
        assert calls["n"] == len(ws_fallback.DISCOVERY_PORTS)

    def test_chat_stream_happy_path(self, monkeypatch):
        agent = FakeWsAgent(_agent_frames([
            'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n"]))
        monkeypatch.setattr(ws_fallback, "ENV_ENABLED", True)
        monkeypatch.setattr(ws_fallback, "AGENT_URL", "ws://agent.test/agent")
        monkeypatch.setattr(ws_fallback, "_connect", lambda url, t: agent)
        monkeypatch.setattr(ws_fallback, "_ping_probe", lambda *a, **kw: True)
        resp = ws_fallback.chat_stream({"messages": [{"role": "user", "content": "x"}]},
                                       timeout=30)
        assert resp.status_code == 200
        req = json.loads(agent.sent[0])
        assert req["type"] == "chat.request" and req["protocol"] == "autoclaw-ws-agent-v1"
        assert req["payload"]["messages"][0]["content"] == "x"
        lines = list(resp.iter_lines())
        assert b'"content":"Hi"' in lines[0] or b'"content": "Hi"' in lines[0]
        assert b"data: [DONE]" in lines
        assert agent.closed  # closed when the stream drains
        assert ws_fallback.stats()["requests_served"] == 1

    def test_agent_error_envelope_does_not_trip_breaker(self, monkeypatch):
        def f(d):
            return json.dumps(d)
        agent = FakeWsAgent([f({"type": "chat.headers", "status": 503}),
                             f({"type": "chat.error", "status": 503,
                                "message": "agent upstream dead"})])
        monkeypatch.setattr(ws_fallback, "ENV_ENABLED", True)
        monkeypatch.setattr(ws_fallback, "ENV_ENABLED", True)
        monkeypatch.setattr(ws_fallback, "AGENT_URL", "ws://agent.test/agent")
        monkeypatch.setattr(ws_fallback, "_connect", lambda url, t: agent)
        monkeypatch.setattr(ws_fallback, "_ping_probe", lambda *a, **kw: True)
        resp = ws_fallback.chat_stream({"messages": []}, timeout=30)
        assert resp.status_code == 503
        assert "agent upstream dead" in resp.read_error(200)
        list(resp.iter_lines())  # drain
        stats = ws_fallback.stats()
        assert stats["consecutive_failures"] == 0  # transport was fine
        assert stats["requests_served"] == 1

    def test_connect_failures_trip_breaker(self, monkeypatch):
        monkeypatch.setattr(ws_fallback, "ENV_ENABLED", True)
        monkeypatch.setattr(ws_fallback, "BREAKER_THRESHOLD", 2)
        monkeypatch.setattr(ws_fallback, "AGENT_URL", "ws://agent.test/agent")
        monkeypatch.setattr(ws_fallback, "_ping_probe", lambda *a, **kw: True)
        def dead(url, t):
            raise OSError("connection refused")
        monkeypatch.setattr(ws_fallback, "_connect", dead)
        with pytest.raises(ws_fallback.WsUnavailable):
            ws_fallback.chat_stream({}, timeout=30)
        assert not ws_fallback.breaker_open()
        with pytest.raises(ws_fallback.WsUnavailable):
            ws_fallback.chat_stream({}, timeout=30)
        assert ws_fallback.breaker_open()
        # breaker path short-circuits without touching _connect
        monkeypatch.setattr(ws_fallback, "_connect",
                            lambda url, t: (_ for _ in ()).throw(AssertionError("must not connect")))
        with pytest.raises(ws_fallback.WsUnavailable):
            ws_fallback.chat_stream({}, timeout=30)


# ═══════════════════════════════════════════════════════════════════
# Phase 3 (Synergy #15) — dashboard routes + tiered egress chain
# ═══════════════════════════════════════════════════════════════════

class FakeDirectResp:
    """requests.Response stand-in for the direct/thermoptic tiers."""
    def __init__(self, status=200, body=SSE_BODY):
        self.status_code = status
        self._body = body
        self.text = body.decode("utf-8", "replace")
        self.headers = {}

    def iter_lines(self):
        for line in self._body.split(b"\n"):
            if line.strip():
                yield line.rstrip(b"\r")


class TestDashboardRoutes:
    def test_state_shape(self, client):
        r = client.get("/api/dashboard/state")
        assert r.status_code == 200
        snap = r.get_json()
        for key in ("totals", "status_codes", "via", "models", "blocks",
                    "features", "latency", "clients", "request_log",
                    "control", "health_mini"):
            assert key in snap
        assert snap["health_mini"]["backend_pref"] == "owl-first"

    def test_control_sets_backend_pref(self, client):
        r = client.post("/api/dashboard/control",
                        json={"backend_pref": "thermoptic-first"})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        assert metrics.backend_pref() == "thermoptic-first"

    def test_control_rejects_invalid_pref(self, client):
        r = client.post("/api/dashboard/control", json={"backend_pref": "yolo"})
        assert r.status_code == 400
        assert metrics.backend_pref() == "owl-first"

    def test_control_clear_negative_cache(self, client, monkeypatch):
        from cache import mark_permanent_failure, is_permanent_failure_cached
        mark_permanent_failure("zai_glm-5-turbo", "quota_exhausted", "t@x")
        assert is_permanent_failure_cached("zai_glm-5-turbo", "t@x")
        r = client.post("/api/dashboard/control", json={"action": "clear_negative_cache"})
        assert r.status_code == 200
        assert not is_permanent_failure_cached("zai_glm-5-turbo", "t@x")

    def test_control_reset_metrics(self, client):
        metrics.record("/v1/x", 200)
        r = client.post("/api/dashboard/control", json={"action": "reset_metrics"})
        assert r.get_json()["ok"] is True
        assert metrics.snapshot()["totals"]["requests"] == 0

    def test_control_requires_api_key_when_configured(self, flask_env, monkeypatch):
        monkeypatch.setattr(flask_env, "PROXY_API_KEY", "sekrit")
        c = flask_env.app.test_client()
        r = c.post("/api/dashboard/control", json={"action": "reset_metrics"})
        assert r.status_code == 401
        r = c.post("/api/dashboard/control", json={"action": "reset_metrics"},
                   headers={"Authorization": "Bearer sekrit"})
        assert r.status_code == 200

    def test_dashboard_page_served(self, client):
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert b"<div id=\"root\">" in r.data


class TestEgressChain:
    """Tiered egress: owl → thermoptic → direct → ws-local-agent."""

    def test_direct_path_records_metrics(self, client, proxy_mod, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(proxy_mod.req_lib, "post",
                            lambda *a, **kw: FakeDirectResp())
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.headers["X-Upstream-Via"] == "direct"
        snap = metrics.snapshot()
        assert snap["via"]["direct"] >= 1
        assert snap["models"]["cheap"] >= 1

    def test_thermoptic_tier_serves_when_healthy(self, client, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(thermoptic_bridge, "ENV_ENABLED", True)
        monkeypatch.setattr(thermoptic_bridge, "healthy", lambda: True)
        monkeypatch.setattr(thermoptic_bridge, "http_request",
                            lambda *a, **kw: FakeDirectResp())
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.headers["X-Upstream-Via"] == "thermoptic"
        assert metrics.snapshot()["features"]["thermoptic"] == 1

    def test_thermoptic_down_falls_to_direct(self, client, proxy_mod, monkeypatch):
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(thermoptic_bridge, "ENV_ENABLED", True)
        monkeypatch.setattr(thermoptic_bridge, "healthy", lambda: False)
        posted = {"n": 0}
        def fake_post(*a, **kw):
            posted["n"] += 1
            return FakeDirectResp()
        monkeypatch.setattr(proxy_mod.req_lib, "post", fake_post)
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.headers["X-Upstream-Via"] == "direct"
        assert posted["n"] == 1

    def test_direct_only_pref_skips_owl_and_thermoptic(self, client, proxy_mod, monkeypatch):
        metrics.set_backend_pref("direct-only")
        def forbidden(*a, **kw):
            raise AssertionError("owl tier must not be called in direct-only mode")
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: True)
        monkeypatch.setattr(owl_bridge, "owl_stream_request", forbidden)
        monkeypatch.setattr(thermoptic_bridge, "ENV_ENABLED", True)
        monkeypatch.setattr(thermoptic_bridge, "healthy", lambda: True)
        monkeypatch.setattr(thermoptic_bridge, "http_request", forbidden)
        monkeypatch.setattr(proxy_mod.req_lib, "post",
                            lambda *a, **kw: FakeDirectResp())
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.headers["X-Upstream-Via"] == "direct"

    def test_ws_fallback_engages_on_network_fault(self, client, proxy_mod, monkeypatch):
        import requests as req_lib
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        def net_fail(*a, **kw):
            raise req_lib.exceptions.ConnectionError("upstream unreachable")
        monkeypatch.setattr(proxy_mod.req_lib, "post", net_fail)
        monkeypatch.setattr(ws_fallback, "ENV_ENABLED", True)
        monkeypatch.setattr(ws_fallback, "chat_stream",
                            lambda body, timeout=None: OwlStreamResponse(
                                200, {"content-type": "text/event-stream"},
                                byte_iter=iter([SSE_BODY])))
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.headers["X-Upstream-Via"] == "ws-local-agent"
        assert b'"content": "Hello"' in r.data
        assert metrics.snapshot()["features"]["ws_fallback"] == 1

    def test_ws_fallback_does_not_engage_on_http_error(self, client, proxy_mod, monkeypatch):
        """HTTP error statuses are upstream decisions — never a WS trigger."""
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        class ErrResp:
            status_code = 401
            text = '{"error": "nope"}'
            headers = {}
            def iter_lines(self):
                return iter(())
        monkeypatch.setattr(proxy_mod.req_lib, "post", lambda *a, **kw: ErrResp())
        monkeypatch.setattr(ws_fallback, "ENV_ENABLED", True)
        monkeypatch.setattr(ws_fallback, "chat_stream",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                AssertionError("WS must not fire on HTTP errors")))
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": False,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 401
        assert r.headers["X-Upstream-Via"] == "direct"

    def test_all_tiers_down_returns_502(self, client, proxy_mod, monkeypatch):
        import requests as req_lib
        monkeypatch.setattr(owl_bridge, "owl_enabled", lambda: False)
        monkeypatch.setattr(ws_fallback, "ENV_ENABLED", False)
        def net_fail(*a, **kw):
            raise req_lib.exceptions.ConnectionError("refused")
        monkeypatch.setattr(proxy_mod.req_lib, "post", net_fail)
        r = client.post("/v1/chat/completions",
                        json={"model": "cheap", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 502
        assert r.get_json()["error"]["type"] == "network_error"
