---
name: project-delivery
description: 确定性项目文件与对话交付组件
reads: [report_state, output_artifacts]
writes: [output_artifacts]
tools: []
---
<component_contract>
仅发布已经通过渲染与版式校验的版本，写入项目内约定位置并向对话返回可定位成果。失败版本、临时文件和未完成状态不得标记为成功交付。
</component_contract>
