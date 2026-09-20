# Codex 最终架构自主连续实施协议

> 目标：在长期实施分支中持续完成最终架构收敛，中途不设置人工验收断点，全部自动证据成立后只进行一次真实测试
>
> 主方案：[`architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md`](architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)

## 1. 执行模式

本项目采用一次总任务、`FA-00`～`FA-07` 连续实施、最终一次真实测试。

Codex 必须：

1. 读取规范和唯一状态文件；
2. 检查当前分支、提交、工作区和生产调用链；
3. 从状态中的 `Current slice` 继续；
4. Characterization First；
5. 完成一个可验证纵切；
6. 运行 focused/affected tests；
7. 更新状态、聚焦提交并推送；
8. 自动开始下一切片；
9. 实时比较设计与实际并补救；
10. 所有自动完成证据成立后才请求一次真实测试。

工作包是依赖、测试、提交和回滚边界，不是用户重新派发任务的边界。

## 2. 分支和 PR

```text
base: agent/declarative-runtime-plan
head: agent/declarative-runtime-implementation
PR: one long-lived PR; keep it Draft during implementation and mark it ready only after the merge-scope checks pass
```

每个切片可以有一个或多个聚焦提交。不得为了永久保留双路径而使用 Feature Flag；代码回退依靠提交。不得未经明确要求合并 PR。

## 3. 唯一实施状态

只维护：

```text
docs/implementation/RUNTIME_EXTRACTION_STATUS.md
```

它记录：

- 当前分支和 HEAD；
- Current FA work package / slice；
- 已完成切片与提交；
- 实际运行的测试；
- 设计—实现差异；
- 研究决定；
- 阻塞；
- 下一自动动作；
- 最终真实测试状态。

它不是 Runtime Manifest，不复制 Event Log 或每次 Action。

## 4. 自主研究

不确定时依次检查：

1. 当前源码、定义、测试和 Run 产物；
2. 官方 GitHub 源码和测试；
3. 官方文档和设计说明；
4. 隔离最小 POC。

只采用官方一手技术资料。若结论不改变实现选择，更新当前差异即可；改变长期边界时记录 `docs/research/decisions/`。

本轮不新增 Microsoft Agent Framework、LangGraph、Burr、Temporal、Restate 或其他生产编排依赖。官方研究用于校准 Loader/Compiler/Executor/State/Persistence 分层。

## 5. FA 连续顺序

```text
FA-00  事实基线、规范和完成矩阵
FA-01  生产架构边界 Characterization
FA-02  通用 Agent/Conversation/Recovery Runtime
FA-03  Distribution Reporting Domain Runtime 与 Legacy 解耦
FA-04  通用 Capability Runtime Binding
FA-05  物理归属收敛与旧流程删除
FA-06  通用 FastAPI/React 产品表面
FA-07  完成审计、构建和最终真实测试
```

原 `WP-00`～`WP-12` 是基础抽取历史，不再是当前完成状态。旧流程兼容、Legacy 默认和双路径 A/B 不再是 FA 工作包的验收条件。

## 6. 每个切片执行循环

### 6.1 勘察

先确认：

- 真实生产入口和构造链；
- MRO、import、依赖注入和 Capability Binding；
- 文件定义到 Compiler/Runtime 的真实连接；
- State、Conversation、Recovery、Event、Output 所有者；
- 受影响测试和持久化行为。

### 6.2 Characterization First

先让测试准确暴露当前缺口，再修改实现。测试应固定要保留的语义，而不是永久固定旧父类、旧 Facade 或旧导入路径。

### 6.3 实现

优先纵向贯通：

```text
Definition → Compiler → Runtime Port → Capability implementation
           → State/Event → Application projection → Test
```

不得只增加抽象类，也不得用新 Adapter 包住旧整流程后声称解耦。

### 6.4 验证

默认：

```bash
uv run ruff check <changed-python-and-test-paths>
uv run pytest -q <focused-and-affected-tests> --maxfail=3
```

前端读取 `frontend/package.json`，运行 focused Vitest、定向 ESLint、TypeScript 或受影响 build。

除非用户明确要求，不运行全量测试回归。必须准确记录测试范围，不能由 focused tests 推导全仓库通过。

### 6.5 状态和提交

测试通过后：

- 更新差异矩阵和下一动作；
- `git diff --check`；
- 创建并推送聚焦提交；
- 更新长期 PR；
- 自动继续。

## 7. 实时差异监控

每个工作包开始和结束都核对：

| 目标 | 必查证据 |
| --- | --- |
| Stateless Kernel | transition 调用链、领域 import/词汇扫描 |
| Generic Compiler/Runtime | Definition→Plan→Host 生产接线 |
| Capability-owned Python | 领域代码目录、Binding 和调用者 |
| 无 Legacy 宿主 | Runner MRO、构造和 service selection |
| Generic Application | route/facade 依赖和 Capability registry |
| Generic React | Capability-ID 分支、Schema/WAITING/Output 投影 |
| Recovery | Conversation/session/result reuse focused behavior |

若实际偏离设计：立即写入状态，新增 Characterization，调整下一切片并修复。不得以旧真实 Run 或类已经存在掩盖未接线。

## 8. 默认禁止的新机制

默认禁止新增不必要的 Gate、判断门禁、Hash、CAS、锁和额外校验链。确实需要时，必须先向用户说明问题证据、现有机制不足、替代方案、影响和回退，获得明确同意后才能实现。

不得为了删除 Legacy 而删除结构化纠正、Schema 原会话修正、Max Token、Tool Slice、No-progress、Tool Result 复用、Conversation/Session 复用或 Same-run 恢复。

## 9. 停止条件

中途不设人工验收门。只有以下真实情况才停止：

- focused/affected 自动测试失败且在当前边界内无法修复；
- 必须破坏真实项目、Run 或 Artifact 才能继续；
- 必须新增生产编排依赖或决定尚未定义的长期公共接口；
- 无法证明应保留的 Recovery/Conversation/Output 语义；
- 官方来源与最小 POC 仍无法消解关键架构矛盾。

普通调研、文件移动、测试改写和内部接口选择不构成停止理由。

## 10. 最终真实测试

只有 `FA-00`～`FA-07` 自动部分全部通过后才执行一次真实测试。交接必须只要求用户操作网页版 Demo，并包含：

- 分支、提交、PR 状态；
- 启动和登录；
- Provider/Model 设置；
- 中立 Capability 的输入、运行和输出；
- Distribution Reporting 的真实项目和 Schema 表单；
- 同一 Run 的 WAITING 恢复（若触发）；
- 状态、Events、Outputs、Artifacts、Cost 的预期；
- 通过/失败标准；
- 失败时保留的 Run ID 和现场；
- 回滚步骤。

真实 Reporting 可能运行约半小时，不做持续长时间轮询。失败后不自动刷新重试或创建新 Run。

## 11. 完成审计

最终宣布完成前，逐项检查主方案第 15 节。每项必须有当前生产代码、测试、构建或真实运行证据；缺失或间接证据按未完成处理。

历史 `report-declarative-*` 成功 Run 只证明当时的业务路径，不证明移除 Legacy 宿主后的最终架构。

## 12. 接续

新会话先运行：

```bash
git status -sb
git branch --show-current
git log -1 --oneline
```

随后完整读取 `AGENTS.md` 和本协议，从状态文件的 Current slice/Next automatic action 继续。不得重新把 Legacy 兼容设为目标，也不得在工作包间询问是否继续。
