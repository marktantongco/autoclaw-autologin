import React, { useEffect, useMemo, useState } from "react";

/**
 * Model picker + live chat probe (v2.6.1 — Synergy 7 surface).
 *
 * Lists every client-facing alias from /api/models — the glm family
 * (with output caps) and the claude-* credit-tier aliases with their
 * CURRENT upstream target (live remote config when reachable, heuristic
 * otherwise). Fires the probe through /api/test-chat, which loopbacks
 * into the exact wire the clients hit (/v1/chat/completions or
 * /v1/messages) — so what the operator sees here is what a real client
 * gets, including banner injection, clamping and the negative cache.
 */

const TIER_BADGE = {
  high: "tier-high",
  medium: "tier-med",
  low: "tier-low",
};

export default function ModelPicker() {
  const [catalog, setCatalog] = useState(null);   // /api/models payload
  const [catalogErr, setCatalogErr] = useState(null);
  const [model, setModel] = useState("");
  const [endpoint, setEndpoint] = useState("openai");
  const [message, setMessage] = useState("Reply with exactly: OK");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);     // {ok, ...} | {ok:false, error}
  const [ms, setMs] = useState(null);

  const loadCatalog = async () => {
    try {
      const r = await fetch("/api/models", { cache: "no-store", credentials: "same-origin" });
      if (r.status === 401) { setCatalogErr("auth locked — sign in above"); return; }
      if (!r.ok) { setCatalogErr(`catalog unavailable (HTTP ${r.status})`); return; }
      const data = await r.json();
      setCatalog(data);
      setCatalogErr(null);
      setModel((cur) => cur || data.default_model || (data.models[0] && data.models[0].id) || "");
    } catch (e) {
      setCatalogErr("catalog unreachable");
    }
  };

  useEffect(() => { loadCatalog(); }, []);

  const selected = useMemo(
    () => (catalog && model && catalog.models.find((m) => m.id === model)) || null,
    [catalog, model]);

  const grouped = useMemo(() => {
    const g = { glm: [], claude: [] };
    for (const m of (catalog && catalog.models) || []) (g[m.family] || (g[m.family] = [])).push(m);
    return g;
  }, [catalog]);

  const send = async () => {
    setBusy(true); setResult(null); setMs(null);
    const t0 = performance.now();
    try {
      const r = await fetch("/api/test-chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ model, message, endpoint }),
      });
      setMs(Math.round(performance.now() - t0));
      const data = await r.json();
      if (r.ok && data.success) {
        setResult({ ok: true, ...data });
      } else {
        const err = data.error || {};
        setResult({
          ok: false,
          status: r.status,
          message: err.message || err.error
            || (typeof err === "string" ? err : JSON.stringify(err).slice(0, 300)),
          failureClass: err.failure_class || null,
          endpoint: data.endpoint || endpoint,
        });
      }
    } catch (e) {
      setResult({ ok: false, message: String(e) });
    } finally {
      setBusy(false);
    }
  };

  const tierSource = catalog && catalog.tiers && catalog.tiers.source;

  return (
    <section className="panel picker">
      <h2>Model picker · live probe
        <span className="hint">
          {tierSource ? ` tier source: ${tierSource}` : ""}
          {" · "}
          <button className="linkish" onClick={loadCatalog}>reload</button>
        </span>
      </h2>

      {catalogErr && <div className="empty">⚠ {catalogErr}</div>}

      {!catalogErr && catalog && (
        <>
          <div className="picker-row">
            <select className="picker-select" value={model}
              onChange={(e) => { setModel(e.target.value); setResult(null); }}>
              <optgroup label="GLM family">
                {grouped.glm.map((m) => (
                  <option key={m.id} value={m.id}>{m.id}</option>
                ))}
              </optgroup>
              <optgroup label="claude-* (credit-tier routed)">
                {grouped.claude.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id} → {m.tier_label} tier{m.upstream ? ` (${m.upstream})` : ""}
                  </option>
                ))}
              </optgroup>
            </select>

            <div className="seg">
              {["openai", "anthropic"].map((ep) => (
                <button key={ep}
                  className={`seg-btn ${endpoint === ep ? "active" : ""}`}
                  onClick={() => { setEndpoint(ep); setResult(null); }}>
                  {ep === "openai" ? "/v1/chat/completions" : "/v1/messages"}
                </button>
              ))}
            </div>
          </div>

          {selected && (
            <div className="picker-meta">
              <span className="badge">
                {selected.family === "claude"
                  ? `tier ${selected.tier_label} → ${selected.upstream || "?"}`
                  : `upstream ${selected.upstream}`}
              </span>
              {selected.max_output != null && (
                <span className="badge">max output {selected.max_output.toLocaleString()}</span>
              )}
              {selected.family === "claude" && (
                <span className="badge">Anthropic wire in/out</span>
              )}
            </div>
          )}

          <div className="picker-row">
            <input className="picker-input" value={message} list={undefined}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !busy) send(); }}
              placeholder="probe message…" />
            <button className="action" disabled={busy || !model || !message.trim()}
              onClick={send}>
              {busy ? "probing…" : "Send probe"}
            </button>
          </div>

          {result && (
            <div className={`picker-result ${result.ok ? "ok" : "bad"}`}>
              {result.ok ? (
                <>
                  <div className="picker-result-head">
                    <span className={`st st-2xx`}>200</span>
                    <span className="mono">{result.endpoint} wire</span>
                    {ms != null && <span className="mut">{ms} ms</span>}
                    {result.model && <span className="mut">model: {result.model}</span>}
                    {result.stop_reason && <span className="mut">stop: {result.stop_reason}</span>}
                  </div>
                  <div className="picker-text">{result.content || "(empty response)"}</div>
                  {result.usage && (
                    <div className="mut picker-usage">
                      tokens: in {result.usage.prompt_tokens ?? "?"} ·
                      out {result.usage.completion_tokens ?? "?"}
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="picker-result-head">
                    <span className={`st ${result.status && String(result.status)[0] === "5" ? "st-5xx" : "st-4xx"}`}>
                      {result.status || "ERR"}
                    </span>
                    <span className="mono">{result.endpoint} wire</span>
                    {ms != null && <span className="mut">{ms} ms</span>}
                    {result.failureClass && (
                      <span className="badge warn">failure class: {result.failureClass}</span>
                    )}
                  </div>
                  <div className="picker-text">{result.message}</div>
                </>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
