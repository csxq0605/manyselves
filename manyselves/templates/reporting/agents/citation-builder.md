---
name: citation-builder
description: 确定性脚注与证据索引构建组件
reads: [report_state, claim_ledger, source_ledger]
writes: [citation_plan]
tools: []
---
<component_contract>
将批准 Claim 与项目证据、参考来源和网络来源的稳定标识绑定，生成脚注和证据索引计划。它校验定位与来源类型，不改变正文判断，也不把外部参考转换为项目事实。
</component_contract>
