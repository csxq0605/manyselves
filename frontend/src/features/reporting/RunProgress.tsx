import type { ReportingRunView } from "./reporting-store";

const labels: Record<ReportingRunView["status"], string> = {
  cancelled: "已取消",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  planning: "正在规划",
  revising: "修订中",
  running: "生成中",
  unknown: "状态未知",
  waiting_user: "等待补充信息",
};

export function RunProgress({ run }: { readonly run: ReportingRunView }) {
  return (
    <div className={`reporting-progress reporting-progress--${run.status}`} role="status">
      <span className="reporting-progress__signal" aria-hidden="true" />
      <span>{labels[run.status]}</span>
      {run.active ? <small>运行中</small> : null}
      {run.error ? <small>{run.error}</small> : null}
    </div>
  );
}
