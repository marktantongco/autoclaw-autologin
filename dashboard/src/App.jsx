import React, { useEffect, useRef, useState, useCallback } from "react";
import StatCard from "./components/StatCard.jsx";
import Sparkline from "./components/Sparkline.jsx";
import BarList from "./components/BarList.jsx";
import RequestLog from "./components/RequestLog.jsx";
import ClientList from "./components/ClientList.jsx";
import ControlPanel from "./components/ControlPanel.jsx";
import DSMLPanel from "./components/DSMLPanel.jsx";

/** Compact counter formatting: 1.2k / 3.4M (Phase-3.1, 8-b W16). */
const compact = (n) =>
  n == null ? "0"
  : n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M"
  : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k"
  : String(n);

/** Uptime rolls over to h / d instead of growing minute counts forever. */
const fmtUptime = (s) => {
  const h = Math.floor((s || 0) / 3600), m = Math.floor(((s || 0) % 3600) / 60);
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
};

/**
 * AutoClaw operator dashboard (Synergy #15 — OmniClaw Local React
 * Dashboard). Live data via the long-running WebSocket metrics stream
 * (/api/dashboard/stream) with automatic REST polling fallback
 * (/api/dashboard/state) when WebSockets are unavailable.
 *
 * Phase-3.1 (research 8-b): exponential WS reconnect backoff with reset on
 * success + periodic WS retry while polling; DSML real-world panel; hourly
 * history strip from persisted rollups.
 */
