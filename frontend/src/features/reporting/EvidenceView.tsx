import { asRecord, type ReportingSnapshotView } from "./reporting-store";

function display(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

export function EvidenceView({ snapshot }: { readonly snapshot: ReportingSnapshotView }) {
  const references = asRecord(snapshot.state.preparation_refs ?? snapshot.checkpoint.preparation_refs);
  const evidence = snapshot.evidence;
  return (
    <section className="reporting-card" aria-label="证据与来源台账">
      <div className="reporting-section-heading">
        <p>EVIDENCE CONTROL</p>
        <h3>覆盖、证据与来源台账</h3>
      </div>
      {Object.keys(references).length === 0 && Object.keys(evidence).length === 0 ? (
        <p className="reporting-empty">尚未生成可追溯的证据快照。</p>
      ) : null}
      <dl className="reporting-ledger">
        {Object.entries(references).map(([key, value]) => (
          <div key={key}><dt>{key}</dt><dd>{display(value)}</dd></div>
        ))}
        {Object.entries(evidence).map(([key, value]) => (
          <div key={`evidence-${key}`}><dt>{key}</dt><dd>{display(value)}</dd></div>
        ))}
      </dl>
    </section>
  );
}
