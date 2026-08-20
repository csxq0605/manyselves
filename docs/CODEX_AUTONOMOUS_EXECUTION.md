# Codex 自主连续实施与人工验收门

> 文档性质：Manyselves 运行时抽取项目的 Codex 总控协议
>
> 目标：Codex 在一个长期实施分支中按主方案自动、顺序推进，不要求人工逐个派发工作包；只在必须由人完成真实环境验证、架构取舍或默认路径切换的关键节点停下
>
> 主方案：[`docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md`](architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)

## 1. 执行模式

本项目采用“自主连续实施 + 人工验收门”，而不是“一次 Codex 任务只做一个工作包”。

Codex 接受一次总任务后，应：

1. 从 `WP-00` 开始读取主方案；
2. 按依赖顺序推进后续工作包；
3. 每个工作包或更小垂直切片独立提交；
4. 测试通过且未到人工验收门时，自动进入下一工作包；
5. 在同一个长期实施分支和同一个 Draft PR 中持续更新；
6. 仅在本文规定的人工验收门、真实阻塞或不可证明的兼容风险处停止；
7. 人工批准后，从已有分支、状态记录和提交继续，不重新规划已完成工作。

工作包仍然是：

- 设计和代码范围边界；
- 测试边界；
- 提交边界；
- 回滚边界。

工作包不再是每次都需要用户重新下达的任务边界。

## 2. 分支和 PR 模型

基线分支：

```text
agent/declarative-runtime-plan
```

Codex 应创建或使用一个长期实施分支，例如：

```text
agent/declarative-runtime-implementation
```

并创建一个以 `agent/declarative-runtime-plan` 为 base 的 Draft PR。

执行期间：

- 一个工作包可以有一个或多个聚焦提交；
- 提交信息应包含工作包编号，例如 `WP-02: add minimal workflow compiler`；
- 不为每个工作包重新创建 PR；
- 不把多个工作包压成一个无法回滚的大提交；
- 人工验收门前必须推送全部提交；
- 未经人工批准，不得合并 PR、切换生产默认路径或删除 Legacy Reporting Runner。

## 3. 唯一执行状态记录

为了支持 Codex 上下文耗尽、任务中断或由新的 Codex 会话接续，实施分支只维护一份人工可读状态文件：

```text
docs/implementation/RUNTIME_EXTRACTION_STATUS.md
```

该文件不是运行时 Manifest，也不复制 Event Log。它只记录实施项目状态：

```text
Current work package
Last completed work package
Current branch and latest commit
Completed commits
Tests actually run
Open research decisions
Current human gate
Human gate result
Known blockers
Next automatic action
```

更新时机仅限：

- 完成一个工作包；
- 到达人工验收门；
- 人工验收结果返回；
- 出现真实阻塞；
- Codex 需要结束当前会话并交给新会话接续。

不得为每个 Action、测试或代码修改再创建额外状态清单。

## 4. 自主推进规则

Codex 在以下条件全部满足时必须自动继续，不询问用户：

- 当前工作包完成条件满足；
- 聚焦测试和受影响测试通过；
- 没有到达本文定义的人工验收门；
- 没有引入需要人工选择的新生产依赖；
- 没有发现现有行为与计划不一致且无法由测试解决；
- 没有需要真实 Provider、真实项目资料、浏览器或服务器环境才能验证的事项；
- 下一工作包的依赖已满足。

Codex 不得因为以下原因停下询问：

- 即将从一个工作包进入下一个工作包；
- 需要创建常规测试或适配器；
- 需要阅读更多当前仓库源码；
- 可以通过官方源码、官方文档或最小 POC 自行解决的技术问题；
- 非破坏性的实现细节存在多个合理选择，但主方案和测试足以决定。

## 5. 自动研究规则

实施中出现调研触发条件时，Codex应先自行调研，再决定是否继续。

调研顺序：

1. 当前仓库源码和测试；
2. 官方 GitHub 源码与测试；
3. 官方文档或设计说明；
4. 隔离的最小 POC；
5. 将影响实现选择的结论写入 `docs/research/decisions/`。

如果调研后可以在不改变既定架构边界的情况下确定方案，Codex应继续实施，不等待人工确认。

只有以下情况进入架构人工门：

