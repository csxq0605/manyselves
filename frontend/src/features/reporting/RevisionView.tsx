import { useState } from "react";

import type { ReportingRevisionRequest } from "./reporting-api";
import { asStringArray, asText, type ReportingSnapshotView } from "./reporting-store";

export interface RevisionViewProps {
  readonly onSubmit: (input: ReportingRevisionRequest) => Promise<unknown>;
  readonly snapshot: ReportingSnapshotView;
}

const moduleIds = ["2.1", "2.2", "2.3", "2.4", "2.5"] as const;

export function RevisionView({ onSubmit, snapshot }: RevisionViewProps) {
  const [feedback, setFeedback] = useState("");
  const [target, setTarget] = useState<(typeof moduleIds)[number]>("2.1");
  const [submitting, setSubmitting] = useState(false);
  const baseline = asText(snapshot.revision.baseline_version_id)
    || asText(snapshot.run.id);
  const previousTargets = asStringArray(snapshot.revision.target_module_ids);
  const canRevise = snapshot.run.status === "completed"
    && snapshot.verification.status === "passed";

  async function submit() {
    if (!feedback.trim() || submitting) return;
    setSubmitting(true);
    try {
      await onSubmit({
        baselineVersionId: baseline,
        feedback: feedback.trim(),
        maxProviderAttempts: 40,
        maxTotalTokens: 400_000,
        promoteToSkill: false,
        targetModuleIds: [target],
      });
      setFeedback("");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="reporting-card" aria-label="版本与修订">
      <div className="reporting-section-heading">
        <p>REVISION CONTROL</p>
        <h3>版本与局部修订</h3>
      </div>
      <dl className="reporting-ledger">
        <div><dt>基线版本</dt><dd>{baseline}</dd></div>
        {snapshot.revision.feedback ? <div><dt>最近反馈</dt><dd>{asText(snapshot.revision.feedback)}</dd></div> : null}
        {previousTargets.length > 0 ? <div><dt>修订模块</dt><dd>{previousTargets.join("、")}</dd></div> : null}
      </dl>
      {canRevise ? <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <label>
          <span>目标模块</span>
          <select value={target} onChange={(event) => setTarget(event.target.value as typeof target)}>
            {moduleIds.map((moduleId) => <option key={moduleId}>{moduleId}</option>)}
          </select>
        </label>
        <label>
          <span>修订反馈</span>
          <textarea value={feedback} onChange={(event) => setFeedback(event.target.value)} />
        </label>
        <button disabled={submitting || !feedback.trim()} type="submit">
          {submitting ? "提交中…" : "创建修订运行"}
        </button>
      </form> : <p className="reporting-empty">报告完成并通过服务端校验后可创建局部修订。</p>}
    </section>
  );
}
