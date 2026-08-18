import { useState } from "react";

import type { ReportingDecisionRequest } from "./reporting-api";
import type { ReportingWaitingInputView } from "./reporting-store";

export interface WaitingInputFormProps {
  readonly onSubmit: (
    decisionId: string,
    input: ReportingDecisionRequest,
  ) => Promise<unknown>;
  readonly request: ReportingWaitingInputView;
}

const actionLabels: Record<string, string> = {
  draft: "保留不确定性并继续起草",
  skip: "保留目录并标记未评估",
  stop: "停止本次运行",
  supplement: "补充资料后继续",
};

export function WaitingInputForm({ onSubmit, request }: WaitingInputFormProps) {
  const allowed = request.allowedActions.length > 0
    ? request.allowedActions
    : ["supplement", "draft", "skip", "stop"];
  const [action, setAction] = useState(allowed[0] ?? "supplement");
  const [content, setContent] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const needsSupplement = action === "supplement";

  async function submit() {
    if (submitting || submitted || (needsSupplement && !content.trim())) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(request.decisionId, {
        action: action as ReportingDecisionRequest["action"],
        ...(needsSupplement
          ? { supplements: [{ content: content.trim(), scope: "run" }] }
          : {}),
      });
      setSubmitted(true);
    } catch {
      setError("提交失败，服务器尚未消费本次决定，可安全重试。 ");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      className="reporting-form"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="reporting-section-heading">
        <p>DECISION REQUIRED</p>
        <h3>等待用户决定</h3>
      </div>
      <p>{request.missingItems.join("；") || "报告流程需要用户确认后才能继续。"}</p>
      {request.affectedModules.length > 0 ? (
        <p className="reporting-meta">影响模块：{request.affectedModules.join("、")}</p>
      ) : null}
      <label>
        <span>处理方式</span>
        <select value={action} onChange={(event) => setAction(event.target.value)}>
          {allowed.map((item) => <option key={item} value={item}>{actionLabels[item] ?? item}</option>)}
        </select>
      </label>
      {needsSupplement ? (
        <label>
          <span>补充信息</span>
          <textarea
            required
            value={content}
            onChange={(event) => setContent(event.target.value)}
          />
        </label>
      ) : null}
      {error ? <p role="alert">{error}</p> : null}
      <button disabled={submitting || submitted || (needsSupplement && !content.trim())} type="submit">
        {submitting ? "提交中…" : "继续流程"}
      </button>
      {submitted ? <p role="status">决定已提交，正在等待服务端刷新。</p> : null}
    </form>
  );
}
