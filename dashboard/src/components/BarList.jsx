import React from "react";

/** Horizontal bar list for via/status/models/blocks distributions. */
export default function BarList({ items, tone = "via" }) {
  const entries = (items || []).filter(([, n]) => Number(n) > 0);
  if (!entries.length) return <div className="empty">no data</div>;
  const max = Math.max(...entries.map(([, n]) => Number(n)), 1);
  return (
    <div className="barlist">
      {entries.map(([k, n]) => (
        <div className="bar-row" key={k}>
          <div className="bar-key">{k}</div>
          <div className="bar-track">
            <div className={`bar-fill ${typeof tone === "function" ? tone(k) : tone}`}
              style={{ width: `${(Number(n) / max) * 100}%` }} />
          </div>
          <div className="bar-num">{n}</div>
        </div>
      ))}
    </div>
  );
}
