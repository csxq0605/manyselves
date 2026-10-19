# PR #6 逐项实验记录（2026-10-19）

本文件只记录本轮每一项改动的“改前 / 改后”对照实验。所有实验离线运行、调用 Provider 0 次；脚本在 `scripts/benchmarks/`，原始结果 JSON 与本文件同目录。真实耗时、费用和缓存命中率仍需真实 Provider 运行确认，这里只证明请求构造层面的变化。

分词器（仅实验依赖，不是项目依赖）：Hugging Face 官方 `XiaomiMiMo/MiMo-V2.5-Pro`、`Qwen/Qwen3-8B`、`deepseek-ai/DeepSeek-V3` 的 `tokenizer.json`。生产默认模型是 `mimo-v2.5-pro`。

## S04 请求预算：中文 token 低估与工具 Schema 漏算

脚本：`uv run --with tokenizers python scripts/benchmarks/request_token_estimate.py --tokenizer mimo=... --tokenizer qwen3=... --tokenizer deepseek=...`
结果：[`2026-10-19-s04-token-estimate.json`](2026-10-19-s04-token-estimate.json)

语料来自已去隐私的归档材料：报告正文、2.4 事实包、证据 JSON、S4-6 表格文本、全部 Agent 提示词、2.4 作者任务实际注册的 17 个工具 Schema。括号内为“估算 / 三个分词器中的最大值”。

| 组成 | 字符 | 实际 token | 改前（固定 0.3/字符） | 改后（按字符类别） |
| --- | ---: | ---: | ---: | ---: |
| 报告正文 | 60,000 | 42,123 | 18,000 (0.43) | 42,569 (1.01) |
| 事实包 | 22,410 | 12,821 | 6,723 (0.52) | 12,729 (0.99) |
| 证据 JSON | 60,000 | 32,841 | 18,000 (0.55) | 30,179 (0.92) |
| 表格文本 | 20,731 | 14,290 | 6,219 (0.44) | 13,897 (0.97) |
| Agent 提示词 | 32,254 | 12,643 | 9,676 (0.77) | 14,108 (1.12) |
| 工具 Schema | 14,357 | 3,479 | 改前不计入预算 | 4,380 (1.26) |
| Python 源码 | 15,665 | 3,583 | 4,699 (1.31) | 5,099 (1.42) |

按历史最大轮次构造的 2.4 作者请求：实际 76,694 token（消息 73,215 + 工具 3,479）。改前预算只算出 38,523（50%），改后 75,738（99%）。

结论与改动：

- 中文内容被低估约一半，128k 窗口检查实际在约 250k 真实 token 才会触发，等于失效；工具 Schema 完全没进预算。改为按非 ASCII / 数字 / 字母 / 其他 ASCII 四类系数估算（0.72 / 1.0 / 0.22 / 0.5），工具 Schema 计入裁剪预算和 Provider 准入估算。
- 历史 2.4 作者轮次在 `working_memory_tokens=36000`（旧估算）时实际已到 66k–74k token，与上面的组合请求一致。因此阈值同步换算为 72000，**压缩触发点按真实 token 保持不变**；不同步换算会让摘要提前约 2 倍触发，反而更慢。这不是调大阈值求收益。
- 未覆盖：没有 Provider 实测 usage 对照；Claude 分词器不公开，未纳入。

## S05 缓存布局：可变状态挡住了历史

脚本：`uv run --with tokenizers python scripts/benchmarks/request_cache_prefix.py --tokenizer mimo.json`
结果：[改前](2026-10-19-s05-cache-prefix-before.json) / [改后](2026-10-19-s05-cache-prefix-after.json)

实验走生产路径（Reporting `bind_task_context` → `TaskContextRebuilder` → `_with_live_task_state` → 预算裁剪 → Provider 消毒 → Anthropic / OpenAI 兼容序列化），用真实写入工具和归档材料构造一次 2.4 作者会话：6 轮证据检索、6 轮章节写入、1 轮提交，共 13 次请求。

