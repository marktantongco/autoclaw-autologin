import React from "react";

export default function StatCard({ label, value, hint, tone }) {
  return (
    <div className={`card ${tone || ""}`}>
      <div className="card-label">{label}</div>
      <div className="card-value">{value}</div>
      <div className="card-hint">{hint}</div>
    </div>
  );
}
