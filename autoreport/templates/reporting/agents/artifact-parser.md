---
id: artifact-parser
role: intake
reads: [project_manifest]
writes: [parsed_artifacts]
tools: [read]
---
解析项目中的工作簿并保留工作表和单元格定位；失败项必须留在 manifest 中。
