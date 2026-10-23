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

/** A step's name and, for the steps that take a while, one line on what it does. */
export interface ReportingStepText {
  readonly label: string;
  readonly note?: string;
}

const steps: Readonly<Record<string, ReportingStepText>> = {
  // full-report
  "initialize-full-report": { label: "初始化报告" },
  "project-preparation-for-full-report": { label: "准备项目资料" },
  "run-reporting-preparation": { label: "解析原始资料", note: "程序解析评估表、照片和模板，把资料归并成证据。" },
  "attach-reporting-preparation": { label: "装配资料准备结果" },
  "project-preparation-for-readiness": { label: "准备证据检查" },
  "run-evidence-readiness": { label: "证据完整性检查", note: "检查资料够不够写报告；缺得多时停下等你确认。" },
  "attach-readiness-preparation": { label: "整理证据检查结果" },
  "run-photo-reading": { label: "读照片", note: "每张照片只读一次，对照它绑定的表格记录；四路并行。" },
  "attach-readiness-to-context": { label: "装配报告上下文" },
  "build-reporting-state": { label: "构建报告上下文" },
  "run-report-ledger": { label: "判断、关联、审查、写作与总编", note: "报告主体：先逐行判断成台账，审查定稿后一次写成正文。" },
  "publish-full-report": { label: "发布报告结果" },
  // preparation
  "build-manifest": { label: "建立文件清单" },
  "prepare-report-taxonomy": { label: "读取评估表结构" },
  "parse-artifacts": { label: "解析文件" },
  "normalize-evidence": { label: "归并证据" },
  "evaluate-coverage": { label: "检查资料覆盖" },
  "load-special-topic-plan": { label: "读取专项计划" },
  "persist-preparation-snapshot": { label: "保存准备结果" },
  "finalize-preparation": { label: "完成资料准备" },
  "restore-preparation-snapshot": { label: "复用准备结果" },
  "finalize-preparation-after-restore": { label: "完成资料准备" },
  // evidence readiness
  "evaluate-readiness": { label: "评估证据完整性" },
  "route-readiness": { label: "决定是否继续" },
  "publish-readiness-before-decision": { label: "发布检查结果" },
  "request-evidence-decision": { label: "等待确认缺失资料", note: "资料缺口需要你决定：补充，或按现有资料继续。" },
  "apply-evidence-decision": { label: "应用你的决定" },
  "route-decision": { label: "按决定继续" },
  "prepare-evidence-supplement": { label: "准备补充资料" },
  "publish-readiness": { label: "发布检查结果" },
  // photo reading
  "prepare-photo-reading": { label: "分配照片" },
  "photo-reading-requires-agent": { label: "判断是否需要读照片" },
  "photo-reading-lanes": { label: "四路读照片", note: "照片分成四路同时读，每张一个新对话。" },
  "reduce-photo-reading": { label: "汇总照片读数" },
  // report ledger
  prepare: { label: "准备判断材料", note: "程序按判断组整理表格记录、知识库条目和照片读数。" },
  "judge-modules": { label: "各模块判断", note: "每个判断组的作者在自己的对话里逐行判断，写进判断台账。" },
  "build-ledger": { label: "合成判断台账", note: "程序把各组判断合成一本台账。" },
  "plan-relate": { label: "安排关联", note: "程序给每位作者挑出同对象、同房间的候选行。" },
  "relate-groups": { label: "作者关联", note: "作者回到判断对话，把自己的行和其他模块的行连起来。" },
  "apply-relations": { label: "写入关联" },
  "plan-review": { label: "安排审查", note: "程序把台账切成模块、对象、跨模块三类审查包。" },
  "review-lenses": { label: "三路审查", note: "模块审查、对象审查、跨模块审查同时进行。" },
  "review-modules": { label: "模块审查", note: "逐模块核对判断和它引用的记录。" },
  "review-objects": { label: "对象审查", note: "核对几个模块都判过的同一对象。" },
  "review-cross": { label: "跨模块审查", note: "核对关联，并提出作者漏掉的关联。" },
  "gather-reviews": { label: "汇总审查意见", note: "程序按行合并三路意见。" },
  "settle-modules": { label: "作者处理审查意见", note: "只有阻断问题才返修；程序复查改过的行，不过再退一次。" },
  "finalize-ledger": { label: "台账定稿" },
  "apply-story": { label: "串主线", note: "程序把成立的关联连成主线，写作前定好。" },
  draft: { label: "写正文与总编", note: "各写作组写第二章，总编同时写第三章和第一章。" },
  "write-modules": { label: "模块写作", note: "每位写作者在自己的对话里写一组小节。" },
  "check-report": { label: "程序检查正文" },
  "edit-pass": { label: "编辑审读", note: "编辑按写作者审读；意见等事实核对后一起改。" },
  "chief-summary": { label: "总编写第三、一章", note: "总编合并行动项写第三章和第一章，再分包核对事实。" },
  "finish-draft": { label: "修订收尾", note: "总编修第一、三章并写第四章；同时作者修错行、写作者按意见改一次。" },
  "chief-finish": { label: "总编修订与第四章" },
  "rows-finish": { label: "修错行与修订小节" },
  compose: { label: "组装报告", note: "程序拼出完整报告并渲染成文档。" },
  // child workflows (shared step names)
  judge: { label: "判断本组各行" },
  relate: { label: "关联本组的行" },
  review: { label: "审查" },
  merge: { label: "合并意见" },
  revise: { label: "返修" },
  recheck: { label: "程序复查" },
  "revise-again": { label: "再改一次" },
  close: { label: "收尾" },
  "each-section": { label: "逐节写作" },
  write: { label: "写一节" },
  edit: { label: "编辑审读" },
  hold: { label: "等事实核对" },
  "chapter-3": { label: "写第三章" },
  "check-3": { label: "程序检查第三章" },
  "revise-3": { label: "修改第三章" },
  "chapter-1": { label: "写第一章" },
  "check-1": { label: "程序检查第一章" },
  "revise-1": { label: "修改第一章" },
  "plan-facts": { label: "切分事实核对包" },
  "fact-check": { label: "分包核对事实", note: "第一、三章切成小包，每包一个新对话核对事实。" },
  "apply-facts": { label: "汇总核对结果" },
  check: { label: "核对一包事实" },
  "fix-input": { label: "准备修订材料" },
  fix: { label: "修第一、三章" },
  "apply-fix": { label: "应用修订" },
  "chapter-4": { label: "写第四章" },
  "fix-rows": { label: "作者修错行" },
  "apply-row-fixes": { label: "写入修行" },
  "revise-sections": { label: "写作者修订小节" },
  "merge-section-revisions": { label: "合并修订" },
};