| 序列化 / 缓存模型 | 改前可复用 | 改后可复用 |
| --- | ---: | ---: |
| OpenAI 兼容（MiMo 等自动前缀缓存） | 60.6% | 79.8% |
| Anthropic 原生（显式断点，20 块回看） | 52.5% | 71.8% |

13 次请求总输入约 32.6 万 token；按 OpenAI 兼容口径，每次会话不可复用的输入从约 12.8 万降到约 6.6 万。

根因：`<current_task_state>`（含全部已写章节全文）和 `<runtime_task_state>` 每轮都变，却排在对话历史前面，所以历史每轮都要全价重算。改动：稳定前缀 → 只追加的历史 → 可变状态放最后；AgentLoop 在可变状态前的最后一条历史上加滚动缓存断点（总断点数不超过 Anthropic 的 4 个），Anthropic 序列化支持在 tool_use / tool_result 块上携带断点；预算裁剪始终保留最新一组交互。剩余不可复用部分主要是每轮重发的当前草稿全文，留给 S06 / F01 处理。

未覆盖：缓存命中的真实计费、模型是否因状态放在末尾而改变行为，需要真实运行确认。

## F03 运行指标与当前关键路径

脚本：
- `scripts/benchmarks/run_metrics.py --ledger <usage.jsonl>`：读一次 Run 的 usage 账本，按阶段输出请求、输入/缓存/输出 token、压缩请求占比、Provider 活跃**并集**时间、阶段窗口，并按请求粒度回溯实际等待链（最后结束的请求 → 它开始前最后结束的请求 → …，合并成阶段段）。`--sessions` 读归档的会话窗口（旧 Run 只留了这一层）。
- `scripts/benchmarks/reporting_critical_path.py`：按生产 YAML（顺序、if、goto、parallel/join、subworkflow，含按模块生成的 Lane）走一遍工作流，以命名场景替代模型决策，输出串行 LLM 深度和会话总数。

结果：[历史阶段](2026-10-19-f03-history-stages.json) / [当前关键路径](2026-10-19-f03-critical-path.json)。用合成账本核对过：两作者并行时等待链选中较慢的一路，再接其审查、Cross、Chief。

**改前口径的问题**：账本原有 `summarize` 只按组求和。历史 525 请求的 `provider_active` 求和为 340 分钟，实际 Provider 活跃并集 108 分钟，观测跨度 118 分钟，求和会把耗时夸大约 3 倍，也给不出关键路径。

**历史 Run（旧编排、mimo-v2.5、525 请求、118 分钟）阶段窗口**（会话窗口重叠，不可相加）：

| 阶段 | 会话 | 请求 | 其中压缩 | 窗口（分钟） |
| --- | ---: | ---: | ---: | --- |
| 模块写作 | 5 | 144 | 24 | 0 – 19 |
| 模块审查/返工/复审 | 10 | 270 | 71 | 7.5 – 75.4 |
| Cross（旧：逐 owner） | 5 | 20 | 7 | 36.7 – 77.9 |
| Chief | 2 | 86 | 18 | 79.3 – 115.3 |
| Final | 2 | 5 | 0 | 102 – 118 |

压缩请求 120/525（23%），压缩活跃并集约 60 分钟，与主流程重叠。最长的单会话是 2.2 审计员（50 请求，其中 20 次压缩，68 分钟）。

**当前工作流的结构关键路径**（`full-report`，五模块、三章）：

