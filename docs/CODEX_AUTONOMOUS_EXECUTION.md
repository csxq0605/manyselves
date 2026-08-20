# Codex 自主连续实施与最终真实测试

> 文档性质：Manyselves 运行时抽取项目的 Codex 总控协议
>
> 目标：Codex 在一个长期实施分支中按主方案自动、顺序推进，不要求人工逐个派发工作包；四阶段迁移全部完成后才统一请求一次真实环境测试
>
> 主方案：[`docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md`](architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)

## 1. 执行模式

本项目采用“四阶段自主连续实施 + 最终一次真实测试”，而不是“一次 Codex 任务只做一个工作包”。

Codex 接受一次总任务后，应：

1. 从 `WP-00` 开始读取主方案；
2. 按依赖顺序推进后续工作包；
3. 每个工作包或更小垂直切片独立提交；
4. focused tests 和受影响测试通过后，自动进入下一工作包；
5. 在同一个长期实施分支和同一个 Draft PR 中持续更新；
6. 不设置中途人工验收断点，仅在真实阻塞或不可证明的兼容风险处停止；
7. 四阶段和 `WP-00`～`WP-12` 全部完成后，才生成最终真实测试交接。

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
- 每个工作包或可验证垂直切片完成后必须推送提交；
- 最终真实测试完成前，不得合并 PR、切换生产默认路径或删除 Legacy Reporting Runner。

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
Current migration stage
Final real-test status
Known blockers
Next automatic action
```

更新时机仅限：

- 完成一个工作包；
- 完成一个迁移阶段；
- 最终真实测试结果返回；
- 出现真实阻塞；
- Codex 需要结束当前会话并交给新会话接续。

不得为每个 Action、测试或代码修改再创建额外状态清单。

## 4. 自主推进规则

Codex 在以下条件全部满足时必须自动继续，不询问用户：

- 当前工作包完成条件满足；
- 聚焦测试和受影响测试通过；
- 没有引入需要人工选择的新生产依赖；
- 没有发现现有行为与计划不一致且无法由测试解决；
- 当前事项可以用 Fake/Scripted Provider、离线项目夹具或自动化投影测试验证；
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

以下情况视为架构研究触发条件：

- 需要新增 Microsoft Agent Framework、LangGraph、Burr、Temporal、Restate 或其他生产运行时依赖；
- 两种方案会形成明显不同的长期公共接口；
- 需要改变现有 Run、Conversation、Tool Result 或 Recovery 的兼容语义；
- 需要放弃已写入主方案的关键不变量；
- 官方来源和 POC 仍不能排除重大不确定性。

本轮迁移不新增生产编排依赖。研究和 POC 可以继续，但必须保持隔离；实现优先使用当前依赖和内部轻量 Compiler/Executor。如果现有边界内确实无法继续，记录精确阻塞事实和所需来源后停止，不得凭记忆编造结论。

## 6. 四阶段连续迁移

本轮实施不设置中途人工验收断点。四阶段只作为依赖、提交和状态记录边界，完成后自动进入下一阶段：

1. **Definition 阶段：** `WP-00`～`WP-01`，冻结基线并实现 Definition Models、Loader、Registry 和 Contract Adapter；
2. **无状态 Kernel/Runtime 阶段：** `WP-02`～`WP-06`，实现 Workflow State、Compiler、Tool、Conversation、Agent Recovery 和声明式控制流；
3. **Reporting 迁移阶段：** `WP-07`～`WP-10`，迁移单 Lane、模块 Cohort、Cross/Chief/Final/Delivery，并形成独立配电报告 Capability；
4. **通用产品化与第二能力阶段：** `WP-11`～`WP-12`，完成通用 FastAPI/React 投影和第二个中立 Capability。

每阶段必须：

- 先 Characterization，再实现；
- 只运行 focused tests 和受影响测试集合，除非用户明确要求全量回归；
- 每个可验证切片独立提交、推送并更新 Draft PR；
- 更新唯一状态文件后自动继续；
- 不调用真实 Provider，不要求真实项目、浏览器或服务器人工验收。

原先分散在各阶段的真实环境检查项合并为最终真实测试矩阵，不再阻断工作包依赖，也不再使用中途批准口令。

## 7. 最终真实测试交接

只有四阶段、`WP-00`～`WP-12`、自动行为等价验证和文档同步全部完成后，Codex 才停止并请求一次真实测试。交接必须包含：

```text
Branch
Latest commit
Draft PR
Completed work packages and stages
Changed architecture and compatibility adapters
Focused/affected tests actually run
Exact real Provider and project setup
Exact CLI, UI, browser and server steps
Legacy/declarative paired-run procedure
Expected semantic, output, recovery and cost results
Artifacts/logs to inspect
Pass criteria
Fail criteria
Rollback steps
Known limitations
```

该最终真实测试统一覆盖原各人工门中的 Legacy 基线、中立 Runtime、单模块等价、完整报告 Shadow、通用 API/UI、第二 Capability、恢复、DOCX 和成本检查。Codex 不得提前要求用户执行其中任何一部分。

最终真实测试前保持 Legacy Reporting Runner 可用并维持当前默认路径；声明式路径必须完整实现且可显式选择。是否切换默认路径只在最终真实测试结果可用后处理，不阻断 `WP-11` 或 `WP-12` 的实现。

## 8. 中断和恢复

如果由新的 Codex 会话接续，启动提示只需要求它：

```text
读取 AGENTS.md 和 docs/implementation/RUNTIME_EXTRACTION_STATUS.md，
确认当前分支和最新提交，从 Current migration stage / Next automatic action 继续，
不要重复已完成工作包，也不要创建中途人工验收断点。
```

## 9. 真实失败与停止条件

连续实施期间，Codex 仅在以下情况停止：

- 自动测试失败且经过定位后无法在当前工作包内修复；
- 新路径无法证明与现有行为兼容；
- 需要删除既有恢复能力才能继续；
- 发现旧 Run、输出合同或交付文件会不兼容；
- Compiler 无法确定输入输出关系；
- 新抽象泄漏具体领域概念；
- 必要外部调研无法完成；
- 必须执行破坏性仓库操作；
- 在不新增生产编排依赖、不改变既定长期公共边界的前提下确实无法继续；
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
- 四阶段和 `WP-00`～`WP-12` 均有自动验证与聚焦提交记录；
- 已生成唯一最终真实测试交接，且此前没有要求中途人工验收；
- 最终真实测试前 Legacy 路径保持可用且仍为默认；
- 文档、测试和实际代码状态一致。
