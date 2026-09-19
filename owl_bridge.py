"""AutoClaw OWL Bridge — sync adapter between Flask/requests code and the
OWL-AGENT async proxy-defense stack (owl_proxy.py or external install).

Hybrid backend selection (Synergy: owl-agent v5.3 × GLM_proxy cloud→local):
  1. If an external OWL-AGENT install exists (~/.owl-agent/proxy_defense.py),
     its ResilientClient is preferred — external install wins the race.
  2. Otherwise the vendored owl_proxy.py module is used.
  3. The whole layer can be disabled via OWL_PROXY_ENABLED=0.

Design notes:
  - Runs ONE background asyncio event loop in a daemon thread. Sync callers
    submit coroutines via asyncio.run_coroutine_threadsafe, so the proxy
    pool / validation workers / caches persist across requests.
  - All public helpers are fail-safe: if the OWL stack fails to initialize
    (missing deps, import errors, broken install), owl_enabled() flips to
    False and callers transparently fall back to direct `requests` calls.
  - SSE chat streaming is bridged with a bounded queue: an async pump task
    forwards raw bytes into it; the sync side iterates and reassembles lines
    (requests.iter_lines() semantics) in the Flask worker thread.
"""

import os
import json
import queue
import asyncio
import logging
import importlib.util
import threading
from pathlib import Path

logger = logging.getLogger("autoclaw.owl_bridge")

# ─── Feature flag: proxy-first is ON by default (Synergy decision) ──
_env = os.environ.get("OWL_PROXY_ENABLED", "1")
OWL_ENABLED_DEFAULT = _env.strip().lower() not in ("0", "false", "no", "off")

EXTERNAL_PROXY_DEFENSE = Path(
    os.environ.get("OWL_EXTERNAL_MODULE",
                   str(Path.home() / ".owl-agent" / "proxy_defense.py")))

_STARTUP_TIMEOUT = float(os.environ.get("OWL_STARTUP_TIMEOUT", "15"))


class OwlUnavailable(RuntimeError):
    """Raised when the OWL layer cannot serve a request (caller should
    fall back to direct requests)."""


# ─────────────────────────────────────────────────────────────
# Backend loading (hybrid: external install → vendored module)
# ─────────────────────────────────────────────────────────────

def _load_backend():
    """Return (module, backend_name); module None → name carries reason."""
    # 1. External OWL-AGENT install (preferred)
    if EXTERNAL_PROXY_DEFENSE.exists():
        try:
            spec = importlib.util.spec_from_file_location(
                "external_proxy_defense", EXTERNAL_PROXY_DEFENSE)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "ResilientClient"):
                logger.info(f"OWL bridge: using EXTERNAL backend "
                            f"({EXTERNAL_PROXY_DEFENSE})")
                return mod, "external"
            logger.warning("OWL bridge: external proxy_defense.py lacks "
                           "ResilientClient — falling back to vendored")
        except Exception as e:
            logger.warning(f"OWL bridge: external backend failed to load "
                           f"({e}) — falling back to vendored")
    # 2. Vendored module
    try:
        import owl_proxy as mod
        logger.info("OWL bridge: using VENDORED backend (owl_proxy.py)")
        return mod, "vendored"
    except Exception as e:
        logger.warning(f"OWL bridge: vendored backend failed to load: {e}")
        return None, f"unavailable: {e}"


# ─────────────────────────────────────────────────────────────
# Background event loop
# ─────────────────────────────────────────────────────────────

