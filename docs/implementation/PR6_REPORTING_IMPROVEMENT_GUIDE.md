# PR #6 报告性能与质量改进指导

适用 PR：[PR #6](https://github.com/csxq0605/manyselves/pull/6)，实施分支 `agent/declarative-runtime-implementation`。

核查基线：`927aef5`，2026-10-15（Asia/Taipei）。依据：[原始会话归档](../../run.md)、[既有质量与编排研究](../research/decisions/REPORTING_COST_QUALITY_ORCHESTRATION.md)、当前源码及归档实验。下表的“当前证据和状态”是这一基线的核查快照，不代表后续实时进度。

后续取证：[全清单首轮证据与实施依据](../research/decisions/REPORTING_PR6_EVIDENCE_AUDIT.md)已对下表基线补充核查，包括S04摘要范围与实验评分版本的更正；实现进度仍见唯一状态文件。[后续生产链核查](../research/decisions/REPORTING_PR6_RULES_AND_BINDINGS.md)进一步更正S06：内部ModuleSubmission包含Claims，不表示当前模型重复提交Claims；当前作者已使用紧凑提交，运行时生成整段来源绑定。

本文固定本 PR 的完整工作范围、条目编号和验收口径；实施顺序按文内根因复核修订，当前以2026-10-19复核为准。动态进度、当前切片、验证结果与下一步统一记录在 [RUNTIME_EXTRACTION_STATUS.md](RUNTIME_EXTRACTION_STATUS.md)，引用本清单编号；不另建一份会逐渐分叉的状态账本。架构边界继续遵循 [AGENTS.md](../../AGENTS.md) 和[最终架构方案](../architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)。

初版指导提交仅修改文档；下方927aef5基线表保持历史快照。后续实现及本次计划复核不能据此被视为整体已验收。

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

## 2026-10-19 根因与分层复核（当前执行依据）

复核基线：`6e25dda`。用户要求重新检查是否将通用执行层缺陷当成报告任务问题；本次保留S01–S06、Q01–Q07、F01–F04全部目标，修订归因、顺序与证据要求，不扩为无边界的平台重写。代码位于Capability目录只能说明当前所有者，不能证明根因天然属于该领域；同样，逻辑看起来通用也不足以证明必须迁入Kernel。

### 复核结论与源码依据

| 证据 | 核查结果 | 能证明什么／尚不能证明什么 |
| --- | --- | --- |
| E1 共用工作流骨架 | [应用入口](../../manyselves/application/workflow_projection.py)、[Binding目录](../../manyselves/runtime/capability_binding.py)、[Compiler](../../manyselves/kernel/workflow/compiler.py)、[InvokeAgentExecutor](../../manyselves/kernel/executors/base.py)、[Host](../../manyselves/runtime/workflow_host.py)按定义和端口连接；Host调用Kernel.transition后委派Executor，Agent端口允许Capability提供Invoker。 | 已定位共同执行骨架；本次未发现能解释报告缺陷的Kernel状态转换反例。源码连接与历史验收不足以排除Compiler、并行/Join或恢复缺陷。出现版本/结果/路由错配时应回到这些层定位。 |
| E2 中立能力的验证盲区 | [parameter-adjustment工作流](../../manyselves/capabilities/parameter_adjustment/workflows/parameter-adjustment.yaml)确有invoke_agent，但其[适配器](../../manyselves/capabilities/parameter_adjustment/adapters/runtime.py)中_ParameterAdjuster.invoke直接返回value=10，build_runtime_binding丢弃services。 | 证明共用Compiler/Host/Agent端口和确定性工具，不经过Provider、AgentLoop、模型工具循环或上下文重建。不能再以该能力运行成功证明“另一领域也验证了通用LLM执行”。这不是要求把确定性参数功能强改成模型调用。 |
| E3 工具策略分歧 | [Module组合](../../manyselves/capabilities/distribution_reporting/runtime/module_provider.py)先编译工件能力再保留通用读取器；[Chief](../../manyselves/capabilities/distribution_reporting/runtime/chief_provider.py)、[Cross](../../manyselves/capabilities/distribution_reporting/runtime/cross_provider.py)、[Final](../../manyselves/capabilities/distribution_reporting/runtime/final_provider.py)、[Aggregate](../../manyselves/capabilities/distribution_reporting/runtime/aggregate_provider.py)仍直接使用[compile_agent_access](../../manyselves/capabilities/distribution_reporting/runtime/artifact_access.py)过滤。 | 相同中立envelope（一个显式可读reference，allowed_tools仅submit_result）直接回放：五条组合均有readable ref，只有Module暴露open_artifact。证明策略不统一，不证明另外四个角色在其实际Task/envelope下必然缺工具。必须先明确Task/Agent/显式工件能力的权威关系；最近局部修复不能算通用协议已收束。 |
| E4 通用预算边界 | [AgentLoop](../../manyselves/runtime/loops/agent_loop.py)的_estimate_tokens用固定0.3字符系数；_chat_with_retries拿到tool_definitions后，_trim_typed_context_to_budget仍只传messages、context_window与max_output，未纳入工具Schema。 | 已确认预算核对不覆盖完整Provider请求。中文/结构化内容与实际模型token差异、Schema遗漏是否导致过历史超窗或语义遗漏尚未测量；不能直接归因所有历史摘要/遗漏。应先修整请求预算口径，不能先通过追加领域字段扩大请求。 |
| E5 冻结合同与模型Schema间仍有边界待核 | [reporting_tool_input](../../manyselves/capabilities/distribution_reporting/contracts/reporting_tool_input.yaml)是空Schema；Compiler会冻结定义依赖，但模型所见工具参数来自[ToolRegistry反射](../../manyselves/runtime/tools/registry.py)及Module等组合中的_schema_cache替换。 | “定义被编译/冻结”不等于“实际发送的工具参数Schema已被同样验证”。需追到实际Schema、调用参数、执行校验和结果，不预先断言Compiler已经丢字段；也不一律禁止Capability按任务投影Schema。 |
| E6 会话与上下文路径 | [AgentExecutionService](../../manyselves/runtime/agent_execution.py)管理稳定会话，[ProviderAgentSessionFactory](../../manyselves/runtime/provider_agent_session.py)负责构造/重配；[TaskContextRebuilder](../../manyselves/runtime/loops/context_rebase.py)是可选hook，多个Reporting bridge分别绑定。AgentLoop还识别write_result_part(s)及历史draft标记。 | 需要核对所有入口是否在会话复用、摘要、continuation和恢复后采用相同的当前输入与工具定义。不能把某个bridge接线成功推为通用服务默认成立；硬编码工具协议的所有权也须核实。现有Hook已能表达的机制不另起框架。 |
| E7 新领域协议的证明边界 | [判断投影](../../manyselves/capabilities/distribution_reporting/runtime/assessment_context.py)核对显式记录集合和字符串锚点；assessments、observation_links允许缺省，规则verified仍为作者声明；来源同坐标仅作为同源比较依据。 | 可以暴露未记录/未定位信息，不证明选对规则、同条件一致或行动正确；可选记录也不能保证作者实际逐项判断。新增字段是设计选择，尚未证明最小必要、优于既有合同或净成本有收益，需在E3–E6核清后决定保留、简化或调整。不能把它当统一成功机制。 |
| E8 检索和输入仍有独立领域问题 | Excel固定电压补算/缓存等已定位在Reporting映射；[知识上下文](../../manyselves/capabilities/distribution_reporting/runtime/research/knowledge_context.py)的候选选择及[词片段匹配](../../manyselves/capabilities/distribution_reporting/runtime/research/reference_matching.py)当前只有Reporting消费者。 | 已有明确数据/规则缺陷应保留修复；通用词法不等于已服务多个Capability。候选变多不证明语义召回正确；无第二个真实消费者证据时不为“看起来通用”迁移目录。 |

E3回放未调用模型、未增加测试文件，仅比较现有生产组合函数在同一输入下的输出；不是端到端故障复现。E4是当前源码确认的预算边界缺失，不是已测得的Provider拒绝或丢事实。E1–E8共同替代“报告报错，所以先补报告字段”的默认归因。

### 17项重新归因与处置顺序

| 编号 | 必查的通用机制／组合边界 | 领域侧职责与本次决策 |
| --- | --- | --- |
| S01 | 结果状态、已完成结果复用、恢复是否重复派发/重试；之后核对finding驱动路由。 | advisory/blocking的业务含义留在Reporting；现有非自动返工修复保留。不能把重复调用全归为审查意见。 |
| S02 | Join后快照、交接输入与版本是否正确/重复，Cross输出是否被下游完整使用。 | Cross关系分析与Chief叙事的分工留在定义；已精简字段保留待评估，先排除交接故障再删阶段/字段。 |
| S03 | 结构化工具Schema是否准确、提交能否触发正确终止、流式工具结果是否被当作普通回答而续写。 | 直接结构化提交提示可保留，但不能据此认定双份输出只由提示造成。 |
| S04 | 当前状态/历史分离、投影与摘要顺序、预算E4、工具配对、会话重绑和恢复E6。 | Reporting提供准确事实/草稿投影，通用Runtime拥有压缩与原子协议。不能只改领域包大小或提高阈值。 |
| S05 | system/稳定输入/动态状态到原生与兼容Provider的实际序列化、模型窗口及缓存参数。 | 已有AgentLoop布局修复属通用层；仍须验证其它入口和净收益，不迁入领域重复实现。 |
| S06 | E3工具暴露、E5模型Schema、结果提交/成功回执、单轮调度与续写。 | 正文分段和引用账本留在Reporting；在工具协议一致前不继续加字段、拆2.4或提高并发。 |
| Q01 | 从规范化快照到实际Provider消息逐边界确认是否丢失/换版/不可读；定位第一次损失。 | 区分解析/路由遗漏与模型未使用；仅前者已定位于映射/DTO时改领域代码，不能用更多字段补偿执行层丢失。 |
| Q02 | 通用工件读取器/解析器是否已损坏输入，以及底层工作簿读取边界。 | S2/S4专表坐标、公式缓存和物理意义属于映射；保留已证实修复，其他格式/OCR不扩为无依据的全格式重写。 |
| Q03 | 量、单位、工况、规则条件是否在工具/JSON/摘要/恢复中完整到达；计算工具实际输入输出一致。 | 选规则、工程比较与风险判断仍属领域/模型；撤销固定电压补算保留。通用机制核清后才决定是否需更强判断合同。 |
| Q04 | 两章拿到的快照、版本、会话上下文及共享结果是否一致。 | 同对象/条件/规则下的专业一致性由领域审查；同源表不能保证同一设备或正确结论，不把专业裁决塞进Kernel。 |
| Q05 | 审查者实际输入和工具可见性是否足以查漏，当前结果和链接是否在修订/交付丢失。 | 对象预期集合与正文/行动处置留在Reporting；新锚点只作定位，必要性、可选缺失和成本需复核，不能当语义通过门禁。 |
| Q06 | 原文、来源身份和归档版本是否真实可读，引用在恢复/新Run是否指向正确字节。 | 标准现行性、适用条件、条款蕴含需要可信来源和专业判断；ID/日期字段不是证明。规则读取局部修复先纳入E3整体检查。 |
| Q07 | 实际Provider/model/route及Schema、工具/上下文是否一致，检索结果是否真的送达。 | Skill与知识选择是领域配置；通用词片段回退是候选机制，不等于语义召回。不能先换模型解释所有错误。 |
| F01 | 先量化真正执行的任务、重试、恢复和交接，排除框架重复派发。 | 再判断写审/Cross/Chief/Final各自新增价值，决定最小阶段组织，不能仅按角色名合并。 |
| F02 | 对比输入保真、工具可用、会话状态相同的路径，防止协议故障污染判断实验。 | “先判断再写”是设计假设；不能因增加记录字段而认定已验证，应与更小的既有parts/引用接口比较负担与效果。 |
| F03 | 请求去向、实际Schema/输入输出、摘要、缓存及关键路径的完整计量口径。 | 历史样例只作诊断；最终一次代表性实测比较当前实现，新增元数据的成本也纳入，不用字符量/活跃时间求和代替费用/墙钟。 |
| F04 | E1–E6的生产边界证据和跨入口复核；未经过Provider的中立能力不作LLM通用性证据。 | 实现、样例人工编辑、模型实际结果分别记录；不以文件位置、Schema通过、测试数量或多个角色同意代替根因/质量证据。 |

### 修订后的实施顺序

1. **先固定共同问题的责任边界（S03–S06、F03/F04）。** 从同一Task/Agent定义和显式工件引用出发，对照Module/Chief/Cross/Final/Aggregate的实际工具集、模型Schema与调用校验；明确Task是否为工具上限、工件读取器如何声明/派生、Runtime和Capability各负责什么。追到Compiler冻结依赖和Executor端口；若定义到调用不一致，修首次偏离处，不在每个角色另加补丁。
2. **核清通用请求、上下文及恢复（S04/S05、Q01、E2/E4/E6）。** 把工具Schema、消息、Provider序列化开销和模型窗口纳入同一预算核对；区分准确计数、估算与未知，不硬改字符系数当作真实token。用现有会话/续写/工件接口回放当前事实、版本、工具调用结果配对以及same-run恢复。复用现有通用AgentLoop/Provider适配捕获没有报告字段的请求，不能再让固定返回10的Invoker替代该边界；不为制造第二消费者复制业务框架或新增一套测试能力。
3. **重新评估最近六组实现的必要性和归属（Q01–Q07、S02/S06）。** 原始读取和已证实的机制修复保留。对assessments、observation_links、重复输入投影与检索回退逐项回答：缺口是否仍在、现有合同为何不足、是否只是定位/诊断、能否简化、恢复是否保留、增加多少输入/输出。没有证据时标为候选，不继续扩张新字段/阶段，不盲目撤销已提交内容，也不将迁目录当作修复。
4. **再补领域质量并决定阶段（Q03–Q07、F01/F02）。** 在实际输入/工具/版本可靠后核对量纲、工况、规则选择、对象身份、正文/行动处置和专业审查职责。某些字段、规则或角色确需领域实现时明确保留；只有业务中立且有真实共享消费者的机制才上移。不得增加配电Kernel Action、通用“专业正确性证明器”或额外审批链。
5. **收束到原验收（S01–S06、Q01–Q07、F03/F04）。** 用已有输入/Run定位并复核通用缺口，不要求重跑完整报告才能分析。仅真实遵循、语义质量、Provider token/缓存和墙钟等剩余效果放到最后一次代表性运行；官方标准状态核验仍需可信来源。用户已暂缓模型配置，不重复索要，也不能将尚存机制缺陷归为“等模型验收”。

### 本次修订的验收边界

- 每个问题记录“原始输入 → 已编译定义/Task → Provider可见消息/工具Schema → 工具执行 → 提交/恢复 → 下游消费”的首次差异、责任层及反证；没有差异证据时不预先指定改Kernel或加领域字段。
- 同一逻辑分别经过相关角色/入口；单Module成功不能代表其余Provider。相同问题能在无报告字段的真实AgentLoop/适配器调用边界出现，才作为通用机制证据；确定性Agent端口成功只证明端口。
- 对正确性至少区分未送达、不可读、已送达但未使用、已使用但选错规则/推理错误、汇总/恢复丢失；对成本同时记录删减与新增负担。结构化记录、锚点和检索命中不作为专业正确率。
- 继续只维护本指导和唯一状态文件；使用已有Trace和受影响检查验证真实缺口，不扩充只镜像实现的合成用例，不运行全量回归，不合并PR。本次计划复核本身不修改生产代码，不把审计发现写成已经修复。

## 对原分析需要修正的结论

- 历史输出量支持优先研究审查成本，但不能单凭平均耗时和输出量断言输入是次要成本。缓存、重复输入、模型差异和并行关键路径都需要区分。
- 不应继续引用“所有24次均温升混淆”概括全部实验。已有后续typed批次及不同错误分布；应按原始批次核对分母，不能忽略已经存在的反例。
- 默认取消摘要、直接提高工具上限、拆分2.4、自动从自由正文生成全部Claims、仅机器报警才语义审查，均仍是待验证设计选项，不是已证实收益。

## 初版实施顺序与交付物（历史，已由2026-10-19复核顺序替代）

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