| 场景 | 串行 LLM 深度 | LLM 会话 | 关键路径构成 |
| --- | ---: | ---: | --- |
| 各审查首读即通过 | 5 | 17 | 写 1 · 审 1 · Cross 1 · Chief 1 · Final 1 |
| 每个审查边界返工一轮 | 12 | 44 | 写 1 · 模块审 3 · Cross 4 · Chief 1 · Final 3 |
| 同上 + Cross 局部审查再返工 | 14 | 54 | 写 1 · 模块审 3 · Cross 6 · Chief 1 · Final 3 |
| 模块返工两轮 | 16 | 64 | 写 1 · 模块审 5 · Cross 6 · Chief 1 · Final 3 |
| Final 用满 3 轮 | 18 | 66 | 写 1 · 模块审 3 · Cross 6 · Chief 1 · Final 7 |

结论：

- 首读即通过时只需 5 个串行阶段；常见的“每处返工一轮”是 12–14 个，耗时主要来自三处审查循环，不是写作本身。Cross 每轮在关键路径上加 4–6 个阶段（联合审 → owner 修订 → 局部审 →〔局部修订 → 局部复审〕→ 联合复审），是最长的单项。
- 历史 Cross 按 owner 与模块审查流水重叠；当前联合 Cross 要等五个模块全部 Join，最慢模块决定起点。
- 这些是结构计数，不是耗时预测；每个阶段的请求数和时长要靠最后一次真实运行用 `run_metrics.py` 测量。F01 的拓扑比较直接用 `reporting_critical_path.py` 对改后的 YAML 重跑。

## S06 / Q05 作者判断记录（assessments）与 Chief 链接（observation_links）的净负担

脚本：`uv run --with tokenizers python scripts/benchmarks/assessment_burden.py --tokenizer mimo.json`
结果：[改前](2026-10-19-s06-assessment-burden-before.json) / [改后](2026-10-19-s06-assessment-burden-after.json)

做法：用生产工具构造（`build_module_provider_tools`、`submission_schema`）量每次请求携带的 Schema；用归档五模块事实包的每条观测（2.1–2.5 共 316 条）合成作者判断记录，对象、来源坐标、事实文字均取自真实记录，推理只用短模板，因此是**下限**；下游副本走生产 `assessment_source_context`（Chief 与 Final 每次请求都携带）。“分组”按同一小节、同一原始事实文字合并记录。脚本按当前合同（`scope`/`basis` 是否必填）自动选择改前/改后记录形态。

**负担在哪里**：Schema 不是主要负担——作者每次请求多 860 token、Chief 多约 340 token，且都在可缓存前缀里。主要负担是作者必须生成的输出和下游每次请求的副本：

| 指标（mimo 分词） | 改前 | 改后 |
| --- | ---: | ---: |
| 作者判断输出，五模块合计（分组 / 逐条） | 29,069 / 40,290 | 15,553 / 21,232 |
| 其中 2.4（分组 / 逐条） | 12,938 / 16,820 | 6,705 / 8,500 |
| 单小节最大判断输出（2.2，逐条） | 8,200 | 4,325 |
| Chief/Final 每次请求的 `module_assessments`（分组 / 逐条） | 31,145 / 41,159 | 16,697 / 20,815 |
| Chief `observation_links` 输出，三章合计（分组） | 5,514 | 5,514（保留） |
| 作者每次请求的工具 Schema 增量 | 860 | 922 |

作者单次回复上限是 12,288 token，改前 2.4 的判断记录下限就与它同量级，且这是在正文之外；2.2 单小节的判断记录加正文更容易触发 Max Token 续写。

**逐字段处置**（消费者见 `assessment_context.py`、Chief/Final source_context、`revision_impact.py`、`final_observation_context`）：

| 字段 | 处置 | 依据 |
| --- | --- | --- |
| `evidence_ids` | 保留 | 分组身份；未判断集合、修订影响、Final 必需行动集合都由它计算 |
| `disposition` | 保留 | Final 据此确定 concern/undetermined 的行动要求；输出极短 |
| `scope` | 改为可选，只写 E-* 记录没有的条件 | 对象/部位/时点就在 E-* 记录里；审计、Cross（含 `source_observation_groups`）、Chief、Final 都已拿到原始证据。模型复写这些字段既重复又可能写错 |

