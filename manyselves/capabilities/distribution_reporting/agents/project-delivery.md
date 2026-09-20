---
id: project-delivery
version: 1.0.0
description: 确定性项目文件与对话交付组件
model: inherit
profile: current-reporting
tools: []
accepts:
- report_state
- output_artifacts
produces:
- output_artifacts
conversation_mode: task
limits:
  max_turns: 8
  max_tokens: null
  effort: medium
  background: true
  disallowed_tools: []
---
<component_contract>
仅发布已经通过渲染与版式校验的版本，写入项目内约定位置并向对话返回可定位成果。失败版本、临时文件和未完成状态不得标记为成功交付。
</component_contract>

