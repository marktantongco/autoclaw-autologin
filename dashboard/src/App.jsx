import React, { useEffect, useRef, useState, useCallback } from "react";
import StatCard from "./components/StatCard.jsx";
import Sparkline from "./components/Sparkline.jsx";
import BarList from "./components/BarList.jsx";
import RequestLog from "./components/RequestLog.jsx";
import ClientList from "./components/ClientList.jsx";
import ControlPanel from "./components/ControlPanel.jsx";

/**
 * AutoClaw operator dashboard (Synergy #15 — OmniClaw Local React
 * Dashboard). Live data via the long-running WebSocket metrics stream
 * (/api/dashboard/stream) with automatic REST polling fallback
 * (/api/dashboard/state) when WebSockets are unavailable.
 */
export default function App() {
  const [data, setData] = useState(null);
  const [conn, setConn] = useState("connecting"); // live | polling | connecting
  const wsRef = useRef(null);
  const pollRef = useRef(null);
  const attemptsRef = useRef(0);

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
  }, []);

  const connectWS = useCallback(() => {
    if (attemptsRef.current >= 3) { startPolling(); return; }
    attemptsRef.current += 1;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    let ws;
    try {
      ws = new WebSocket(`${proto}${location.host}/api/dashboard/stream`);
    } catch (e) { startPolling(); return; }
    wsRef.current = ws;
    ws.onopen = () => { setConn("live"); if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg && msg.hello) return; // handshake frame
        setData(msg);
      } catch (e) { /* ignore malformed frame */ }
    };
    ws.onclose = () => { wsRef.current = null; setTimeout(attemptsRef.current >= 3 ? startPolling : connectWS, 1500); };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* noop */ } };
  }, [startPolling]);

  useEffect(() => {
    connectWS();
    return () => {
      if (wsRef.current) { try { wsRef.current.close(); } catch (e) { /* noop */ } }
      if (pollRef.current) clearInterval(pollRef.current);
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
    return <div className="boot">Connecting to AutoClaw proxy…</div>;
  }

  const t = data.totals || {};
  const lat = data.latency || {};
  const hm = data.health_mini || {};
  const statusEntries = Object.entries(data.status_codes || {}).sort(([a], [b]) => a.localeCompare(b));
  const errStatuses = statusEntries.filter(([s]) => s.startsWith("4") || s.startsWith("5"));

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
          <span className="badge">up {Math.floor((data.uptime_s || 0) / 60)}m</span>
        </div>
      </header>

      <section className="cards">
        <StatCard label="Requests" value={t.requests ?? 0} hint={`${t.stream ?? 0} stream · ${t.buffered ?? 0} buffered`} />
        <StatCard label="Error rate" value={`${data.error_rate_pct ?? 0}%`} hint={`${t.errors ?? 0} × 5xx`} tone={data.error_rate_pct > 5 ? "bad" : "ok"} />
        <StatCard label="Avg latency" value={`${lat.avg_ms ?? 0} ms`} hint="rolling window" />
        <StatCard label="p95 latency" value={`${lat.p95_ms ?? 0} ms`} hint="rolling window" />
        <StatCard label="Blocks" value={t.blocks ?? 0} hint="loop · cache · auth" tone={t.blocks > 0 ? "warn" : "ok"} />
        <StatCard label="Clients" value={data.client_count ?? 0} hint="masked identities" />
      </section>

      <section className="grid-2">
        <div className="panel">
          <h2>Latency — last { (lat.window || []).length } requests</h2>
          <Sparkline points={lat.window || []} />
        </div>
        <div className="panel">
          <h2>Egress transport (via)</h2>
          <BarList items={Object.entries(data.via || {})} tone="via" />
        </div>
      </section>

      <section className="grid-3">
        <div className="panel">
          <h2>Status codes</h2>
          <BarList items={statusEntries} tone={(v) => (v.startsWith("2") ? "ok" : "bad")} />
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

      <ControlPanel control={data.control || {}} onSetPref={(p) => post({ backend_pref: p })}
        onAction={(a) => post({ action: a })} />

      <section className="grid-2">
        <RequestLog log={data.request_log || []} />
        <ClientList clients={data.clients || {}} />
      </section>

      <footer className="foot">
        AutoClaw v{hm.version || "?"} · Synergies 1–9, 13, 14, 15 ·{" "}
        <a href="/" target="_blank" rel="noreferrer">classic UI</a> ·{" "}
        <a href="/health" target="_blank" rel="noreferrer">/health</a>
      </footer>
    </div>
  );
}
