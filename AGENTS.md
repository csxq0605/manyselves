# AGENTS.md — Manyselves 最终架构实施约定

本文件适用于整个仓库。所有 Codex 实施必须先读取本文件及其引用的规范文档，并以实际源码、定义、测试和运行产物校正文档中的当前状态。

## 1. 必读顺序

1. [`docs/PROJECT_POSITIONING.md`](docs/PROJECT_POSITIONING.md)
2. [`docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md`](docs/architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)
3. [`docs/CODEX_AUTONOMOUS_EXECUTION.md`](docs/CODEX_AUTONOMOUS_EXECUTION.md)
4. [`docs/research/DECLARATIVE_RUNTIME_LANDSCAPE.md`](docs/research/DECLARATIVE_RUNTIME_LANDSCAPE.md)
5. [`docs/implementation/RUNTIME_EXTRACTION_STATUS.md`](docs/implementation/RUNTIME_EXTRACTION_STATUS.md)
6. 当前切片涉及的源码、文件定义、测试和持久化 Run 产物

这些文件分别决定产品定位、最终架构、连续执行方式、外部研究基线和当前实施位置。历史交接、旧迁移计划、代码注释或测试名称与它们冲突时，不得据此保留已经废止的兼容目标；必须检查生产调用链并更新规范或实现。

## 2. 唯一目标形态

Manyselves 的最终目标是：

> 一个无状态、业务无关的 Kernel；一个通用 Workflow Compiler 与 Executor Runtime；一个文件定义系统；多个 Capability 自己拥有的领域 Python Runtime/Tools；一个通用 FastAPI/React 产品外壳。

```text
Markdown / YAML / JSON Schema / Python Tool references
                         │
                         ▼
              Definition Loader & Registry
                         │
                         ▼
                  Workflow Compiler
                         │
                         ▼
             Resolved Plan + Stateless Kernel
                         │
                         ▼
           Generic Action / Executor Runtime
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
        Agents          Tools       Interactions
          │              │              │
          └──────────────┼──────────────┘
                         ▼
              State / Recovery / Events
                         │
                         ▼
               FastAPI / React / Outputs
```

系统只有一个 Kernel 和一个通用编译/执行语义。每个 Capability 可以拥有自己的领域模型、确定性校验、Agent Invoker、Python Tools、渲染和交付实现，但不得复制 Kernel、Compiler、Workflow Host、Run API 或前端工作区。

## 3. 强制分层与依赖方向

目标代码边界：

```text
manyselves/
├── kernel/                  # 定义、合同、编译结果、纯状态转换、端口
├── runtime/                 # 通用 Agent/Tool/Conversation/Interaction/Recovery/Event 执行
├── capabilities/
│   ├── distribution_reporting/
│   │   ├── definitions/     # 或现有等价的文件定义目录
│   │   ├── domain/          # Reporting 领域模型与规则
│   │   └── runtime/         # Reporting Python Tools/Invokers/Render/Delivery/Binding
│   └── parameter_adjustment/
├── application/             # 通用 Capability/Workflow/Run 生命周期与投影
├── webapi/                  # 通用 FastAPI 接口
└── frontend/                # Schema/State/Event/Output 驱动的通用 React
```

强制依赖方向：

```text
kernel ← runtime ← capability runtime ← application/webapi
                                  ↑
                         capability definitions
```

其中：

- `kernel` 不导入 `runtime`、`capabilities`、`core.reporting`、FastAPI 或 React；
- `runtime` 不认识 Capability ID、Editor、Auditor、Cross、Chief、模块 `2.1`～`2.5` 或报告字段；
- Capability 只通过 Kernel/Runtime 的公开定义、端口和执行协议接入；
- Capability 之间默认不得互相导入；真正被两个及以上 Capability 共享且业务中立的机制才可上移；
- `application`、`webapi` 和 `frontend` 不以 Capability ID 选择流程或渲染；
- Python Tool 实现一个确定性业务操作，不得以 `run_entire_*` 形式把整段编排重新藏回 Python。

## 4. “无状态 Kernel”的准确含义

无状态不表示 Run 没有状态，而表示 Kernel 本身不保存可变业务状态。Kernel 接收冻结的 Plan、当前 State 和 Event，纯粹地产生新 State 与 Effects：

```text
ResolvedPlan + WorkflowState + WorkflowEvent
                    │
                    ▼
             New State + Effects
```

