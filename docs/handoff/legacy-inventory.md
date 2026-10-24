# 旧记录交接盘点（Legacy Inventory）

- 盘点日期：2026-10-11
- 盘点范围：
  - 旧 Claude Code 工作阶段 `session_01BhE1RyXJqguLtszpsZobYL`（2026-10-10，标题“本科毕设仓库评估”，只有一轮问答，没有改过代码）；
  - 仓库 `csxq0605/manyselves` 的 `main@9b5193a`、全部远端分支、PR #1～#4；
  - `docs/implementation/RUNTIME_EXTRACTION_STATUS.md`（下称“状态文件”）、`AGENTS.md`、`docs/README.md`。
- 证据标注：凡是没有直接证据、靠推理得出的条目，都标为“推断”。

## 一、旧记录概况

| 来源 | 内容 | 证据 |
|---|---|---|
| 旧工作阶段 | 用户问“这个仓库适不适合作为本科毕设，可以怎么写”。助手只读了仓库，给出评估、三个题目、七章论文结构和四条下一步建议，没有提交任何内容。用户之后没有再回复。 | 会话 `session_01BhE1RyXJqguLtszpsZobYL` 的 transcript；状态 completed，分支 `main` |
| 仓库开发史 | 2026-07-31 到 2026-09-20 共 137 个提交，最终以 PR #4 合并、`9b5193a Mark product complete` 收尾 | `git log`；状态文件 Current position |

## 二、清单 ① 已完成的事

| # | 事项 | 证据 |
|---|---|---|
| 1 | 声明式运行时架构落地：无状态 Kernel、Workflow Compiler、通用 Runtime、两个 Capability（`distribution_reporting`、`parameter_adjustment`），由 FastAPI/React 外壳驱动 | `manyselves/kernel`、`manyselves/runtime`、`manyselves/capabilities/*`；PR #4 <https://github.com/csxq0605/manyselves/pull/4>；合并提交 `a2f5b31` |
| 2 | PR #4 合并到 `main`，产品被标记为完成 | `a2f5b31 Merge PR #4`、`9b5193a Mark product complete`（2026-09-20）；状态文件第 7、21～27 行：“产品完成……Next automatic action: None; normal maintenance only” |
| 3 | 六个报告入口，其中包括 `revise_report` 修订；修订带 `impact_mode=none|auto|confirm` 影响分析，confirm 模式下的 WAITING 和 `accept_selected` 都已接入产品流程 | `194f4a0`、`5937704`；状态文件 M9.101、M9.108 |
| 4 | 修订时会读取当前输入，并保留来源可追溯性；报告要求和当前证据会经过 Cross 修订传递下去 | `e3639a7`、`c423092`、`6ab2bec`；状态文件 M9.102～M9.104 |
| 5 | DOCX 交付规则：文件打不开时阻断发布；内容、标题或 token 有问题时照常发布并附带 `validation_warnings` | 状态文件 M9.106；`79ca5ef` 的策略已恢复 |
| 6 | main 独有提交的对账（24 个提交），部署、上传、目录 ZIP、子路径等修复 | `d03978f`；状态文件 M9.106 |
| 7 | 真实 Provider、项目和浏览器验收：全量 Run 和修订 Run 都完成到交付，用户最终确认验收 | 状态文件 M9.104 Final closeout、M9.108；运行产物不在仓库内（推断：在用户本机的独立项目 Work 目录） |
| 8 | 前端：会话状态栏精简、成本/输出浮层、完成卡和 MiMo 成本投影 | `d455631`、`a5d79ad`、`2a97aa0`；状态文件 M9.97～M9.99 |
| 9 | 文档收敛为 README + 操作手册；补充部署和离线打包文档 | `56a88ad`、`7cb207c`；`docs/OPERATION_MANUAL.md`、`docs/RUNNING.md`、`docs/OFFLINE_PACKAGING_REMOTE_DEPLOYMENT.md` |
| 10 | 毕设可行性评估（只有口头建议，没有文件） | 旧工作阶段的最终回复 |

当前规模（本次实测）：`manyselves/` Python 109,803 行，`tests/` Python 67,610 行，`frontend/` TS/TSX 25,651 行；`VERSION` 为 `v0.0.2`。

## 三、清单 ② 已定下的目标、决定和规则

| # | 内容 | 来源 |
|---|---|---|
| 1 | 最终形态是一个无状态、业务无关的 Kernel，一个通用 Compiler/Runtime，一个文件定义系统，各 Capability 自带领域 Runtime/Tools，再加一个通用 FastAPI/React 外壳 | `AGENTS.md` §2；`docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md` |
| 2 | 依赖方向是 kernel ← runtime ← capability runtime ← application/webapi；Kernel/Runtime 中不得出现业务词汇，也不得新增领域 Action | `AGENTS.md` §3、§6 |
| 3 | Recovery 语义不得退化：结构化纠正、Schema 修正、Max Token 续写、Tool 结果复用、Same-run 恢复等 | `AGENTS.md` §7 |
| 4 | Legacy Runner 和双路径兼容已废止，不再作为完成条件 | `AGENTS.md` §8 |
| 5 | 验证只跑 focused/affected 测试（`uv run ruff check`、`uv run pytest -q ... --maxfail=3`），除非用户要求，不跑全量测试 | `AGENTS.md` §10 |
| 6 | 新增门禁、Hash、CAS、锁或编排框架前，必须先得到用户同意 | `AGENTS.md` §12 |
| 7 | 判断当前状态以状态文件顶部的 Current position 为准；历史段落中的“失败/待验证”只作追溯，不代表当前状态 | `docs/README.md` 第 31 行；状态文件 M9.108 |
| 8 | 已接受的产品合同：成本只做“观察和展示”；`warn/pause` 和 `max_*` 是保留输入，不保证硬停止 | 状态文件第 24 行 |
| 9 | 新协调规范（2026-10-11 用户发布）：每个讨论串产出交接文档 `docs/handoff/<主题>.md`；`docs/STATUS.md` 和 `docs/handoff/INDEX.md` 只由“状态与记录”讨论串维护 | 用户在项目时间线发布的规范 |
| 10 | 毕设方向只是建议，尚未确定：推荐题目《基于声明式定义的可恢复多智能体应用运行时设计与实现》 | 旧工作阶段（推断：用户没有回应，所以未定） |