const laneStep = /^(select-photo-lane|each-photo-lane|read-photo-lane|read-photo|accept-photo-reading|record-photo-reading|fresh-photo-conversation)-(\d+)$/;
const laneLabels: Readonly<Record<string, string>> = {
  "accept-photo-reading": "程序核对读数",
  "each-photo-lane": "逐张读照片",
  "fresh-photo-conversation": "开新对话",
  "read-photo": "读一张照片",
  "read-photo-lane": "第 {n} 路",
  "record-photo-reading": "记录读数",
  "select-photo-lane": "选出本路照片",
};

export function reportingStepText(actionId: string): ReportingStepText {
  const known = steps[actionId];
  if (known) return known;
  const lane = laneStep.exec(actionId);
  if (lane) {
    const label = laneLabels[lane[1]!] ?? actionId;
    return { label: label.replace("{n}", lane[2]!) };
  }
  return { label: reportingActionLabel(actionId) };
}

const itemNouns: Readonly<Record<string, string>> = {
  "ledger-chief-fact-check": "事实包",
  "ledger-cross-review": "关联片",
  "ledger-edit-pass": "写作者",
  "ledger-module-judgment": "判断组",
  "ledger-module-relate": "判断组",
  "ledger-module-review": "模块",
  "ledger-module-settle": "判断组",
  "ledger-module-writing": "写作组",
  "ledger-object-review": "对象批",
  "ledger-row-fix": "判断组",
  "ledger-section-revise": "写作者",
};

/** What one item of a step that runs many at once is called, such as 判断组. */
export function reportingItemNoun(workflowId: string): string {
  return itemNouns[workflowId] ?? "任务";
}
