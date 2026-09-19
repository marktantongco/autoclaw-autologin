import React, { useState } from "react";

const PREFS = [
  { id: "owl-first", label: "OWL-first", desc: "free-pool racing → thermoptic → direct" },
  { id: "thermoptic-first", label: "thermoptic-first", desc: "browser camouflage → OWL → direct" },
  { id: "direct-only", label: "direct-only", desc: "no proxy layers (debug)" },
];

/** Backend-switch control surface (OmniClaw dashboard contract). */
export default function ControlPanel({ control, onSetPref, onAction }) {
  const [busy, setBusy] = useState(false);
  const apply = async (fn) => { setBusy(true); try { await fn(); } finally { setBusy(false); } };
  return (
    <section className="panel control">
      <h2>Control surface</h2>
      <div className="pref-row">
        {PREFS.map((p) => (
          <button key={p.id}
            className={`pref ${control.backend_pref === p.id ? "active" : ""} ${busy ? "busy" : ""}`}
            disabled={busy || control.backend_pref === p.id}
            onClick={() => apply(() => onSetPref(p.id))}>
            <span className="pref-label">{p.label}</span>
            <span className="pref-desc">{p.desc}</span>
          </button>
        ))}
      </div>
      <div className="action-row">
        <button className="action" disabled={busy} onClick={() => apply(() => onAction("clear_negative_cache"))}>
          Clear negative cache
        </button>
        <button className="action danger" disabled={busy} onClick={() => apply(() => onAction("reset_metrics"))}>
          Reset metrics
        </button>
        <span className="hint">
          updated {control.updated_at ? new Date(control.updated_at * 1000).toLocaleTimeString() : "—"}
        </span>
      </div>
    </section>
  );
}