- 需要新增 Microsoft Agent Framework、LangGraph、Burr、Temporal、Restate 或其他生产运行时依赖；
- 两种方案会形成明显不同的长期公共接口；
- 需要改变现有 Run、Conversation、Tool Result 或 Recovery 的兼容语义；
- 需要放弃已写入主方案的关键不变量；
- 官方来源和 POC 仍不能排除重大不确定性。

如果 Codex 环境无法访问必要官方来源，必须记录精确调研问题和所需来源后停止，不得凭记忆编造结论。

## 6. 人工验收门

人工门是执行程序中的显式节点。除这些节点和真实阻塞外，Codex应持续推进。

### HG-00：真实 Legacy 基线

**到达条件：** `WP-00` 的 Characterization、语义 Trace 和测试基线完成。

**Codex 自动完成：**

- 冻结 Fake/Scripted Provider 下的稳定语义 Trace；
- 列出真实报告运行的固定输入、模型、配置和输出检查项；
- 生成可执行的人工测试说明；
- 推送提交并更新状态文件。

**人工执行：**

- 使用当前 Legacy Reporting Runner、真实 Provider 和代表性项目资料运行一次完整报告；
- 确认模块、审查、Cross、Chief、Final、恢复和 DOCX 交付均真实可用；
- 保存基线 Run ID、最终产物和实际成本结果。

**批准后：** Codex 自动继续 `WP-01`。

### HG-01：编排底座和依赖选择

**到达条件：** 只有在研究或 POC 表明必须选择外部框架或新增生产运行时依赖时触发；如果自研轻量 Compiler/Executor 可以继续，则不触发。

**Codex 自动完成：**

- 对候选方案完成隔离 POC；
- 比较当前 AgentLoop、Tool、Artifact、Recovery、部署、依赖体积和迁移成本；
- 给出推荐、拒绝方案和回退路径；
- 不把 POC 接入生产路径。

**人工执行：**

- 选择 `adopt`、`adapt` 或 `do-not-adopt`；
- 批准是否新增生产依赖和公共接口方向。

**批准后：** Codex按决定继续当前工作包。

### HG-02：中立声明式运行时实测

**到达条件：** `WP-01` 至 `WP-06` 完成；Definition、Loader、Registry、Contract、最小 Compiler、Tool、Conversation、InvokeAgent Recovery 和控制流已经形成一条不依赖 reporting 的中立工作流。

**Codex 自动完成：**

- 运行全部自动测试；
- 提供一个与配电报告无关的样例 Capability；
- 提供本地运行命令、预期状态变化、恢复触发方式和输出 Schema；
- 证明 Kernel Import Boundary 通过。

**人工执行：**

- 在真实本地环境使用一个真实 Provider 运行中立工作流；
- 人为触发一次结构化输出纠正或 Continuation；
- 检查同 Conversation 恢复、Tool 调用、最终结构化输出和日志。

**批准后：** Codex 自动继续 `WP-07`。

### HG-03：单模块真实 Agent 等价验证

**到达条件：** `WP-07` 完成，单个配电报告模块 Lane 已迁移到新定义/编译/执行路径，并保留 Legacy 路径。

**Codex 自动完成：**

- 使用 Fake/Scripted Provider 证明 Legacy 与新路径语义 Trace 等价；
- 提供 Feature Flag 和回退方式；
- 提供单模块真实测试说明；
- 不切换默认执行路径。

**人工执行：**

- 对同一模块和同一份资料分别运行 Legacy 与新路径；
- 检查原作者/原审查会话复用、Finding、返修、Recheck、结果结构和实际内容；
- 检查模型漏提交或输出截断时的新路径恢复。

**批准后：** Codex 自动继续 `WP-08`。

### HG-04：完整报告 Shadow Run

**到达条件：** `WP-08` 和 `WP-09` 完成，五模块、Barrier、Cross、Chief、Final 和 Delivery 已能通过新路径执行，但仍由 Feature Flag 控制。

**Codex 自动完成：**

- 全部自动化等价测试通过；
- 旧路径继续保持默认；
- 准备同输入的完整报告 Shadow/paired run 说明；
- 列出必须比较的身份、合同、Gate、恢复、Markdown、DOCX 和成本项。

**人工执行：**

- 使用真实 Provider 和代表性项目资料分别运行 Legacy 与新路径；
- 验证 Cross Finding 准确回写、原 Conversation 保持、Local Regression、最终审计和交付；
- 比较结果质量、失败恢复、耗时和成本；
- 完成至少一次中途人工补充或模型纠正场景。

