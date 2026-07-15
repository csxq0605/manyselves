---
name: revision-router
description: 确定性局部修订路由组件
reads: [module_drafts, review_issues]
writes: [module_tasks]
tools: []
---
<component_contract>
依据问题的责任模块和修订预算生成局部返工任务。它保持其他已完成模块不变，达到上限时保留成果并显式标记未解决问题，不把超限状态伪装成通过。
</component_contract>
