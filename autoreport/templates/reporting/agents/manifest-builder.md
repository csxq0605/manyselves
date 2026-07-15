---
name: manifest-builder
description: 确定性项目资料清单构建组件
reads: [report_request]
writes: [project_manifest]
tools: []
---
<component_contract>
在项目边界内枚举输入资料，记录相对路径、内容哈希、格式、版本线索和处理状态。它不解释资料含义，不进行安全判断，也不以语言模型人格运行。
</component_contract>
