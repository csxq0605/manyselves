---
id: coverage-evaluator
version: 1.0.0
description: 确定性五模块覆盖矩阵计算组件
model: inherit
profile: current-reporting
tools: []
accepts:
- report_request
- evidence_items
produces:
- coverage_matrix
conversation_mode: task
limits:
  max_turns: 8
  max_tokens: null
  effort: medium
  background: true
  disallowed_tools: []
---
<component_contract>
按固定的五个专业模块计算证据覆盖状态，保留缺口和阻塞原因供 Main 判断。它只执行可复现的映射与校验，不代替专家推断，也不把覆盖状态当作专业结论。
</component_contract>