**批准后：** Codex 自动继续 `WP-10`，但仍不得删除 Legacy 路径。

### HG-05：默认路径切换批准

**到达条件：** `WP-10` 完成，配电报告已经成为独立参考 Capability，新旧路径兼容测试和真实 Shadow Run 均通过。

**Codex 自动完成：**

- 提供默认路径切换 diff；
- 提供 Feature Flag、回滚和旧 Run 兼容说明；
- 列出仍保留的 Legacy 组件；
- 不自行切换默认值。

**人工执行：**

- 批准 `keep-legacy-default`、`enable-new-default` 或 `revise`；
- 若批准新默认，执行一次真实完整报告回归。

**批准后：** Codex按决定继续 `WP-11` 和 `WP-12`。

### HG-06：服务端封装与第二 Capability 验收

**到达条件：** `WP-11` 和 `WP-12` 完成。

**Codex 自动完成：**

- FastAPI/React 使用通用 Run、Interaction、Output 和 Cost 投影；
- 第二个中立 Capability 不依赖 reporting；
- 自动化测试、构建和部署 Smoke 通过；
- 提供服务器人工验收说明。

**人工执行：**

- 在内部服务器部署；
- 分别运行配电报告和第二 Capability；
- 检查项目、会话、输出、成本、失败恢复和浏览器交互；
- 确认新 Capability 不包含隐式报告角色或流程。

**批准后：** 进入最终清理、文档更新和合并准备。

## 7. 人工门到达时 Codex 必须输出什么

到达人工门时，Codex必须先完成并推送当前工作，然后停止。回复必须包含：

```text
Gate ID
Why this gate is required
Branch
Latest commit
Draft PR
Completed work packages
Changed files
Automated tests actually run
Automated test results
Exact human setup
Exact human commands or UI steps
Expected results
Artifacts/logs to inspect
Pass criteria
Fail criteria
Rollback steps
Known limitations
Resume command/token
```

Codex不得只说“请测试一下”。

## 8. 人工批准和恢复

人工回复应使用明确的控制语句：

```text
APPROVE_GATE HG-XX
REJECT_GATE HG-XX: <reason>
SUPPLEMENT_GATE HG-XX: <new facts or artifacts>
```

Codex收到批准后应：

1. 将人工结果写入唯一状态文件；
2. 不重复已经完成的工作包；
3. 从 Gate 定义的下一工作包继续；
4. 继续自动执行直到下一个人工门。

如果由新的 Codex 会话接续，启动提示只需要求它：

```text
读取 AGENTS.md 和 docs/implementation/RUNTIME_EXTRACTION_STATUS.md，
确认当前分支和最新提交，从尚未通过的当前 Gate 或 Next automatic action 继续。
```

## 9. 真实失败与停止条件

除人工门外，Codex仅在以下情况停止：

- 自动测试失败且经过定位后无法在当前工作包内修复；
- 新路径无法证明与现有行为兼容；
- 需要删除既有恢复能力才能继续；
- 发现旧 Run、输出合同或交付文件会不兼容；
- Compiler 无法确定输入输出关系；
- 新抽象泄漏具体领域概念；
- 必要外部调研无法完成；
- 发生破坏性仓库操作或默认路径切换，必须人工批准；
- 当前方案本身存在需要用户重新决策的矛盾。

停止前必须：

- 保存可复现测试或失败 POC；
- 提交非破坏且已验证的部分；
- 更新唯一状态文件；
- 明确阻塞事实和下一步选项。

## 10. 完成定义

整个自主实施程序只有在以下条件全部满足时才完成：

- 通用 Definition、Contract、Workflow Compiler 和 Action/Executor Runtime 已实现；
- Agent、Tool、Conversation、Recovery、State 和 Event 已接入；
- Kernel 不含配电报告领域概念；
- 配电报告通过声明式 Capability 运行，并保持原有功能、接口、恢复和输出兼容；
- 至少一个无配电报告角色和流程的新 Capability 运行成功；
- FastAPI、React 和内部服务器部署封装完成；
- 任务完成后成本可以展示；
- 全部人工门有明确通过记录；
- Legacy 路径的保留或删除经过人工批准；
- 文档、测试和实际代码状态一致。
