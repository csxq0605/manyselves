# PR #6 报告性能与质量改进指导

适用 PR：[PR #6](https://github.com/csxq0605/manyselves/pull/6)，实施分支 `agent/declarative-runtime-implementation`。

核查基线：`927aef5`，2026-10-15（Asia/Taipei）。依据：[原始会话归档](../../run.md)、[既有质量与编排研究](../research/decisions/REPORTING_COST_QUALITY_ORCHESTRATION.md)、当前源码及归档实验。下表的“当前证据和状态”是这一基线的核查快照，不代表后续实时进度。

后续取证：[全清单首轮证据与实施依据](../research/decisions/REPORTING_PR6_EVIDENCE_AUDIT.md)已对下表基线补充核查，包括S04摘要范围与实验评分版本的更正；实现进度仍见唯一状态文件。

本文固定本 PR 的完整工作范围、条目编号、实施顺序和验收口径。动态进度、当前切片、验证结果与下一步统一记录在 [RUNTIME_EXTRACTION_STATUS.md](RUNTIME_EXTRACTION_STATUS.md)，引用本清单编号；不另建一份会逐渐分叉的状态账本。架构边界继续遵循 [AGENTS.md](../../AGENTS.md) 和[最终架构方案](../architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)。

本指导的提交只修改文档，不表示以下改动已获验证或已实施。

目标是基于已有运行证据改善整份报告的速度、成本与正确性，覆盖“速度和成本”“质量”“流程与整体评估”三大项。“先审查负担”指定实施顺序，不缩小总范围。一个子项完成后，必须回到总清单更新未完成范围，不能把局部测试通过当成整个大项完成。

## 1. 速度与成本：审查负担只是其中一部分

| 编号 | 原问题 | 当前证据和状态 | 剩余工作与判断边界 |
| --- | --- | --- | --- |
| S01 | 审查诱发返工 | 927aef5 仅完成模块 advisory 留档、非自动返工；blocking 仍修订复审。之前联合 Cross 已采用相同语义。 | 已完成一个子项；不能等同于审查负担优化完成。历史 Cross 链174请求不是全部可删除成本。 |
| S02 | Cross 审查兼写综合分析 | models/agentic.py 的 CrossSynthesisInput 仍有15个必填字段，列表允许为空；chief_runtime.py:168 与 final_review_tools.py:231 实际消费它。 | 精简重复表达，保留关系、来源、条件和边界；明确 Chief 最终展开职责。不能直接删字段，也不必新增模型角色。 |
| S03 | 审查先解释再结构化提交 | 之前13次合成请求有正文与工具提交两份可见输出；不是逐字重复或当前完整报告占比。 | 核查各角色实际提交轨迹和可以省略的复述，不把内部推理当冗余删除。 |
| S04 | 摘要频繁 | config/schema.py 默认 working_memory_tokens=36000；agent_loop.py 依据完整工作集触发压缩。历史120次摘要已有计数。 | 对原请求重建比较历史尾部、当前事实/草稿、摘要各自大小；区分原样历史、压缩与发送前去重的顺序。不能直接认定120次摘要大部分可取消，或把阈值改成10万就有收益。 |
| S05 | 缓存布局 | agent_loop.py:4469 将实时 task state 拼进 system；anthropic_provider.py:225 对整个 system 文本块设置缓存标记。 | 稳定身份/任务合同/冻结事实与动态草稿分离，核查真实 Provider 序列化前缀；去掉重复写入历史不等于已经修复缓存。实际命中率尚未测量。 |
| S06 | 作者重复输出与工具往返 | ModuleSubmission 仍包含正文与 claims；默认每轮工具数4；2.4 author max_tokens=12288。module_provider.py:483 按 Task 的 tools 构造可用工具。 | 核查 Claims 哪些可由既有绑定投影、哪些承载语义；核查真实 Task 工具集及单轮写入协议。不能仅数 Agent 声明就断言每次发送全部16个工具；拆2.4需评估共享结论与合并成本。 |

## 2. 质量：完整输入、正确判断与最终正文是三道不同问题

| 编号 | 原问题 | 当前证据和状态 | 剩余工作与判断边界 |
| --- | --- | --- | --- |
| Q01 | 原表漏读/召回遗漏 | 已有完整模块事实包和未引用证据输入修复。run.md 留有4处SPD仅处理2处、13测点只列11处及14条剩余电流的历史证据。 | 用已有冻结事实及已有成稿对照对象覆盖，分别定位解析遗漏、路由遗漏、正文遗漏；完整送达不等于完整使用。 |
| Q02 | Excel尺寸声明 | intake/adapters.py:83–96、mappers/s4_4.py:692–701 仍以普通模式和只读模式逐行 strict zip。既有审查报告称已复现缺 dimension；本清单仅重新核实了相同代码路径，实施前须保留可复跑的最小复现。 | 这个已报告的读取缺陷没有包含在927aef5中，应保持待修，不能因局部审计测试通过而被忽略。 |
| Q03 | 物理量混淆/大小比较错误 | S4-4 已区分 measured_temperature、ambient_temperature、temperature_rise，Decimal计算差值，缺环境温度不计算。 | 尚不能证明正文忠实采用这些字段；阈值、单位、对象、时点和适用条件须共同绑定。程序比较大小之前必须知道选对规则。 |
| Q04 | 两章结论相反 | 联合Cross检查共同快照；相同事实可路由到两模块。 | 尚无经生产验证的跨章共享专业判断机制。应回放11.3A与2.3%/3%旧样本，区分不同工况的合理差异和同对象同条件矛盾。 |
| Q05 | 异常覆盖检查 | review_preflight.py 的新增确定性检查是控制标记、重复来源ID与来源注册；这些不是“原始异常→最终正文”的全覆盖核对。 | 补充对象级预期集合与成稿引用/行动映射的设计和离线验证；不能在仅现有结构预检通过时直接跳过语义审查。 |
| Q06 | 标准版本和适用性 | 来源账本已有引用与内容追溯；状态文档明确标准现行性、适用条件和引用蕴含未验收。 | 版本状态需要可信来源及核验日期；“ID存在”不代表版本现行，也不代表原文支持结论。 |
| Q07 | Skill与模型 | Skill已限制为方法资料，不应替代原始证据；Agent多为 model: inherit。 | 规则本身和专业判断质量仍需核对；角色选模需要核实实际配置与调用记录，不能把某次Haiku实验当作所有生产角色现状。 |

## 3. 流程与整体评估：需要用前两块的结果决定职责

| 编号 | 原问题 | 当前证据和状态 | 剩余工作与判断边界 |
| --- | --- | --- | --- |
| F01 | 阶段多、反复重推结论 | 文件工作流仍是模块写审之后 Cross → Chief → Final → Delivery，Cross修订还经过局部审计。 | 映射每阶段实际新增信息、所检缺陷及修改影响，判断哪些重复推导可以去掉；不能因角色名称相近就合并。 |
| F02 | “先判断再写”是否更好 | 已有合成实验；状态文档记录其会把缺环境量错解释为缺规则。 | 先修清判断输入/规则适用性边界，用既有样本核查，不直接把原型升级生产。 |
| F03 | 缺少整体比较 | 历史完整Run的525请求及分角色数据可以用；目前PR未合并，历史模型/编排不等同当前代码。 | 先做已有运行的阶段成本、已知缺陷、当前修复映射；缺失的逐finding数据标明不可恢复。新端到端运行仅用于最后不能离线回答的真实收益，不要求用户先重跑才开始分析。 |
| F04 | 完成标准混淆 | 99项离线测试证明927aef5局部语义与恢复，不证明整份报告更快更正确。 | 分开记录代码行为已验证、历史缺陷是否复现/已修、实际耗时/token/质量是否测量。不得用一个子项通过替代整项完成。 |

## 对原分析需要修正的结论

- 历史输出量支持优先研究审查成本，但不能单凭平均耗时和输出量断言输入是次要成本。缓存、重复输入、模型差异和并行关键路径都需要区分。
- 不应继续引用“所有24次均温升混淆”概括全部实验。已有后续typed批次及不同错误分布；应按原始批次核对分母，不能忽略已经存在的反例。
- 默认取消摘要、直接提高工具上限、拆分2.4、自动从自由正文生成全部Claims、仅机器报警才语义审查，均仍是待验证设计选项，不是已证实收益。

## 实施顺序与交付物

1. **先补齐证据与依赖。** 复用已有 Run、冻结事实、成稿、请求记录与合成实验，逐项形成“已知问题 → 当前路径 → 已修/仍存/证据不足”的对应关系（F03）。先完成审查负担剩余的 S02、S03 核查；同一轮总审查必须覆盖 S04–S06 和 Q01–Q07，不能又停在一个审查子项。缺失的逐 finding、模型输出或费用数据明确列出，不要求重新生成完整报告才开始分析。
2. **修复已证实的读取缺陷，完成确定性质量边界设计。** Q02 先复现后修复；Q01、Q03–Q06 明确对象身份、物理量、单位、适用条件、来源状态和异常覆盖的公共输入与既有消费路径。缺测量值、缺规则、规则不适用分别表达，不以错误假设建立共享判断。
3. **按完整链路实施性能改动。** 根据第一步证据，处理 S02–S06 的输出职责、结构化提交、摘要/缓存布局和作者调用组织。涉及 Cross 综合材料时同步处理 Chief 与 Final；涉及上下文时同步验证工具协议、事实保留及恢复。每个切片可以小，但必须记录在总清单的位置与仍未解决的项。
4. **再决定角色和阶段组织。** 用 S/Q 的结果判断 F01、F02 中哪些重复推导可以消除、哪些审查仍有独立责任；Q07 的角色选模基于真实配置和缺陷表现。不能直接把“先判断再写”或固定三个阶段当成结论。
5. **最后补足只能真实运行回答的问题。** F03、F04 先完成离线可验证部分；只有当前改动的实际墙钟时间、token、缓存和成稿语义质量等剩余问题需要新的代表性运行。明确输入、模型、版本、范围和比较口径；不把每个小切片都变成一次完整报告重跑。

## 验收与进度记录

| 范围 | 必须交付的证据 | 不能替代验收的结果 |
| --- | --- | --- |
| S01–S03 审查负担 | 原始 findings/verdicts 留存、阻断问题闭环、Cross→Chief→Final 所需材料完整；列出移除的实际调用/重复输出与保留的质量责任 | 少几个 JSON 字段、仅统计审查总 token、纯建议用例通过 |
| S04–S06 上下文及作者开销 | 真实请求构造与 Provider 序列化的离线对照，标明稳定/变动前缀；所需事实、来源绑定、工具配对和恢复保持正确；调用次数/字符与真实 token/缓存指标分开 | 仅调高阈值、只数 Agent 声明工具、把字符减少当费用收益 |
| Q01–Q07 正确性 | 已有异常对象逐项追溯到输入、判断、正文和行动；数值/单位/工况/规则边界反例；标准状态的来源与核验依据；覆盖同条件矛盾和不同条件合理差异 | 解析成功、来源 ID 存在、Schema 合格、两个角色给出同一结论 |
| F01–F04 全流程 | 阶段责任及依赖图、旧缺陷到新行为的对照、明确不可离线确认的项；有新运行时记录可比墙钟、输入/输出/摘要/缓存与质量结果 | 并行活跃时间求和、历史模型与当前模型直接比较、局部测试通过即宣布全目标完成 |

每个实施记录在唯一状态文件中包含：清单编号、具体问题、修改范围、验证证据、未验证内容、提交，以及下一项。大项只有其各条目都有明确处置与证据后才能关闭；不采纳的设计选项也需要说明证据和替代处理，不能直接从清单消失。

既有 `927aef5` 只证明 S01 中“模块建议不强制返工”这一子项，99 个离线测试不代表 S、Q、F 全部完成。本文不预先承诺固定提速比例，不新增审批门禁、运行时框架或默认全量回归要求。

## 当前核查的主要源码入口

- 审查与综合材料：[结果模型](../../manyselves/capabilities/distribution_reporting/runtime/models/agentic.py)、[联合 Cross](../../manyselves/capabilities/distribution_reporting/runtime/cross_joint.py)、[Chief](../../manyselves/capabilities/distribution_reporting/runtime/chief_runtime.py)、[Final](../../manyselves/capabilities/distribution_reporting/runtime/final_review_tools.py)。
- 请求和工具组织：[配置默认值](../../manyselves/config/schema.py)、[Agent loop](../../manyselves/runtime/loops/agent_loop.py)、[Anthropic 序列化](../../manyselves/runtime/providers/anthropic_provider.py)、[模块 Provider](../../manyselves/capabilities/distribution_reporting/runtime/module_provider.py)。
- 事实与质量：[通用表格读取](../../manyselves/capabilities/distribution_reporting/runtime/intake/adapters.py)、[S4-4 映射](../../manyselves/capabilities/distribution_reporting/runtime/mappers/s4_4.py)、[事实包](../../manyselves/capabilities/distribution_reporting/runtime/research/fact_context.py)、[机器预检](../../manyselves/capabilities/distribution_reporting/runtime/review_preflight.py)、[来源账本](../../manyselves/capabilities/distribution_reporting/runtime/source_ledger.py)。
- 阶段编排：[报告后半段工作流](../../manyselves/capabilities/distribution_reporting/workflows/distribution-reporting-tail.yaml)。