export default function App() {
  const [data, setData] = useState(null);
  const [conn, setConn] = useState("connecting"); // live | polling | connecting
  const wsRef = useRef(null);
  const pollRef = useRef(null);
  const retryRef = useRef(null);   // pending WS retry timer while polling
  const attemptsRef = useRef(0);
  const MAX_WS_ATTEMPTS = 5;

  const startPolling = useCallback(() => {
    if (pollRef.current) return;
    setConn("polling");
    const tick = async () => {
      try {
        const r = await fetch("/api/dashboard/state", { cache: "no-store" });
        if (r.ok) setData(await r.json());
      } catch (e) { /* keep last snapshot */ }
    };
    tick();
    pollRef.current = setInterval(tick, 2000);
    // 8-b W8: keep retrying WS while polling — a proxy restart used to
    // strand the page in polling mode forever.
    const retry = () => {
      if (!wsRef.current) connectWS(true);
      retryRef.current = setTimeout(retry, 30000);
    };
    retryRef.current = setTimeout(retry, 30000);
  }, []);

  const connectWS = useCallback((resetAttempts = false) => {
    if (resetAttempts) attemptsRef.current = 0;
    if (attemptsRef.current >= MAX_WS_ATTEMPTS) { startPolling(); return; }
    attemptsRef.current += 1;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    let ws;
    try {
      ws = new WebSocket(`${proto}${location.host}/api/dashboard/stream`);
    } catch (e) { startPolling(); return; }
    wsRef.current = ws;
    ws.onopen = () => {
      attemptsRef.current = 0; // healthy connect resets the backoff ladder
      setConn("live");
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      if (retryRef.current) { clearTimeout(retryRef.current); retryRef.current = null; }
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg && msg.hello) return; // handshake frame
        if (msg && msg.error === "too_many_clients") { startPolling(); return; }
        setData(msg);
      } catch (e) { /* ignore malformed frame */ }
    };
    ws.onclose = () => {
      wsRef.current = null;
      // exponential backoff: 1s → 2s → 4s → 8s → capped 15s
      const wait = Math.min(15000, 1000 * Math.pow(2, attemptsRef.current - 1));
      setTimeout(attemptsRef.current >= MAX_WS_ATTEMPTS ? startPolling : connectWS, wait);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* noop */ } };
  }, [startPolling]);

  useEffect(() => {
    connectWS();
    return () => {
      if (wsRef.current) { try { wsRef.current.close(); } catch (e) { /* noop */ } }
      if (pollRef.current) clearInterval(pollRef.current);
      if (retryRef.current) clearTimeout(retryRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const post = useCallback(async (body) => {
    try {
      await fetch("/api/dashboard/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      // optimistic refresh
      const r = await fetch("/api/dashboard/state", { cache: "no-store" });
      if (r.ok) setData(await r.json());
    } catch (e) { /* surfaced via metrics on next tick */ }
  }, []);

  if (!data) {
    return <div className="boot">◌ Connecting to AutoClaw proxy…</div>;
  }

  const t = data.totals || {};
  const lat = data.latency || {};
  const hm = data.health_mini || {};
  const statusEntries = Object.entries(data.status_codes || {}).sort(([a], [b]) => a.localeCompare(b));
  const historyEntries = (data.rollups || [])
    .filter((b) => b.requests > 0)
    .map((b) => {
      const d = new Date(b.hour * 3600 * 1000);
      return [`${String(d.getHours()).padStart(2, "0")}:00`, b.requests];
    });

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="logo">⌾</span>
          <div>
            <h1>AutoClaw Proxy</h1>
            <div className="sub">v{hm.version || "?"} · Synergy stack live</div>
          </div>
        </div>
        <div className="badges">
          <span className={`badge ${conn === "live" ? "ok" : conn === "polling" ? "warn" : ""}`}>
            {conn === "live" ? "● WS live" : conn === "polling" ? "◐ REST polling" : "○ connecting"}
          </span>
          <span className={`badge ${hm.owl_enabled ? "ok" : ""}`}>OWL {hm.owl_enabled ? "on" : "off"}</span>
          <span className={`badge ${hm.thermoptic_healthy ? "ok" : ""}`}>thermoptic {hm.thermoptic_healthy ? "healthy" : "off"}</span>
          <span className={`badge ${hm.ws_fallback_ready ? "ok" : ""}`}>ws-agent {hm.ws_fallback_ready ? "ready" : "n/a"}</span>
          <span className="badge">accounts {hm.accounts ?? "?"}</span>
          <span className="badge" title="proxy uptime · telemetry persisted across restarts">
            up {fmtUptime(data.uptime_s)}
          </span>
        </div>
      </header>

      <section className="cards">
        <StatCard label="Requests" value={compact(t.requests)} hint={`${t.stream ?? 0} stream · ${t.buffered ?? 0} buffered`} />
        <StatCard label="Error rate" value={`${data.error_rate_pct ?? 0}%`} hint={`${compact(t.errors ?? 0)} × 5xx`} tone={data.error_rate_pct > 5 ? "bad" : "ok"} />
        <StatCard label="Avg latency" value={`${lat.avg_ms ?? 0} ms`} hint="true stream duration · rolling window" />
        <StatCard label="p95 latency" value={`${lat.p95_ms ?? 0} ms`} hint="true stream duration · rolling window" />
        <StatCard label="Blocks" value={compact(t.blocks)} hint="loop · cache · auth" tone={t.blocks > 0 ? "warn" : "ok"} />
        <StatCard label="Clients" value={compact(data.client_count)} hint="masked identities" />
      </section>

      <DSMLPanel dsml={data.dsml} featureModels={data.feature_models} />

      <section className="grid-2">
        <div className="panel">
          <h2>Latency — last { (lat.window || []).length } requests</h2>
          {(lat.window || []).length
            ? <Sparkline points={lat.window || []} />
            : <div className="empty">waiting for traffic…</div>}
        </div>
        <div className="panel">
          <h2>Egress transport (via)</h2>
          {Object.keys(data.via || {}).length === 0
            ? <div className="empty">no egress yet</div>
            : <BarList items={Object.entries(data.via || {})} tone="via" />}
        </div>
      </section>

      <section className="grid-3">
        <div className="panel">
          <h2>Status codes</h2>
          {statusEntries.length === 0
            ? <div className="empty">no responses yet</div>
            : <BarList items={statusEntries} tone={(v) => (v.startsWith("2") ? "ok" : "bad")} />}
        </div>
        <div className="panel">
          <h2>Blocks</h2>
          {Object.keys(data.blocks || {}).length === 0
            ? <div className="empty">no blocks recorded</div>
            : <BarList items={Object.entries(data.blocks || {})} tone="bad" />}
          <h2 style={{ marginTop: 14 }}>Feature activations</h2>
          {Object.keys(data.features || {}).length === 0
            ? <div className="empty">no shim/fallback activations yet</div>
            : <BarList items={Object.entries(data.features || {})} tone="via" />}
        </div>
        <div className="panel">
          <h2>Models</h2>
          {Object.keys(data.models || {}).length === 0
            ? <div className="empty">no chat traffic yet</div>
            : <BarList items={Object.entries(data.models || {})} tone="via" />}
        </div>
      </section>

      {historyEntries.length > 0 && (
        <section className="grid-2">
          <div className="panel">
            <h2>History — hourly requests <span className="mut">persisted · {historyEntries.length}h</span></h2>
            <BarList items={historyEntries} tone="via" />
          </div>
          <div className="panel">
            <h2>Block rate by hour</h2>
            <BarList
              items={(data.rollups || []).filter((b) => b.blocks > 0)
                .map((b) => {
                  const d = new Date(b.hour * 3600 * 1000);
                  return [`${String(d.getHours()).padStart(2, "0")}:00`, b.blocks];
                })}
              tone="bad" />
            {(data.rollups || []).every((b) => b.blocks === 0) &&
              <div className="empty">zero blocks in the retention window</div>}
          </div>
        </section>
      )}

      <ControlPanel control={data.control || {}} onSetPref={(p) => post({ backend_pref: p })}
        onAction={(a) => post({ action: a })} />

      <section className="grid-2">
        <RequestLog log={data.request_log || []} />
        <ClientList clients={data.clients || {}} />
      </section>

      <footer className="foot">
        AutoClaw v{hm.version || "?"} · Synergies 1–6, 8–15 · #7 import mode ·{" "}
        <a href="/" target="_blank" rel="noreferrer">classic UI</a> ·{" "}
        <a href="/health" target="_blank" rel="noreferrer">/health</a>
      </footer>
    </div>
  );
}
