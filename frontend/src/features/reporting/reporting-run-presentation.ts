import type { WorkflowRunResponse } from "../runs/workflow-api";

const workflowLabels: Readonly<Record<string, string>> = {
  "aggregate-existing": "汇总既有模块",
  "distill-template-skill": "蒸馏报告模板 Skill",
  "full-report": "配电安全报告",
  "module-report": "单模块报告",
  "render-existing": "渲染既有报告",
};

const actionLabels: Readonly<Record<string, string>> = {
  "attach-module-results": "汇总模块结果",
  "attach-readiness-preparation": "整理证据检查结果",
  "attach-readiness-to-context": "装配报告上下文",
  "attach-reporting-preparation": "装配资料准备结果",
  "build-reporting-state": "构建报告上下文",
  "finish-full-report": "完成报告",
  "initialize-full-report": "初始化报告",
  "project-preparation-for-full-report": "准备项目资料",
  "project-preparation-for-readiness": "准备证据检查",
  "publish-full-report": "发布报告结果",
  "run-evidence-readiness": "证据完整性检查",
  "run-module-cohort": "模块协同与写作",
  "run-reporting-preparation": "解析与准备原始资料",
  "run-reporting-tail": "跨模块复核与总报告",
};

export function reportingWorkflowLabel(workflowId: string): string {
  return workflowLabels[workflowId] ?? workflowId;
}

export function reportingActionLabel(actionId: string): string {
  return actionLabels[actionId] ?? actionId;
}

export function reportingRunStage(run: WorkflowRunResponse): string {
  const actionId = run.state.next_action_id;
  if (typeof actionId !== "string" || actionId.length === 0) {
    return run.run.status === "completed" ? "已完成" : "正在准备运行";
  }
  return reportingActionLabel(actionId);
}

export function reportingRunStepSummary(run: WorkflowRunResponse): string | null {
  const index = run.state.next_action_index;
  if (typeof index !== "number" || !Number.isInteger(index) || index < 1) return null;
  return `已完成 ${index} 个顶层步骤`;
}
