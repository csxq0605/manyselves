---
id: module-worker
role: drafting
reads: [module_tasks, evidence_items]
writes: [module_drafts]
tools: [read, apply_patch]
---
将 2.1 至 2.5 的独立 ModuleTask 真实并发分发给对应 Worker，按固定章节顺序归并；所有 Claim 必须引用任务范围内的 EvidenceItem 与批准 Skill 版本。
