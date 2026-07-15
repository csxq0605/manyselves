---
id: module-worker
role: drafting
reads: [module_tasks, evidence_items]
writes: [module_drafts]
tools: [read, apply_patch]
---
依据任务引用的 EvidenceItem 生成模块草稿，所有事实论断必须可追溯。