权威状态由 Runtime Store 持久化；完整轨迹进入 Event Log；Conversation、Tool Result、Artifact 和 Recovery 分别通过通用端口管理。Kernel 不调用 Provider、文件系统、数据库、HTTP 或业务 Runner。

## 5. 文件定义与 Python 的边界

文件定义负责“做什么、怎样组合”：

- Capability、Agent、Task、Tool、Contract、Workflow、Recovery；
- Agent 身份、Prompt、可见 Tools、输入输出 Schema；
- 顺序、条件、循环、并行、Join、Subworkflow、Conversation Key；
- RequestInput、输出发布和恢复策略引用。

Python 负责“一个步骤具体怎么做”：

- Provider/Agent 会话调用；
- 确定性 Tool 与外部系统接入；
- 领域模型转换和校验；
- Capability 特有的结果接受、渲染和交付；
- 通用 Runtime 无法也不应理解的领域操作。

若修改业务流程必须改 Python `if/for/await` 才能改变动作顺序，说明编排仍未完成声明化。若 YAML 只是调用一个执行完整领域流程的粗粒度 Python Tool，也不算完成。

## 6. Kernel Action 不得业务化

允许的 Action 是通用执行动作，例如：

```text
SetVariable / If / ConditionGroup / Goto / Parallel / Join / Subworkflow
CreateConversation / InvokeAgent / InvokeTool / ValidateContract
RequestInput / PublishResult / EndWorkflow
```

禁止新增 `CrossReviewAction`、`ModuleAuditAction`、`ChiefRevisionAction` 等领域 Action。Editor、Auditor、Cross、Chief 只是 Capability 文件定义中的 Agent/Task 身份。

## 7. Recovery 不得退化

最终通用 Runtime 与 Capability Runtime 的组合必须保留：

- 结构化提交纠正；
- Schema 错误后的原 Conversation 修正；
- Max Token Continuation；
- Tool Slice Continuation；
- No-progress；
- 已完成 Tool Result 复用；
- 原 Conversation / Session 复用；
- Same-run 已完成结果恢复。

这些语义不得留在 Legacy Runner 才成立，也不得被塞进 Kernel。通用 Runtime 负责通用事件、状态和执行机制；Capability 提供领域 Prompt、合同和结果解释。

## 8. 旧兼容目标已经废止

Legacy Reporting Runner、旧 Reporting API、旧 Run 恢复和双路径 A/B 不再是最终产品要求，也不再是完成条件。实施不得为了保持 Legacy 默认而继续让新路径继承、包装或反向调用旧的整流程 Runner。

允许在拆分过程中短暂保留尚未迁出的实现，但必须满足：

- 被明确标注为待迁移或待删除债务；
- 不作为声明式生产路径的父类、Host 或隐藏编排器；
- 不新增兼容 Adapter 或 Feature Flag 延长双路径；
- 每个切片用 Characterization 证明所需业务语义后，迁移到通用 Runtime 或 Capability Runtime；
- 删除旧入口前先证明新的最终路径覆盖受影响功能，不以旧路径兼容为验收标准。

历史 Run 和旧路径可以保留为诊断证据，但不得支配新公共接口和目录边界。任何批量删除真实运行数据仍属于破坏性操作，必须另行处理。

## 9. 连续自主实施

本项目采用一次总任务、按最终架构依赖顺序持续推进、最后一次真实测试。活动阶段和切片见主实施方案。

每个可验证垂直切片必须：

1. 检查生产入口、调用链、定义和测试；
2. 把目标设计与实际代码逐项对照，记录差异；
3. Characterization First；
4. 实现最小完整纵切；
5. 运行 focused tests 和受影响测试；
6. 更新唯一状态文件；
7. 创建并推送聚焦提交；
8. 自动进入下一切片。

不得在切片结束后询问是否继续，不设置中途人工验收断点。只有整个最终架构、自动验证和文档一致后，才进行一次真实 Provider/项目/浏览器测试。

## 10. 验证政策

默认只运行：

```bash
uv run ruff check <changed-python-paths> <changed-test-paths>
uv run pytest -q <focused-and-affected-tests> --maxfail=3
```

涉及前端时读取 `frontend/package.json` 后运行真实存在的 focused test、定向 lint、TypeScript 检查或受影响 build。

除非用户明确要求，不运行全量测试回归。窄测试只能证明其覆盖的边界，不能用于宣称整个最终架构完成。

