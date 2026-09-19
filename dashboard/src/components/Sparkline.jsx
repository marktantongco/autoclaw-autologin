import React from "react";

/** Minimal SVG sparkline — no chart dependency, keeps the bundle tiny. */
export default function Sparkline({ points, height = 90 }) {
  const pts = (points || []).map((p) => Number(p) || 0);
  if (pts.length < 2) {
    return <div className="empty">waiting for traffic…</div>;
  }
  const w = 560;
  const max = Math.max(...pts, 1);
  const step = w / (pts.length - 1);
  const y = (v) => height - 6 - (v / max) * (height - 14);
  const path = pts.map((v, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${path} L${w},${height} L0,${height} Z`;
  return (
    <svg viewBox={`0 0 ${w} ${height}`} className="spark" preserveAspectRatio="none">
      <path d={area} fill="rgba(56,224,168,0.12)" />
      <path d={path} fill="none" stroke="#38e0a8" strokeWidth="2" />
    </svg>
  );
}
