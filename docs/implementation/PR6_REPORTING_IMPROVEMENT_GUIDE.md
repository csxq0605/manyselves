# PR #6 报告性能与质量改进指导

适用 PR：[PR #6](https://github.com/csxq0605/manyselves/pull/6)，实施分支 `agent/declarative-runtime-implementation`。

核查基线：`927aef5`，2026-10-15（Asia/Taipei）。依据：[原始会话归档](../../run.md)、[既有质量与编排研究](../research/decisions/REPORTING_COST_QUALITY_ORCHESTRATION.md)、当前源码及归档实验。下表的“当前证据和状态”是这一基线的核查快照，不代表后续实时进度。

后续取证：[全清单首轮证据与实施依据](../research/decisions/REPORTING_PR6_EVIDENCE_AUDIT.md)已对下表基线补充核查，包括S04摘要范围与实验评分版本的更正；实现进度仍见唯一状态文件。[后续生产链核查](../research/decisions/REPORTING_PR6_RULES_AND_BINDINGS.md)进一步更正S06：内部ModuleSubmission包含Claims，不表示当前模型重复提交Claims；当前作者已使用紧凑提交，运行时生成整段来源绑定。

本文固定本 PR 的完整工作范围、条目编号和验收口径；实施顺序按文内根因复核修订，当前以2026-10-19再次复核后的有界工作项为准。动态进度、当前切片、验证结果与下一步统一记录在 [RUNTIME_EXTRACTION_STATUS.md](RUNTIME_EXTRACTION_STATUS.md)，引用本清单编号；不另建一份会逐渐分叉的状态账本。架构边界继续遵循 [AGENTS.md](../../AGENTS.md) 和[最终架构方案](../architecture/AI_NATIVE_RUNTIME_EXTRACTION_PLAN.md)。

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

首次复核基线：`6e25dda`；本次再核基线：`35cf858`。保留S01–S06、Q01–Q07、F01–F04全部目标。首次复核补充了通用层盲区，但把过多条目串成了“先排除全部核心问题”的前置链；本次改为按已证实缺口和实际依赖推进。代码位于Capability目录不能证明根因属于领域；确定性中立能力也不能证明模型循环正确。这两点都不构成重新审计整个平台的理由。

原始讨论还明确提出比较“一位主笔＋资料工具＋一次来源审查”“专题分析并行＋一位主笔”“章节并行＋共享事实和判断”（[原会话](../../run.md)，搜索“重新构思报告流程”）。F01/F02必须交付这三种方案的职责、依赖与代价比较，不能仅保留现有角色并一直标记待真实模型。实现位置服从根因；改动应适用于合同允许的输入，不依据项目名、特定E-ID、样例数量或某份成稿选分支。

### 复核结论与源码依据

| 证据 | 核查结果 | 能证明什么／尚不能证明什么 |
| --- | --- | --- |
| E1 共用工作流骨架 | [应用入口](../../manyselves/application/workflow_projection.py)、[Binding目录](../../manyselves/runtime/capability_binding.py)、[Compiler](../../manyselves/kernel/workflow/compiler.py)、[InvokeAgentExecutor](../../manyselves/kernel/executors/base.py)、[Host](../../manyselves/runtime/workflow_host.py)按定义和端口连接；Host调用Kernel.transition后委派Executor，Agent端口允许Capability提供Invoker。 | 已定位共同执行骨架；本次未发现能解释报告缺陷的Kernel状态转换反例。源码连接与历史验收不足以排除Compiler、并行/Join或恢复缺陷。出现版本/结果/路由错配时应回到这些层定位。 |
| E2 中立能力的验证盲区 | [parameter-adjustment工作流](../../manyselves/capabilities/parameter_adjustment/workflows/parameter-adjustment.yaml)确有invoke_agent，但其[适配器](../../manyselves/capabilities/parameter_adjustment/adapters/runtime.py)中_ParameterAdjuster.invoke直接返回value=10，build_runtime_binding丢弃services。 | 证明共用Compiler/Host/Agent端口和确定性工具，不经过Provider、AgentLoop、模型工具循环或上下文重建。不能再以该能力运行成功证明“另一领域也验证了通用LLM执行”。这不是要求把确定性参数功能强改成模型调用。 |
| E3 工具策略分歧及实际任务反证 | [Module组合](../../manyselves/capabilities/distribution_reporting/runtime/module_provider.py)保留按引用派生的读取器，其余组合仍经[compile_agent_access](../../manyselves/capabilities/distribution_reporting/runtime/artifact_access.py)过滤。相同简化envelope回放只有Module暴露open_artifact；但实际[Chief任务](../../manyselves/capabilities/distribution_reporting/tasks/chief-chapter-edit.yaml)和[联合Cross任务](../../manyselves/capabilities/distribution_reporting/tasks/cross-joint-runtime-review.yaml)均已声明open_artifact/search_text。[Final准备](../../manyselves/capabilities/distribution_reporting/runtime/final_delivery_binding.py)则明确将章正文和source_context内联，Task/envelope均只声明submit_result。 | 已证明组合策略分歧，尚未证明Chief/Cross生产任务缺读取器。Final/Aggregate是否需额外读取取决于其职责与实际交付材料。先列出实际Task→envelope→注册工具及需要读取的引用；只修可定位的不可达资料或定义/执行不一致，不把五条路径工具数量相同作为目标。 |
| E4 通用预算边界 | [AgentLoop](../../manyselves/runtime/loops/agent_loop.py)的_estimate_tokens用固定0.3字符系数；_chat_with_retries拿到tool_definitions后，_trim_typed_context_to_budget仍只传messages、context_window与max_output，未纳入工具Schema。 | 已确认预算核对不覆盖完整Provider请求。中文/结构化内容与实际模型token差异、Schema遗漏是否导致过历史超窗或语义遗漏尚未测量；不能直接归因所有历史摘要/遗漏。应先修整请求预算口径，不能先通过追加领域字段扩大请求。 |
| E5 Schema来源已部分核实，冻结边界仍待定位 | [reporting_tool_input](../../manyselves/capabilities/distribution_reporting/contracts/reporting_tool_input.yaml)为空；[ToolRegistry](../../manyselves/runtime/tools/registry.py)反射后有任务级替换。实际submit_result的Schema由[submission_schema](../../manyselves/capabilities/distribution_reporting/runtime/contracts/submissions.py)从结果模型生成，同时传入[提交工具](../../manyselves/capabilities/distribution_reporting/runtime/collaboration_tools.py)；模块及Chief紧凑提交另经model_validate后装配。新增assessments也使用模型Schema与[normalize_assessments](../../manyselves/capabilities/distribution_reporting/runtime/assessment_context.py)。 | 不是“空文件Schema所以模型提交没有合同”。当前有Schema来源、处理与校验；尚未证明定义冻结、传输转换、恢复后使用的版本完全一致。只沿具体字段/版本差异补证或修复，不预设需要新Schema框架、统一注册重写或Compiler改造。 |
| E6 会话与上下文路径 | [AgentExecutionService](../../manyselves/runtime/agent_execution.py)管理稳定会话，[ProviderAgentSessionFactory](../../manyselves/runtime/provider_agent_session.py)负责构造/重配；[TaskContextRebuilder](../../manyselves/runtime/loops/context_rebase.py)是可选hook，多个Reporting bridge分别绑定。AgentLoop还识别write_result_part(s)及历史draft标记。 | 需在受影响入口核对会话复用、摘要、continuation和恢复后的当前输入与工具定义；发现具体差异再扩大核查范围。不能把某个bridge接线成功推为通用服务默认成立；硬编码工具协议的所有权也须核实。现有Hook已能表达的机制不另起框架。 |
| E7 新领域协议的证明边界 | [判断投影](../../manyselves/capabilities/distribution_reporting/runtime/assessment_context.py)核对显式记录集合和字符串锚点；assessments、observation_links允许缺省，规则verified仍为作者声明；来源同坐标仅作为同源比较依据。 | 可以暴露未记录/未定位信息，不证明选对规则、同条件一致或行动正确；可选记录也不能保证作者实际逐项判断。新增字段是设计选择，尚未证明最小必要、优于既有合同或净成本有收益，应核清与这些记录相关的实际输入/工具差异，再决定保留、简化或调整；不依赖E3–E6全面审计。不能把它当统一成功机制。 |
| E8 检索和输入仍有独立领域问题 | Excel固定电压补算/缓存等已定位在Reporting映射；[知识上下文](../../manyselves/capabilities/distribution_reporting/runtime/research/knowledge_context.py)的候选选择及[词片段匹配](../../manyselves/capabilities/distribution_reporting/runtime/research/reference_matching.py)当前只有Reporting消费者。 | 已有明确数据/规则缺陷应保留修复；通用词法不等于已服务多个Capability。候选变多不证明语义召回正确；无第二个真实消费者证据时不为“看起来通用”迁移目录。 |

E3回放未调用模型，仅比较组合函数；本次真实Task定义核查限制了其外推范围。E4是已确认、可直接实施的预算缺口；E2是证据覆盖不足；E1/E5/E6主要是按症状定位的检查方向。不能将这几类都列为已证实故障，也不能把完成它们的全面审计设为所有质量和流程工作的共同前置条件。当前预算裁剪还受context_rebuilder是否绑定影响，实施时须明确覆盖入口，保留必需任务状态和工具调用/结果配对。

### 17项的责任边界与剩余交付

下表规定交付范围，不另记实时进度；已实施行为、提交和实测状态仍以唯一状态文件为准。检查方向只有出现与本条相关的差异才升级为修复，不能自动变成全平台前置任务。

| 编号 | 责任边界与剩余交付 | 收束条件 |
| --- | --- | --- |
| S01 | Reporting保留advisory/blocking含义；整理实际返工触发、被修改范围和审查职责。重复派发只在出现相同任务/版本重复执行证据时追到Runtime。 | 已实现的非自动返工不重复开发；缺历史逐finding数据明确不可恢复，实际误报/返工收益留待实测。 |
| S02 | 复用已贯通的Cross 15→10字段及Chief/Final交接；逐字段列出新增分析与已有正文的重复，纳入F01职责比较。 | 对保留/合并字段给出消费者与信息理由；没有丢失证据不重新打开整套Join审计。 |
| S03 | 核对真实提交协议是否造成重复续写，并保留直接结构化提交指引。Schema检查追实际发送、处理和结果，不从内部模型推导作者输出。 | 协议故障有首次差异及修复；模型是否仍先输出整份审查解释另作遵循率问题，不靠截断隐藏。 |
| S04 | 通用AgentLoop修工具Schema未纳入预算的缺口；明确消息、工具、输出预留、窗口来源及估算误差，并检查绑定/未绑定重建器路径。 | 必需事实/当前状态及完整工具单元保留；装不下时用既有耗尽结果报告，不能静默裁掉事实。短任务摘要策略与真实摘要收益仍待比较。 |
| S05 | 保留稳定前缀、动态状态分离和Provider缓存布局；预算改动只复核受影响的序列化、状态更新与恢复。 | 不因真实命中率未测重复重构已验证布局；缓存和费用改善只有Provider数据才能结项。 |
| S06 | 核对实际Task/envelope/工具注册及引用可达性；统计写入、列举、提交各自职责，评估新增assessments的输入输出负担。 | 修实际合同不一致；已紧凑提交和无需固定列举不重复实现。拆2.4、提高并发/工具上限须有瓶颈及合并代价依据，否则明确不采纳。 |
| Q01 | 用已有输入跟踪原始对象→解析→路由→实际请求/读取→成稿的首次差异；保留完整事实与未引用事实可审路径。 | 已送达但未采用不再归为解析修复；遗漏对象处置包含判断、行动或明确未知，不能用来源ID命中替代。 |
| Q02 | 保留dimension、合并环境条件、公式缓存及缺值修复；按已知格式缺陷核实读取入口。 | 已修缺陷维持关闭；没有新增复现不扩成全格式/OCR或Excel重算工程。缓存新鲜度、大表内存记录为具体限制。 |
| Q03 | 核对量、单位、对象、工况及规则条件；保留撤销固定电压补算。比较现有事实/计算/parts合同与新增判断记录各自承担的内容。 | 输出“已知、可计算、缺条件、未选规则、无适用规则”的处理依据；专业判断与模型选择规则不被字段校验冒充。只在传输有差异时修通用层。 |
| Q04 | 在共同快照上核对同对象同条件矛盾和不同条件合理差异，检查跨章传递。 | 同源坐标/路由副本不当设备身份或统一结论；若版本不一致再追Join/恢复，不能预先全面排查。 |
| Q05 | 复核预期集合、assessments与observation_links的消费者、缺省语义、修订失效及额外负担；与已有正文/来源绑定比较。 | 对每组字段作保留/简化/替代决定并给依据；定位能力和专业覆盖分别验收，不新增语义通过门禁。 |
| Q06 | 检查规则原文、归档版本及状态来源在实际审查/修订任务中可达；继续按可信来源核现行性、适用性和引用蕴含。 | 作者声明不等于核验；网络阻塞仅挂起对应标准状态核验，不挂起资料传递与合同修复。 |
| Q07 | 保留已改的蒸馏条件约束、Skill消费边界及来源候选机制；核对身份/Task/Skill/知识的指令冲突与检索遗漏，不只检查模型route。 | 手工修改14份材料不算蒸馏机制成功；无精确命中时词片段回退不算语义召回成功。角色选模和新蒸馏实际效果待Provider可用。 |
| F01 | 列出模块写审、Cross、局部回归、Chief、Final的输入、独立产出、重复推导、返工触发和串行依赖，完成三种写作组织方案比较。 | 对每个阶段明确保留/合并/取消/条件触发的依据及责任承接；不能只有现有流程图或“待模型”。不需先排除所有Runtime假设。 |
| F02 | 比较直接写、写后审、先判断再写如何复用同一事实/规则和保存未知；结合Q03/Q05决定最小判断交接。 | 判断优先仍是候选，新增可选记录不等于模型按顺序完成推理；无需为比较实现三套生产流水线。 |
| F03 | 复用历史525请求和现有Trace，区分已删重复、必要工作、新增负担与未知；列出当前关键路径和最后实测口径。 | 不能恢复的数据明确缺失；字符/估算token不冒充费用，并行耗时不求和当墙钟；对新增字段计净负担。 |
| F04 | 每项分别记录机制状态、根因证据、候选决策、效果验收；保留修复及反证，只有受影响变化才补定向验证。 | 不按测试数量、目录归属或固定值中立Invoker结项；未通过Provider的证据明确限定到调用边界。 |

### 实施顺序、交付与停止扩张条件

1. **完成有界的机制修复（S03–S06、Q01/Q06）。** 优先修E4已确认的完整请求预算缺口；同步沿实际作者、审计、Cross、Chief、Final、Aggregate任务形成工具/引用/Schema对照。E3/E5只修实际不一致，恢复检查限定在变更涉及的会话重绑、工具结果与same-run路径。交付为可定位的前后差异和受影响入口的完整行为；找不到差异的检查项记录“本边界未发现”，继续下一项，不扩成全Kernel/Compiler/Join审计。通用层修复不要求人为制造第二个Capability；领域代码上移仍遵循真实共享消费者约束。
2. **完成判断交接和知识使用的设计处置（Q01、Q03–Q07、S06/F02）。** 对事实、计算、专业判断、正文引用和行动分别确认已有所有者；逐项评估assessments、observation_links、候选检索及Skill指令冲突。交付为现有合同不足的具体依据、字段保留/简化/替代决定及新增负担。已证实读取修复保持关闭，未知保留未知；不会因一个已送达样例仍写错而继续叠字段。此项的静态核查不依赖步骤1全部结束，只将受具体缺陷影响的部分挂起。
3. **完成流程组织取舍（S01–S03、F01/F02）。** 基于已有阶段依赖和历史轨迹，比较一位主笔、专题分析并行＋主笔、章节并行＋共享判断三种方案的最少模型阶段、重复输入/输出、串行等待、修订范围及审查责任。先交付可审阅的比较和选择理由，再按证据实施必要的定义/交接改动；已知不可离线判定的质量胜负留到实测。不能要求先实现三套流程，也不能以缺模型为由不做职责比较。该工作可与步骤1/2独立部分并行推进，只有改变判断交接的方案依赖步骤2的合同决定。
4. **收束剩余验收（F03/F04与各项效果）。** 所有实现缺口和设计选项必须有明确处置后，准备最后一次代表性真实运行的输入、版本、模型、关键路径、token/缓存及对象质量口径。已有历史数据用于定位，不伪装同版本对照；没有可比基线时只报告当前绝对结果。用户已暂缓模型配置，不重复索要；官方来源受限和真实模型测量分别挂起。发现失败保留现场，不自动反复完整重跑。

这四项是原17项的工作组织，不是新增范围或四道串行门槛。机制修复可复用，领域规则按合同适用；“通用”不承诺所有模型在所有专业任务上都判断正确。新的检查或改动必须能指回原条目、具体缺陷或待决设计，不能单凭“可能还有核心问题”无限扩展。

### 本次修订的验收边界

- 对已知故障沿“原始输入 → 已编译定义/Task → Provider可见消息/工具Schema → 工具执行 → 提交/恢复 → 下游消费”定位首次差异；追到足以决定责任层即止，不要求每项重复检查整条链。
- 复核真实相关角色/入口及其不同合同。相同输入不代表相同职责；不追求所有角色暴露相同工具。业务中立的调用边界可证明通用缺陷，无需先建另一个模型Capability。
- 对正确性区分未送达、不可读、已送达但未使用、选错规则/推理错误、汇总/恢复丢失；对象集合及锚点只作诊断，不自动升级专业判断保证。
- 已修机制、已接线待评价的设计、未实施缺口和待外部证据分别记录。保留已完成的结构性工作，既不把所有未测效果标为未实现，也不把所有剩余工作归为等模型。
- 继续只维护本指导和唯一状态文件；使用已有Trace及受影响检查，不扩充只镜像实现的用例，不运行全量回归、不合并PR。本轮只复核计划，不把静态发现写成已修。

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
