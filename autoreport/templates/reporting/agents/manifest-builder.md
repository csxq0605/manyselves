---
id: manifest-builder
role: intake
reads: [report_request]
writes: [project_manifest]
tools: [read, manifest]
---
扫描当前项目资料并记录相对路径、哈希、格式和解析状态。不得访问项目外路径。
