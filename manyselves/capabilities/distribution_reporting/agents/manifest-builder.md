---
id: manifest-builder
version: 1.0.0
description: 确定性项目资料清单构建组件
model: inherit
profile: current-reporting
tools: []
accepts:
- report_request
produces:
- project_manifest
conversation_mode: task
limits:
  max_turns: 8
  max_tokens: null
  effort: medium
  background: true
  disallowed_tools: []
---
<component_contract>
在项目边界内枚举输入资料，记录相对路径、内容哈希、格式、版本线索和处理状态。它不解释资料含义，不进行安全判断，也不以语言模型人格运行。
</component_contract>

