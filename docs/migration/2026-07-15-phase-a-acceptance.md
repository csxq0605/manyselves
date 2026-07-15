# Phase A 2.4 纵向迁移验收矩阵

权威目标为 `work/local-multi-agent-report-plan.html`，行为基线为 Harness 0.2.6、插件源码/包 0.2.8 与当前知识库。`work/` 全程只读；真实资料测试先复制到 pytest 临时项目的 `Inputs/`。

| 要求 | 状态 | 实现/证据 |
|---|---|---|
| AutoReport 为唯一 GUI/runtime | 通过 | `ReportingService` 由 Main Agent tool、MessageBus 与 TaskBoard 执行；未引入 Nexgent/Dify runtime |
| 只扫描项目 `Inputs/` | 通过 | `intake/manifest.py`；根目录、Work、Outputs 回归测试 |
| S2-1 专用映射 | 通过 | 25 个真实资料项映射具备情况、有效性与精确单元格 |
| S4-4 专用映射 | 通过 | 真实资料映射 101 条 2.4 证据；保留配电房/柜号/行列 |
| WPS DISPIMG 图片 | 通过 | 21 个已引用图片 ID 全部解析到 24 个媒体资产；同排绑定 |
| S4-6 既有结论不压过原始事实 | 通过 | S4-6 evidence `needs_confirmation=true`、置信度低于 1 |
| 固定 2.1-2.5 taxonomy | 通过 | `taxonomy.py`；未知编号与跨模块编号被拒绝 |
| 子模块 Coverage | 通过 | 无关证据不能让 2.4 READY；`ask/block/skip/draft` 回归 |
| 2.4 Skill/Worker | 通过 | 14 个子模块均解析到版本化 Skill；Claim 保存 Evidence/Skill ID |
| 96.99% 阈值门禁 | 通过 | 计算值 96.992%；Worker 明确“未达到100%”；Auditor 阻塞“当前已过载” |
| 缺图处理 | 通过 | NG 无同排照片生成去重 warning；Renderer 输出缺图占位 |
| 局部返工 | 通过 | 最大两轮；只替换责任子模块，其他 Claim ID 不变 |
| ReportState | 通过 | 未批准草稿、blocking issue、未知 Evidence 均不能进入 Renderer |
| DOCX 模板 | 通过 | 交接模板 SHA256 `ac956b5d452d2acffcdf21776ee7e889d6b7afc307d63b8d0d72ef731f9ffe87` |
| DOCX 结构与打开检查 | 通过 | 标题、模块/子模块标题、审校表、图片/缺图、输出哈希自动验证 |
| 真实三表端到端 | 通过 | `tests/reporting/integration/test_real_phase_a_24.py`，生成已审计 DOCX |
| 逐页视觉检查 | 通过 | LibreOffice 转 PDF 后 34 页 PNG 全检；中文字体嵌入、无裁切、无图片越界 |

## 验证命令

```bash
PYTHONPATH="$PWD" PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen .venv/bin/pytest -p no:cacheprovider --basetemp=.build-tmp/reporting tests/reporting -q
.venv/bin/ruff check autoreport/core/reporting autoreport/core/tools/reporting_tool.py tests/reporting
```

视觉验证使用显式 Fontconfig 指向 macOS 系统中文字体；最终 PDF 嵌入 `HiraginoSansGB-W3/W6`。Renderer 同时把所有段落样式的东亚字体固定为 `Hiragino Sans GB`，避免继承交接模板中不可用的“微软雅黑 Light/黑体”。
