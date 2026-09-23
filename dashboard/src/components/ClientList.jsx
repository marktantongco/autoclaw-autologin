import React from "react";

/** Masked-IP client list — SHA-256 prefixes only (never raw addresses). */
export default function ClientList({ clients }) {
  const rows = Object.entries(clients || {}).sort(([, a], [, b]) => b.requests - a.requests);
  return (
    <div className="panel">
      <h2>Clients <span className="hint">masked · {rows.length}</span></h2>
      <div className="logwrap">
        <table className="log">
          <thead><tr><th>client</th><th>requests</th><th>first seen</th><th>last seen</th></tr></thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan="4" className="empty">no clients yet</td></tr>}
            {rows.map(([id, c]) => (
              <tr key={id}>
                <td className="mono">{id.slice(0, 12)}</td>
                <td className="mono">{c.requests}</td>
                <td className="mono dim">{new Date(c.first_seen * 1000).toLocaleTimeString()}</td>
                <td className="mono dim">{new Date(c.last_seen * 1000).toLocaleTimeString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