## 四、清单 ③ 未完成或计划中的事（按优先级）

| 优先级 | 事项 | 依据 |
|---|---|---|
| P0 | 确定这个仓库是否用作毕设、选哪个题目；先向导师确认两件事：AI 生成代码是否允许使用，合作开发时个人贡献怎样界定 | 旧工作阶段“接下来要做的事”第 1 条；提交作者为 csxq0605/csxq 97 次、liu_jiawei 40 次 |
| P0 | 统一状态文件的规则：`AGENTS.md` 规定 `RUNTIME_EXTRACTION_STATUS.md` 是“唯一”状态文件，新规范又要求建立 `docs/STATUS.md` | 见第五节矛盾 1 |
| P1 | 整理“我的贡献清单”（以提交和设计文档为证据） | 旧工作阶段第 2 条 |
| P1 | 写开题报告（研究内容、技术路线、进度安排） | 旧工作阶段最后的提议 |
| P1 | 设计并采集第 6 章实验数据：中断恢复成功率、token 节省、新增 Capability 的改动量、声明式与硬编码的改动量对比、成本统计（`usage_ledger.py`） | 旧工作阶段第 3 条 |
| P2 | 从源码整理第 4 章的架构图和时序图（Compiler、Kernel 状态转换、Recovery） | 旧工作阶段的提议 |
| P2 | 准备公开或脱敏的演示用例；真实业务数据和模板不能进论文 | 旧工作阶段第 4 条、风险 3 |
| P3 | 产品层的已知限制（产品已验收，这些都不是阻塞项）：成本硬停止未实现；应用联网搜索缺 Brave key；模型回忆的标准条款未经核实；Word 逐页版面和下载性能未独立验收；一个 Windows 打开文件替换测试被 deselect | 状态文件第 24 行、M9.104、M9.106 |
| P3 | 清理远端旧分支：`agent/declarative-runtime-plan` 比 main 多 7 个提交（推断：内容已通过其他提交并入）；`docs/handoff-todo-20260812` 有 1 个未合并的文档提交。删除前需要用户同意 | `git rev-list --count origin/main..<branch>` |

## 五、矛盾与无法确认的地方

1. **状态文件的“唯一性”冲突。** `AGENTS.md` §15 规定 `RUNTIME_EXTRACTION_STATUS.md` 是唯一的跨会话状态文件，新协调规范却要求 `docs/STATUS.md`。需要决定新文件是取代它、引用它，还是只记录毕设和协调层面的进度。
2. **`AGENTS.md` 仍是“实施中”的协议。** §9、§15、§17 要求长期分支 `agent/declarative-runtime-implementation`、Draft PR 和连续切片，但状态文件已经写明“产品完成，PR #4 已合并，当前分支 main”。`AGENTS.md` 没有随完成状态更新。
3. **PR 合并标记不一致。** GitHub API 显示 PR #3 和 #4 都是 `merged: false` 但带有 `merged_at`。另外 PR #3 显示在 2026-09-16 合并，main 上却只有 PR #4 的合并提交 `a2f5b31`。推断：合并是在本地完成后推送的，PR 是手动关闭的。
4. **“产品完成”与历史失败记录并存。** M9.103 记录全量 Run 的内容验收 FAILED（示例被删掉），M9.104 写明专业条款未核实、Word 版面未验收；M9.108 则凭用户确认宣告完成。用户确认这件事只记录在状态文件里，没有其他外部证据可查。
5. **代码规模口径。** 旧工作阶段写的是“Python 约 11 万行（测试约 6.8 万行）”，容易读成测试包含在 11 万行之内。实测源码 109,803 行、测试另有 67,610 行，两者是分开的。
6. **毕设是否成立无法确认。** 旧工作阶段只给了建议，用户没有回应。导师要求、学校对 AI 生成代码的规定、liu_jiawei 的角色（合作者还是他人），记录里都没有。
7. **真实运行产物不在仓库。** 验收证据所引用的 Run 目录、`Work/acceptance-*.json` 都在用户本机的独立项目中（推断），本次无法核对。
8. **根目录 `ProductCapabilities/` 与 `manyselves/capabilities/` 不是一回事。** 前者只有 `skills/feedback`，`docs/CONFIGURATION.md` 第 203 行称它为“Product capability material”，`artifact_access.py` 第 71 行引用了这个目录名。写论文时不要把它和 Capability 机制混为一谈。它具体做什么，本次没有深入核实。