class _OwlRuntime:
    """Owns the daemon loop thread + the ResilientClient instance."""

    def __init__(self):
        self._loop = None
        self._client = None
        self._module = None
        self._backend_name = None
        self._init_error = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    # -- lifecycle ------------------------------------------------
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="owl-bridge-loop", daemon=True)
            self._thread.start()
        if not self._ready.wait(timeout=_STARTUP_TIMEOUT + 5):
            self._init_error = self._init_error or "init timeout"

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._init())
            # Signal readiness BEFORE run_forever — the finally below only
            # runs when the loop stops, and callers wait on _ready.
            self._ready.set()
            if self._client is not None:
                self._loop.run_forever()
        except Exception as e:
            self._init_error = f"{type(e).__name__}: {e}"
            logger.warning(f"OWL bridge loop died: {self._init_error}")
            self._ready.set()

    async def _init(self):
        mod, self._backend_name = _load_backend()
        if mod is None:
            self._init_error = self._backend_name
            return
        cls = mod.ResilientClient
        try:
            client = cls(startup_timeout=_STARTUP_TIMEOUT)
        except TypeError:
            client = cls()  # external backend without startup_timeout kwarg
        if hasattr(client, "start"):
            await client.start()
        elif hasattr(client, "__aenter__"):
            await client.__aenter__()
        self._client = client
        self._module = mod

    # -- execution ------------------------------------------------
    def submit_handle(self, coro_fn):
        """Schedule coro_fn() on the OWL loop; returns concurrent.Future."""
        if self._loop is None:
            raise OwlUnavailable("owl loop not running")
        return asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)

    def submit(self, coro_fn, timeout):
        """Run coro_fn() on the OWL loop, wait, return result."""
        if self._init_error or self._client is None:
            raise OwlUnavailable(self._init_error or "owl runtime not initialized")
        fut = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)
        try:
            return fut.result(timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            # concurrent.futures.TimeoutError IS TimeoutError on py3.11+
            fut.cancel()
            raise OwlUnavailable(f"owl request timed out after {timeout}s")

    def stats(self):
        if self._client is None or self._loop is None or not self._loop.is_running():
            return {"enabled": False,
                    "reason": self._init_error or "not initialized"}
        try:
            fut = asyncio.run_coroutine_threadsafe(self._client.get_stats(),
                                                   self._loop)
            data = fut.result(timeout=10)
        except Exception as e:
            return {"enabled": True, "backend": self._backend_name,
                    "error": str(e)}
        data["backend"] = self._backend_name
        data["enabled"] = True
        return data

    def backend(self):
        return self._backend_name

    def shutdown(self):
        """Graceful close (best-effort; used by tests and atexit)."""
        client, loop = self._client, self._loop
        self._client = None
        if client and loop and loop.is_running():
            async def _close():
                if hasattr(client, "close"):
                    await client.close()
                elif hasattr(client, "__aexit__"):
                    await client.__aexit__(None, None, None)
            try:
                fut = asyncio.run_coroutine_threadsafe(_close(), loop)
                fut.result(timeout=10)
            except Exception:
                pass
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)


_runtime: _OwlRuntime = None
_runtime_lock = threading.Lock()
_disabled_by_error = False


def _get_runtime():
    """Lazily start the singleton runtime; None when OWL is unusable."""
    global _runtime, _disabled_by_error
    if not OWL_ENABLED_DEFAULT or _disabled_by_error:
        return None
    with _runtime_lock:
        if _runtime is None:
            _runtime = _OwlRuntime()
            _runtime.start()
            if _runtime._init_error:
                logger.warning(f"OWL disabled after init failure: "
                               f"{_runtime._init_error}")
                _disabled_by_error = True
        if _runtime._init_error or _runtime._client is None:
            return None
        return _runtime


def owl_enabled() -> bool:
    """True when the OWL proxy layer is active (env-enabled AND healthy)."""
    return _get_runtime() is not None


def owl_backend():
    rt = _get_runtime()
    return rt.backend() if rt else None


def owl_stats() -> dict:
    """Stats for /health endpoint (never raises)."""
    rt = _get_runtime()
    if rt is None:
        return {"enabled": False,
                "env_enabled": OWL_ENABLED_DEFAULT,
                "reason": "disabled or init failed"}
    return rt.stats()


def shutdown_owl():
    """Stop the OWL runtime (idempotent)."""
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            _runtime.shutdown()
            _runtime = None


# ─────────────────────────────────────────────────────────────
# requests-compatible response shims
# ─────────────────────────────────────────────────────────────

class OwlResponse:
    """Minimal requests.Response-like shim for buffered OWL responses."""

    def __init__(self, status_code, content, headers):
        self.status_code = int(status_code)
        self._content = content or b""
        self.headers = headers or {}

    @property
    def text(self):
        return self._content.decode("utf-8", errors="replace")

    @property
    def content(self):
        return self._content

    def json(self):
        return json.loads(self._content.decode("utf-8", errors="replace"))


class OwlStreamResponse:
    """requests-like streaming shim for SSE passthrough.

    - .status_code, .headers
    - .iter_lines() → bytes lines, no trailing newline (requests semantics)
    - .read_error(limit) → drained error body text (status != 200 paths)
    """

    def __init__(self, status_code, headers, byte_iter=None, error_text=None):
        self.status_code = int(status_code)
        self.headers = headers or {}
        self._bytes = byte_iter
        self._error_text = error_text
        self._pump_future = None

    def iter_lines(self):
        if self._bytes is None:
            return iter(())
        gen = self._gen()
        return gen

    def _gen(self):
        try:
            buf = b""
            for chunk in self._bytes:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.rstrip(b"\r")
            if buf:
                yield buf.rstrip(b"\r")
        finally:
            self.close()

    def read_error(self, limit=4096):
        if self._error_text is not None:
            return self._error_text[:limit]
        taken = b""
        for chunk in (self._bytes or ()):
            taken += chunk
            if len(taken) >= limit:
                break
        return taken[:limit].decode("utf-8", errors="replace")

    def close(self):
        pf = getattr(self, "_pump_future", None)
        if pf is not None and not pf.done():
            pf.cancel()


