import React from "react";
import StatCard from "./StatCard.jsx";
import Sparkline from "./Sparkline.jsx";
import BarList from "./BarList.jsx";

/**
 * DSML tool-call shim — real-world metrics panel (Phase-3.1, research
 * task 8-c schema). Renders the /health "dsml" block: parse success rate,
 * call grain by path, detected markup leaks (the headline safety metric),
 * the approximate tool-loop signal and shim overhead latency.
 *
 * props:
 *   dsml          — the "dsml" block from the dashboard payload
 *   featureModels — snapshot feature_models map ("dsml_shim|<model>" → n)
 */
export default function DSMLPanel({ dsml, featureModels }) {
  if (!dsml) {
    return (
      <div className="panel">
        <h2>DSML tool-call shim</h2>
        <div className="empty">dsml metrics unavailable</div>
      </div>
    );
  }
  const r = dsml.rates || {};
  const leaks = dsml.markup_leaks || {};
  const leaksTotal = Object.values(leaks).reduce((a, b) => a + b, 0);
  const callsTotal = (dsml.calls_stream ?? 0) + (dsml.calls_buffered ?? 0);
  const ov = dsml.overhead_ms || {};
  const perModel = Object.entries(featureModels || {})
    .filter(([k]) => k.startsWith("dsml_shim|"))
    .map(([k, v]) => [k.slice("dsml_shim|".length), v]);
  const leakEntries = Object.entries(leaks).filter(([, v]) => v > 0);
  const attempted = (dsml.parse_attempts ?? 0) + (dsml.responses_stream ?? 0);

  return (
    <div className="panel dsmlpanel">
      <h2>DSML tool-call shim {dsml.enabled === false ? "· disabled" : ""}</h2>
      <div className="cards dsmlcards">
        <StatCard
          label="Parse success"
          value={`${r.parse_success_pct ?? 0}%`}
          hint={`${attempted} attempts · ${dsml.parse_failures ?? 0} block fails`}
          tone={(r.parse_success_pct ?? 100) < 90 ? "warn" : "ok"}
        />
        <StatCard
          label="Tool calls"
          value={callsTotal}
          hint={`${dsml.calls_stream ?? 0} stream · ${dsml.calls_buffered ?? 0} buffered`}
        />
        <StatCard
          label="Markup leaks"
          value={leaksTotal}
          hint={leaksTotal ? "detected — inspect below" : "none detected"}
          tone={leaksTotal > 0 ? "bad" : "ok"}
        />
        <StatCard
          label="Tool loop"
          value={`${r.tool_loop_pct ?? 0}%`}
          hint="~approx · role:tool requests"
          tone="ok"
        />
      </div>

      <div className="dsmlgrid">
        <div>
          <h3>Markup leaks <span className="mut">detected</span></h3>
          {leaksTotal === 0
            ? <div className="empty">zero leaks — clean passthrough</div>
            : <BarList items={leakEntries} tone="bad" />}
        </div>
        <div>
          <h3>Shim overhead ms</h3>
          {(ov.window || []).length
            ? <Sparkline points={ov.window} />
            : <div className="empty">no shimmed responses yet</div>}
          <div className="mut" style={{ marginTop: 4 }}>
            avg {ov.avg ?? 0} ms · p95 {ov.p95 ?? 0} ms · holdback cost
          </div>
        </div>
        <div>
          <h3>Per-model activations</h3>
          {perModel.length === 0
            ? <div className="empty">no shimmed models yet</div>
            : <BarList items={perModel} tone="via" />}
        </div>
      </div>
      <div className="mut" style={{ marginTop: 8 }}>
        {dsml.injections ?? 0} injections → {dsml.responses_stream ?? 0} stream +{" "}
        {dsml.responses_buffered ?? 0} buffered shimmed · {dsml.finish_overrides ?? 0}{" "}
        finish_reason overrides · tool loop is a fleet-level approximation
        (role:&quot;tool&quot; also occurs for native calls)
      </div>
    </div>
  );
}
