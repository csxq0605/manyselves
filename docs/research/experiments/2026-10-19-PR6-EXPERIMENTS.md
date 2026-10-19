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
