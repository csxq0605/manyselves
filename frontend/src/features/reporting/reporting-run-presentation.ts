import type { WorkflowRunResponse } from "../runs/workflow-api";

const workflowLabels: Readonly<Record<string, string>> = {
  "extract-template-skills": "提炼报告模板 Skill",
  "full-report": "配电安全报告",
  "render-existing": "渲染既有报告",
  "triage-writing-lessons": "整理写作经验",
};

const actionLabels: Readonly<Record<string, string>> = {
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
  "run-photo-reading": "读照片",
  "run-report-ledger": "判断、关联、审查、写作与总编",
  "run-reporting-preparation": "解析与准备原始资料",
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
