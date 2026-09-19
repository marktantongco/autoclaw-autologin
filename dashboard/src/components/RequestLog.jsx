import React from "react";

const statusClass = (s) => (s >= 500 ? "st-5xx" : s >= 400 ? "st-4xx" : s >= 300 ? "st-3xx" : "st-2xx");

export default function RequestLog({ log }) {
  const rows = [...(log || [])].reverse().slice(0, 60);
  return (
    <div className="panel">
      <h2>Request log <span className="hint">newest first · last {rows.length}</span></h2>
      <div className="logwrap">
        <table className="log">
          <thead>
            <tr><th>time</th><th>route</th><th>st</th><th>via</th><th>model</th><th>ms</th><th>tag</th></tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan="7" className="empty">no requests yet</td></tr>
            )}
            {rows.map((r, i) => (
              <tr key={i}>
                <td className="mono">{new Date(r.ts * 1000).toLocaleTimeString()}</td>
                <td className="mono">{r.route}</td>
                <td><span className={`st ${statusClass(r.status)}`}>{r.status}</span></td>
                <td className="mono">{r.via}</td>
                <td className="mono dim">{r.model || "—"}</td>
                <td className="mono">{r.latency_ms}</td>
                <td className="mono dim">{r.block ? `⛔ ${r.block}` : r.feature ? `✦ ${r.feature}` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