## 11. Characterization First

修改既有执行语义前，先用测试或语义 Trace 固定实际行为，尤其是：

- Agent/Provider/Tool 循环；
- Conversation/Session 身份；
- Recovery 与 Same-run 恢复；
- Parallel/Join/Subworkflow/WAITING；
- Reporting 的结构化结果、审查、渲染和交付。

Characterization 不是永久保留旧类结构。它用于把应保留的业务语义从旧实现中分离出来，随后测试应改为通过最终公共边界验证。

## 12. 新门禁、Hash、CAS 与依赖

默认禁止新增不必要的安全门禁、判断门禁、Hash、CAS、锁或额外校验链。若确有必要，必须先向用户说明：

- 具体问题和可复现证据；
- 现有 State、Contract、Event 或 Recovery 为何不足；
- 影响范围、替代方案和回退方式。

得到明确同意前不得实现。

本轮不新增生产编排框架。Microsoft Agent Framework、LangGraph、Burr、Temporal 和 Restate 仅作为官方参考；现有轻量 Compiler/Runtime 是生产方向。普通 Python/Schema 依赖也必须由真实缺口驱动，不得以“架构完整”为由堆叠框架。

## 13. 实时差异监控

每次开始和结束一个阶段都必须重新检查：

- 生产调用链是否仍经过旧 Runner/Facade；
- Kernel/Runtime 是否出现领域导入或业务词汇；
- Capability 文件是否仍调用整流程粗粒度 Tool；
- 定义引用是否真正由 Compiler 冻结和校验；
- Agent/Tool/Conversation/Recovery 是否真正走通用端口；
- Application/FastAPI/React 是否存在 Capability-ID 分支；
- 文档状态是否与当前分支、提交和测试证据一致。

发现设计与实际不一致时，立即把差异写入 `RUNTIME_EXTRACTION_STATUS.md`，补充 Characterization，调整后续切片；不得用已存在的类、通过的窄测试或历史真实 Run 掩盖生产未接线。

## 14. 调研规则

技术不确定时依次查阅：

1. 当前仓库源码、定义、测试和运行产物；
2. 官方 GitHub 源码和测试；
3. 官方文档与设计说明；
4. 隔离的最小 POC。

只使用官方一手技术来源形成架构结论。若结论不改变实现选择，不必增加研究文件；若改变长期边界，记录到 `docs/research/decisions/`。调研不能替代生产接线和验证。

## 15. 分支、提交和状态

长期实施分支：

```text
base: agent/declarative-runtime-plan
head: agent/declarative-runtime-implementation
Draft PR: one long-lived PR
```

每个架构切片独立提交和推送。`docs/implementation/RUNTIME_EXTRACTION_STATUS.md` 是唯一跨会话实施状态；它不是运行时状态，也不应复制 Event Log。

## 16. 完成定义

只有以下事实全部由当前代码和验证证明，项目才完成：

1. 生产路径只有一个业务无关 Stateless Kernel 和一个通用 Compiler/Runtime；
2. Agent、Tool、Contract、Task、Workflow、Recovery 从文件定义加载并编译；
3. 每个 Capability 拥有自己的领域 Python Runtime/Tools，而不拥有独立 Kernel；
4. 声明式 Reporting 不继承、包装或反向调用 Legacy 整流程 Runner；
5. 通用 Application/FastAPI 通过 Registry/Binding 启动任意 Capability；
6. React 只依据 Schema、Run State、Interactions、Events 和 Outputs 渲染；
7. Reporting 与中立 Capability 都通过同一生产链；
8. Recovery、Conversation、Tool Result 和 Same-run 行为在最终路径成立；
9. 未使用的新旧兼容 Runner、Facade、入口和测试已删除或明确隔离，不在生产图；
10. focused/affected 自动验证、包构建和最后一次真实 Provider/项目/浏览器测试通过；
11. 文档、状态、生产调用链和发布产物一致。

在这些条件满足前，状态必须保持“实施中”，不得因旧兼容路径可运行或历史真实 Run 成功而宣称完成。

## 17. 接续方式

新会话必须先执行并报告：

```bash
git status -sb
git branch --show-current
git log -1 --oneline
```

然后读取本文件和唯一状态文件，从 `Current slice` / `Next automatic action` 继续；不得重复已完成切片，不得退回“双路径兼容即完成”的旧目标。