# ─────────────────────────────────────────────────────────────
# Public request API (sync, fail-safe)
# ─────────────────────────────────────────────────────────────

def owl_request(method, url, headers=None, json_body=None, timeout=30,
                verify=False):
    """Buffered request via OWL stack. Returns OwlResponse.
    Raises OwlUnavailable — callers must fall back to `requests`."""
    rt = _get_runtime()
    if rt is None:
        raise OwlUnavailable("owl disabled")
    client = rt._client

    async def _do():
        return await client.request(method.upper(), url, headers=headers,
                                    json=json_body)

    result = rt.submit(_do, max(timeout, 30))
    status = getattr(result, "status", None)
    content = getattr(result, "content", b"")
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    if status is None:
        raise OwlUnavailable("backend returned malformed response")
    return OwlResponse(status, bytes(content),
                       dict(getattr(result, "headers", {}) or {}))


def owl_stream_request(method, url, headers=None, json_body=None, timeout=30,
                       verify=False):
    """Streaming request via OWL stack (SSE-safe).

    Returns OwlStreamResponse. Raises OwlUnavailable → caller falls back.
    Connection establishment (time-to-headers) is raced across HEDGE_FANOUT
    proxies; once headers arrive the body is pumped through a bounded queue.
    """
    rt = _get_runtime()
    if rt is None:
        raise OwlUnavailable("owl disabled")
    client = rt._client
    q: queue.Queue = queue.Queue(maxsize=4096)
    _SENTINEL = object()

    def _emit(self_item):
        """Thread-safe queue put from the event loop side.
        NOTE: queue.Queue methods are plain (non-awaitable) — `await q.put()`
        would enqueue the item and then raise TypeError (await None), killing
        the pump before the sentinel ever lands. put_nowait is the contract;
        on Queue.Full the consumer is gone — stop pumping."""
        try:
            q.put_nowait(self_item)
            return True
        except queue.Full:
            return False

    async def _pump():
        stream = await client.stream_request(
            method.upper(), url, headers=headers, json_body=json_body,
            timeout=max(timeout, 10))
        if not _emit(("headers", stream.status, dict(stream.headers or {}))):
            return
        try:
            if stream.status not in (200, 201):
                text = await stream.read_error(4096)
                _emit(("error_text", text))
                _emit((_SENTINEL,))
                return
            async for chunk in stream.aiter_raw():
                if not _emit(("chunk", chunk)):
                    return
            _emit((_SENTINEL,))
        except asyncio.CancelledError:
            _emit((_SENTINEL,))
            raise
        except Exception as e:
            _emit(("stream_error", f"{type(e).__name__}: {e}"))
            _emit((_SENTINEL,))
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass

    pump_future = asyncio.run_coroutine_threadsafe(_pump(), rt._loop)

    # Wait for headers (blocks the Flask worker thread until TTFB)
    try:
        kind, status, hdrs = q.get(timeout=max(timeout, 30))
    except queue.Empty:
        pump_future.cancel()
        raise OwlUnavailable(f"owl stream: no headers within {timeout}s")

    if kind == "error_text":          # failed before headers
        pump_future.cancel()
        raise OwlUnavailable(f"owl stream failed before headers: {status}")
    if kind == "stream_error":
        pump_future.cancel()
        raise OwlUnavailable(f"owl stream failed: {hdrs}")

    if kind != "headers":
        pump_future.cancel()
        raise OwlUnavailable(f"owl stream: unexpected event {kind}")

    def _byte_gen():
        try:
            while True:
                item = q.get()
                k = item[0]
                if k == "chunk":
                    yield item[1]
                elif k == "stream_error":
                    logger.warning(f"OWL stream interrupted mid-body: {item[1]}")
                    return
                else:  # error_text / sentinel
                    return
        finally:
            if not pump_future.done():
                pump_future.cancel()

    if status not in (200, 201):
        # Error path: pull the drained error text (pump already queued it)
        err = ""
        try:
            item = q.get(timeout=10)
            if item[0] == "error_text":
                err = item[1]
        except queue.Empty:
            pass
        resp = OwlStreamResponse(status, hdrs, error_text=err)
    else:
        resp = OwlStreamResponse(status, hdrs, byte_iter=_byte_gen())

    resp._pump_future = pump_future
    return resp