> **Q07更正：** 上行“Final都已拿到原始证据”不成立。Final审计（`chief-editor-auditor`）只有`submit_result`，读不到E-*记录，S06后看到的是没有对象的E-ID。已由运行时派生的`observation_identities`补上，见下文Q07。
| `basis` | 改为可选，只写正文没说的理由 | 推理在 `narrative_anchor` 指向的正文里，审查者本来就必须读正文；只有缺观测、缺工况、规则未核验、不适用或数量比较等正文未写的才需要记录 |
| `rules`、锚点、`no_action_reason` | 保留 | 规则选择和适用性不一定写在正文；锚点是“定位”信号的唯一来源，体积小 |
| `expected_evidence_ids`、`rule_source_refs` | 不变（运行时生成） | 已由运行时枚举，不由模型输出 |
| `observation_links` | 保留 | Chief 改写正文后只有 Chief 能指出位置；Final 的 `unmapped_action_evidence_ids` 依赖它；无运行时替代 |
| 下游视图 | 省略空的可选字段 | 空字符串和空数组不携带信息；未判断、未落入正文、无行动说明的定位列表照常由运行时计算 |

边界：没有新增门禁或判断阈值；`scope`/`basis` 缺省不是缺陷，也不会因此跳过语义审查。审计提示改为按 E-ID 读原始证据核对对象/位置/时点，并对照锚点处正文的推理。定位能力（锚点、未判断集合）和专业覆盖（审查者读正文+证据）仍分别验收。

未覆盖：模型在新合同下实际写多少判断、推理是否因此更多写进正文，需要真实运行用 `run_metrics.py` 看作者输出 token 和续写次数；合成记录的推理是短模板，真实推理更长时，`basis` 的节省会更大。

## F01 / F02 三种写作组织与阶段职责

脚本：`uv run python scripts/benchmarks/writing_organizations.py`（Provider调用0）
结果：[2026-10-19-f01-writing-organizations.json](2026-10-19-f01-writing-organizations.json)；结构深度沿用上节 `reporting_critical_path.py`。

做法：取归档完整运行（旧编排，525请求）每个会话的Provider活跃秒数，按角色归类（作者、模块审计、模块修订、Cross、Chief、Final），按三种组织的依赖关系组合：串行相加、并行取最大。该运行中模块修订会话同时吸收了Cross修复，所以“章节并行”把它只计入模块通道一次，Cross项只计审查会话。“专题并行＋主笔”取决于作者时间中判断所占份额 `s`（未知），按0.25/0.5/0.75给区间。上下文用生产token估算器量归档事实包。这些是活跃时间组成的下限模型，不是墙钟预测，也不是同版本对照。

**三种组织的串行时长与单会话负担**

| 组织 | 串行活跃时长（分钟） | 关键路径构成 | 单个写作会话需持有的事实（token） |
| --- | ---: | --- | ---: |
| 章节并行＋共享事实/判断（当前） | **110.4** | 最慢模块通道2.2：64.2（写7.2·审34.8·修22.2）＋Cross 14.1＋Chief/Final 32.1 | 12,729（2.4） |
| 一位主笔＋资料工具＋一次来源审查 | 264.0 | 写64.7＋单次审查107.7＋修订59.5＋Chief/Final 32.1 | 30,510（五模块合计） |
| 专题分析并行＋一位主笔 | 156.7–179.6（s=0.75–0.25） | 判断并行、主笔串行写全部正文；审查仍可按模块并行 | 46,063（事实＋五模块判断记录） |

