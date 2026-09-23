import React, { useState } from "react";

/**
 * v2.5.0 (SPEC-8b7 D4): dashboard login card.
 *
 * Shown when /api/dashboard/state answers 401 (proxy API key configured but
 * no session). The key is exchanged ONCE for an HttpOnly SameSite=Strict
 * session cookie via POST /api/dashboard/auth — it is never stored in
 * localStorage, never re-sent by the UI, and never rendered anywhere.
 */
export default function LoginCard({ onSuccess }) {
  const [key, setKey] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!key || busy) return;
    setBusy(true);
    setErr("");
    try {
      const r = await fetch("/api/dashboard/auth", {
        method: "POST",
        headers: { Authorization: `Bearer ${key}` },
        credentials: "same-origin",
      });
      if (r.ok) { onSuccess(); return; }
      if (r.status === 429) setErr("Too many attempts — wait 60 seconds.");
      else {
        const j = await r.json().catch(() => ({}));
        setErr(j?.error?.message || `Rejected (${r.status}).`);
      }
    } catch (e2) {
      setErr("Network error — is the proxy up?");
    }
    setBusy(false);
  };

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <span className="logo">⌾</span>
        <h2>Dashboard locked</h2>
        <p className="mut">
          This proxy runs with an API key. Enter it once to mint a session
          (HttpOnly cookie, 8h). The key itself is never stored by the UI.
        </p>
        <input
          type="password"
          autoFocus
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="AUTOCLAW_PROXY_API_KEY"
          aria-label="Proxy API key"
        />
        <button type="submit" disabled={busy || !key}>
          {busy ? "Unlocking…" : "Unlock dashboard"}
        </button>
        {err && <div className="login-err">{err}</div>}
      </form>
    </div>
  );
}
