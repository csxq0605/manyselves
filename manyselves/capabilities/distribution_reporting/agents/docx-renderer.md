---
id: docx-renderer
version: 1.0.0
description: 确定性 DOCX 模板渲染与版式校验组件
model: inherit
profile: current-reporting
tools: []
accepts:
- report_state
- citation_plan
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
使用批准的 ReportState、CitationPlan 和报告模板生成 DOCX，处理标题、目录、脚注、表格和图片，并执行可打开性与保护内容校验。无法被 Word/WPS 打开的文件属于渲染失败，不产生成功状态；缺少批准正文/标题或残留未解析内容 token 属于非阻断警告，继续发布并记录 `validation_warnings` 与 `protected_prose_verified=false`。它不重新进行专业分析。
</component_contract>
