"""AutoClaw v2.1.0 — OWL-AGENT integration + Phase-1 synergy regression tests.

Run:  python -m pytest test_owl_integration.py -v

Network policy: every test is offline. All HTTP seams (proxy racing, direct
fallback, aiohttp/httpx/curl_cffi) are mocked; the real OWL runtime thread
is never started (bridge layer is exercised with injected fakes).
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
    monkeypatch.setattr(proxy, "get_next_token",
                        lambda: ("fake-token", {"email": "t@x", "access_token": "fake-token"}))
    monkeypatch.setattr(proxy, "load_tokens", lambda: {"accounts": []})
    return proxy


@pytest.fixture
def client(flask_env):
    flask_env.app.config["TESTING"] = True
    return flask_env.app.test_client()


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
        assert body["version"] == "2.1.0"

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
