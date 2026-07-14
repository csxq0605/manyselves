---
id: artifact-parser
role: intake
reads: [project_manifest]
writes: [project_manifest, parsed_artifacts]
tools: [parse_supported_files]
---

解析受支持的项目文件，并为每条输出保留来源位置。