- 两种替代组织去掉了Cross，但只有Cross超过各自的平衡点才划算：主笔方案167.7分钟、专题方案60.4–83.3分钟；历史Cross最长14.1分钟。
- 两种替代组织把多模块事实放进一个会话，是当前最大单会话的2.4–3.6倍。working memory是72,000，加上正文草稿、工具结果和规则原文后更早触发压缩。历史最长的单会话（2.2审计，50请求、20次压缩、68分钟）已显示单会话事实负担是慢的主要来源之一，替代组织会把它放大。
- 质量：主笔方案仍需要跨模块一致性检查，只是由同一作者承担，失去独立审查；专题方案的判断会话和写作会话分离，正是下文F02实验中判断错误稳定传播的形态。

结论：保留章节并行。当前最慢环节在单个模块通道内部的审查/修订（2.2审计35分钟），由S01（advisory不返工）、S03（直接结构化提交）、S04/S05（预算与缓存）和S06（判断记录减半）处理，不需要改组织拓扑。

**逐阶段职责、依赖与处置**（依据：`distribution-reporting-tail.yaml`、`cross_joint.py`、`cross_owner_runtime.py`、各Agent/Task定义）

| 阶段 | 输入 | 独立产出 | 曾有的重复推导 | 返工触发 | 串行依赖 | 处置 |
| --- | --- | --- | --- | --- | --- | --- |
| 模块写作 | 模块事实包、规则/Skill、模板 | 模块正文、Claims、紧凑判断记录 | 判断记录复写E-*对象/时点与正文推理（S06已去除） | — | 仅依赖准备阶段；五模块并行 | 保留 |
| 模块审查/修订/复审 | 当前提交、项目证据工具（search_project_evidence、open_project_source、calculate） | 对照原始资料的定位finding与verdict | 复审已只看有界差异（module_review_delta） | 仅blocking finding（S01）；预检失败先回作者 | 依赖本模块作者；模块间并行 | 保留；复审已是条件触发 |
| 联合Cross审查 | 五模块合并快照、assessments、source_observation_groups | 跨模块冲突finding、第三章唯一的关系综合（synthesis_inputs） | 不再复述单模块判断（提示与紧凑字段S02） | 仅blocking跨模块finding | 必须等五模块Join：比较对象是共同当前版本 | 保留；修复轮条件触发（needs-repairs） |
| Owner修订＋局部审查/复审 | 被分派finding、该模块当前版本 | 修订后模块的单模块证据核验 | 无：联合审查者没有项目证据工具，只核关系 | 仅当本模块有被分派修复，且通过预检 | 联合复审消费local_regression_review_refs；merge_repairs要求局部审计完成后才合并 | 条件触发（已是）；不能并入联合复审，也不能与之并行 |
| Chief分章 | 已批准模块、Cross综合、module_assessments | 第1/3/4章读者正文、observation_links | 下游判断副本31.1k→16.7k token（S06） | — | 依赖Cross结果；三章并行 | 保留 |
| Final审查/Chief修订/复审 | Chief成稿、上游判断与行动对象 | 成稿是否保留全部异常/未知、未新增无依据前置 | 不重做上游专业审计（角色定义） | 仅blocking；最多3轮 | 依赖Chief；三章并行 | 保留；轮次条件触发 |

未采纳：用“修订只改措辞”跳过局部审查。这需要新的语义变化判断门禁，按AGENTS.md须先征得同意；当前也没有误跑局部审查的成本证据。阶段顺序上没有可去除的串行依赖：上面每条依赖都对应一个下游真正读取的数据。

**F02：直接写、写后审、先判断再写**

| 方式 | 事实/规则复用 | 未知如何保存 | 代价与已知问题 |
| --- | --- | --- | --- |
| 直接写 | 一个会话 | 只在正文里；下游只能从正文重推 | Q05前的状态：Chief/Final找不到未判断和未处置对象 |
| 写后审（当前） | 作者在同一会话随正文写紧凑判断记录；审查者读同一事实 | `disposition=undetermined`，`rules`保留候选条款及`verification`/`applicability`中的缺失条件 | 每个模块多一次审查；判断输出是S06后的15.6k token（五模块合计，下限） |
| 先判断再写 | 判断会话与写作会话分别装入同一事实包（2.4：再加12.7k token） | 原型用单个unknown，丢掉了候选规则 | 每模块多一个串行阶段；2026-10-15实验（[记录](../decisions/REPORTING_COST_QUALITY_ORCHESTRATION.md)）四次把缺环境温度误解为缺规则 |

决定：不增加独立判断阶段。最小判断交接就是作者随正文提交的紧凑判断记录：同一会话、同一事实，没有第二次装载。它保留候选规则和缺失条件，避开原型的失败形态；审查者按E-ID对照原始证据与锚点处正文。判断优先仍只是候选；可选记录也不能证明模型先判断后写作。

未覆盖：三种组织的成稿质量胜负只能用真实模型比较；本节只比较依赖、活跃时长下限和上下文负担，未改Workflow拓扑。

## Q01 / Q03 / Q04 已知遗漏在哪个消费者首次可见

脚本：`uv run python scripts/benchmarks/observation_visibility.py`（Provider调用0）
结果：[2026-10-19-q01-observation-visibility.json](2026-10-19-q01-observation-visibility.json)

做法：取历史成稿已评审确认的三处缺陷，用历史小节正文作作者提交，经当前生产函数（`_review_evidence`、`module_assessment_views`、`source_observation_groups`）回放。Claims按旧整段绑定引用小节中出现的全部E-ID。判断记录比较两种作者行为：
- `narrative`：只为正文实际讨论的E-ID写记录。
- `grouped`：把全部引用合成一条`no_concern`记录，作为最坏情况。

“改前”是历史运行的合同：审查者只拿到被引用的E-ID，没有判断记录，也没有跨章同源对照。可见不等于被发现，审查者仍可能看漏。

| 缺陷 | 历史正文 | 改前：审查者能看到 | 当前：审查者能看到 | 当前：未判断信号（narrative / grouped） | 当前：Cross同源链接 |
| --- | --- | --- | --- | --- | --- |
| 剩余电流E-0071（11.3A，占运行电流45.20%） | 图注引用，正文称全部不大于2.1A、整体OK | 是（图注引用） | 是 | 有 / 无 | 无（单一路由） |
| SPD四处原表NG | 图注全部引用，正文只确认两处 | 是 | 是 | 有 / 无 | 无 |
| 测温末两行（E-0230–E-0233） | 两节都未引用 | **否** | 是，且标为未绑定 | 有 / 有 | 有：2.4.4与2.2.2.1完全相同的路由副本 |

**首次差异的位置：**
- 测温两行在旧流程是送达缺口：作者事实包里有，审查者看不到，任何下游也看不到。当前审查者拿到范围内的全部事实（`bound_submodule_ids=[]`标出未绑定）。未判断列表经同一视图到达模块审计、Cross、Chief和Final。Cross还拿到两模块同一原始观测的链接。这一处已由既有机制闭合，与作者行为无关。
- E-0071与SPD在旧流程已送达审查者，缺陷在作者判断（Q03），旧审查也没发现。当前只要作者按合同只记录正文讨论过的对象，它们就进入未判断列表。如果作者把不同的观测合成一条`no_concern`，结构信号消失，只剩审查者逐条比对原始事实与正文。

**改动：**
- `evidence-auditor`提示补一句：合并多个E-ID的记录要逐个核对，原表标记、数值或工况不同的观测不能由同一disposition覆盖。作者工具说明已要求“只有disposition、推理与锚点相同才合并”，这一句补上审查侧。
- 同步`manyselves/templates/reporting/agents/evidence-auditor.md`镜像。S06漏改了它，镜像仍要求逐条核对scope/basis。
- 没有新增字段、门禁或依据NG文字的运行时判断：原表NG只证明该记录的标记，专业判断仍归作者与审查者。

**Q04：** 同一原始观测分到两个模块时（测温两行），两侧作者都要判断。Cross用`source_observation_groups`把它们关联起来，比较两侧判断和正文；路由副本不当作两台设备，也不自动判定矛盾。本回放中两侧都遗漏，当前会同时出现在两个模块的未判断列表和同一条同源链接里。

**未覆盖：** 模型实际会不会合并不同观测、审查者能否发现E-0071的数值矛盾和SPD的数量差异，需要真实运行。本回放只证明这些信息现在到得了哪些消费者。

## Q07 指令冲突、检索遗漏与Final对象身份

脚本：`uv run --with tokenizers python scripts/benchmarks/final_identity.py --tokenizer mimo.json`（Provider调用0）
结果：[2026-10-19-q07-final-identity.json](2026-10-19-q07-final-identity.json)

**发现（S06回退）：** S06把`scope`改为可选，理由是各消费者都能读E-*。核对实际Task→envelope→工具后，Final审计不成立：它的输入是章正文加`source_context`（`module_assessments`、`observation_delivery`），工具只有`submit_result`。Chief和Final修订有`source_refs`与读取工具，不受影响。

**改动：** `final_observation_context`增加`observation_identities`。它是运行时从本Run证据快照取的每个已判断E-ID的`subject`和`observed_at`，不由模型写，不含判断。三个调用点（Final初审、复审、Final修订）都传入workspace/run_id。Final审计提示补一句用途。没有加读取工具（Final职责是核对成稿与判断记录，不重新取证），也没有加门禁。

| 口径（S06合成记录，208条/316个E-ID） | S06前 | S06后 | 当前 |
| --- | --- | --- | --- |
| Final能确定对象的判断记录 | 208（模型写的scope） | **0** | 208（运行时派生） |
| Final每次请求的判断相关上下文 | 29,683 token | 16,697 | 16,697 + 6,018 = 22,715 |

**取舍：** 只给对象和时间，不给定位串或事实文字。定位串是表格坐标，Final打不开；事实文字已在正文和判断推理里。实测对比（MiMo分词器）：对象+定位串+事实覆盖全部E-ID为27.1k；只覆盖concern/undetermined为13.0k；对象+时间覆盖全部为6.0k。只覆盖concern会让no_concern记录在Final处仍无对象，无法核对“某对象正常”的表述，所以覆盖全部已判断E-ID。

**指令冲突核对（本边界未发现新冲突）：**
- Skill正文由`render_template_skill`包装为方法资料，优先级为合同 > 角色 > Skill，示例不是强制项。
- 知识上下文声明候选、非客户事实，生成规则与示例不是任务指令。
- 审查提示对blocking/advisory的说法与准备阶段一致。
- 21份生产Agent提示与19份模板镜像逐份比对，除空行外一致（Q01已补的evidence-auditor之外没有新漂移）。

**候选检索（本边界未发现遗漏）：** 每个小节按自身编号和标题检索，参考资料准备脚本给每个KU追加“检索关联主题：<小节编号 标题>”，所以每个小节一定命中自己的KU。三处已知缺陷的规则KU都落在观测所在小节：
- E-0071@2.4.3.1 → KU-2.4-11；
- SPD@2.3.3 → KU-2.3-03；
- 测温两行经2.2.2.1路由副本 → KU-2.2-04。

历史缺陷不是检索漏召。
- 2.4.4（末端抽查）的作者不会自动拿到KU-2.2-04的测温判据，靠同一观测的2.2.2.1路由与Cross链接补位。
- 同一份KU文件按模块取多个小节，超过14k字内联目标时整份退为“原文未展开”，作者需用`open_reference`。上传的KU库不在仓库内，2.4模块（14个KU，仅修订字段下限约7.1k字）是否越过该线无法离线测量；放入最后一次真实运行，看`context/module-*-knowledge.md`中各KU是内联还是未展开。

**未覆盖：** 角色选模、蒸馏遵循和语义召回效果需要Provider。
