# Manyselves 实验性整体成本控制计划

> 文件定位：这是提交 `5f5a5e3` 的 Phase A–D 实现与历史验收说明，里面的起点、
> 基线 SHA 和 `333 passed` 是当时记录，不是当前分支现场。当前交接入口见
> `docs/experimental-cost-control-handoff.md`；后续唯一执行计划见
> `docs/experimental-cost-parallel-agent-orchestration-plan-v2.md`。

## 1. 基线与边界

- 基线分支：`test-improvements@b8c455e3dc7d65d99747523ed86db53e315fafcc`。
- 新分支起始快照：`cost-control-experiments@0aff4d7036da8e3abd792c26ed5ba5b93d43d9e8`，包含原分支的 13 个未提交改动；后续实现提交在此快照之上演进。
- 最近一次成功运行 `report-f91bc1714d` 只作为量化基线：201 次 Provider 调用、1,677,389 Token；它早于当前完整快照，不能证明本分支已经完成真实端到端验证。
- 实施前定向基线为 75 个测试通过；最终只报告当前快照的一次合并定向结果，不累加重叠测试。
- 原始 `manyselves`、`test`、`manyselves-test-improvements`、`test-improvements`、成功基线和已有运行目录均不修改、不清理、不重启。

## 2. 必须保持的语义

1. 专家继续负责证据选择、正文、Claim 和 `E-*` 绑定。
2. 原审查员继续拥有 Finding 和 ResolutionVerdict；机器检查、Main、Chief 或其他审查角色不得代替其关闭 Finding。
3. `submit_result` 的 typed payload 和 checkpoint 是完成边界；普通文本不是结果。
4. 预算不得在尚未完成 typed 提交的 Provider turn 内硬中断。
5. 同一 run 暂停后必须从 checkpoint 恢复，不得重跑已完成阶段。
6. 不以 lint 代替语义审查，不以统一降低 `max_tokens` 制造额外 continuation。
7. 不并发五条完整 `_module_pipeline()`；协作并行只发生在有确定性 barrier 的三波协议内。
8. 旧 delivery/report-version/ArtifactGateway 引用保持可读；首版 retention 不自动删除历史内容。

## 3. 分阶段实施

### Phase A：请求固定成本与轮次

1. 建立 fake-provider 请求成本基线，按调用记录：
   - message 字符数；
   - tool schema 字符数；
   - tool 名称；
   - Provider 调用数；
   - task、agent、stage 和稳定 call id。
2. 以 `submit_result.input_schema` 作为唯一机器合同：
   - Prompt 只保留 output kind、合同版本和语义约束；
   - 不再内嵌 `valid_example`、完整字段表和嵌套类型全文。
3. 生成任务专属 `write_result_part` schema：
   - `part_id` 使用当前任务允许值的枚举；
   - 模块正文要求 `evidence_ids`；
   - 非模块正文不暴露 `evidence_ids`。
4. 新增兼容性的 `write_result_parts`：
   - 批内先全部验证，验证失败时零写入；
   - 不把磁盘 I/O 中途失败宣称为跨文件事务原子；
   - 模块每批最多 4 个小节；
   - Chief/模板任务每批最多 8 个部分；
   - 保留单条工具用于旧运行恢复。
5. 工具成功落盘后，在后续 Provider 历史中用 `part_id`、artifact ref、字符数和 SHA-256 替换长正文；磁盘正文和当前未配对 tool call 不改写。
6. 收窄 module revision 和 chief revision 的 allowed tools，只保留任务确实需要的能力。

验收：

- task message 不再含完整 submission schema/example，tool schema 仍可严格验证 typed payload；
- fake-provider 场景无新增 correction，最终正文和 typed payload 不变；
- revision/chief 工具 schema 字符数相对同 fixture 基线下降至少 30%；
- 批量 revision 的典型 fixture 在不超过 3 次 Provider 请求内完成；
- 已持久化长正文不在下一轮 Provider message 中再次出现。

### Phase B：内容寻址存储与生命周期去重

1. 新增项目级 SHA-256 内容库：
   - canonical blob：`Work/content/sha256/<prefix>/<digest>`；
   - 原子 ingest；
   - blob 记录 digest 和 size；原始后缀、media type 继续由 delivery/version/photo 等上游 manifest 与兼容视图保存；
   - 支持 verify 和兼容视图。
2. delivery manifest v2：
   - receipt 字段和可打开路径保持兼容；
   - delivery 目录使用指向 blob 的视图，不再复制 DOCX、模块 Markdown 和索引；
   - v1 实体快照继续可读。
3. report-version storage v2：
   - version 元数据不可变；
   - artifact/session summary 指向共享 blob；
   - loader 将 manifest `version_id`、compatibility view 与实际 version 目录强绑定，拒绝跨 version/view 路径替换；
   - v1 version 不重写、不迁移。
4. 模板和图片在首次进入项目时 ingest：
   - run 级路径保留为兼容视图；
   - version/delivery 不再新增普通文件副本。
5. 停止新 legacy 双写：
   - ArtifactGateway 已成功持久化后不再额外写 legacy checkpoint/tool-result；
   - 保留旧路径读取兼容，不删除旧文件。
6. conversation trace 改为小 manifest 加压缩内容引用；GUI conversation store 不动。
7. retention 首版只生成 CAS 范围的 `storage-usage.json` 和 `retention-plan.json`：
   - 计算引用、复用和宽限期候选；
   - 不执行删除；
   - active/pinned 排除和 quarantine 是未来执行器必须实现的安全门，本版不宣称已经执行。

验收：

- 同一 digest 只有一个 canonical blob；
- delivery/version 的相同 DOCX、模板、图片和模块内容不产生普通文件副本；
- v1 成功运行仍能加载、验证和恢复；
- v2 receipt、version、局部修订和输出校验 hash 一致；
- 新运行不再增加 legacy tool-result/context-checkpoint payload；
- 报告 CAS canonical/logical/reused bytes；全工作区 allocated bytes 审计不在本版完成范围。

### Phase C：审查前置、delta recheck 与阶段成本决策

1. 新增独立 `review_preflight.py`，只检查可确定判定的规则：
   - 复用上游固定结构/Pydantic 校验；
   - 新增来源注册、内部控制标记、目标范围、正文可读性和 content hash 检查；
   - 不把语义质量判断伪装成机器 Finding。
2. preflight 失败进入 machine-correction contract，在首次付费 review 前返回原作者；其结果不得生成 approved/verdict。
3. module recheck 只发送：
   - changed statements；
   - 当前 Finding 所需的旧语义；
   - 相关 `E-*` excerpt；
   - 未改部分的 content hash。
   - 因 typed recheck 会重置 Provider 会话，仍重发同一份有上限的 Knowledge slice；不重发未改正文和无关证据。
4. 扩展 UsageLedger：
   - uncached/cached input、output、duration；
   - OpenAI/Anthropic adapter 规范化 cache read/cache write，并在流式响应可用时读取 Provider usage；不可用时连同 tool-call arguments 做保守估算，避免把正文输出记为零；
   - message/tool schema 字符；
   - stage、task、agent、provider；
   - 价格字段保留版本化接口；没有配置价格表时明确记录 `unconfigured`，不虚构估算金额；
   - 与 context manifest 通过 call id 关联，并区分 adapter 前逻辑请求与 adapter 后 wire payload 指纹，重复成本归因以实际发送形态为准。
5. 将 ReportingRunBudget 改为阶段边界控制器：
   - `observe`：仅计量；
   - `warn`：完成 checkpoint 后提示；
   - `pause_at_boundary`：先持久化待评估边界，再完成 checkpoint，并写入结构化 pending state；
   - 显式 same-run resume 解除一个成本窗口；本版不冒充独立 Main `submit_result` 决策合同；
   - 默认不恢复旧的请求内硬停。
6. Chief 付费阶段使用独立 completion：
   - 候选稿、输入合同、TaskEnvelope、Claim/Cross/模块审查和当前请求语义均以 current-run ref 与 SHA-256 绑定；
   - full/revision 恢复只接受 identity、hash 和当前上下文同时吻合的 completion；
   - 陈旧、错 run/agent 或补充约束已变化的裸候选稿不得为了“省一次调用”被继续使用。

验收：

- 明确的结构错误在首次付费审查前被退回，且没有伪造 reviewer finding；
- 给 fixture 增加超大未修改小节时，recheck 输入大小基本不变；
- 每个阶段能独立汇总调用、uncached/cached Token、重复字符和延迟；只有配置版本化价格表后才验收估算金额；
- boundary pause 后同 run 恢复，不重复 Provider 工作，不丢 finding/verdict。

### Phase D：三波协作

1. Wave 1：37 个固定叶子各自执行可调度、可恢复的 research/interface discovery，只能提交 `submodule_discovery_submission`。
   - 每个任务仅接收当前叶子的 Evidence/coverage/Knowledge 增量；整模块 Knowledge、manifest 与 peer taxonomy 使用同模块共享引用。
   - 首次工具 Schema 给出当前 module/submodule 的有效样例，不再删除样例或退回通用 `2.1` 身份。
2. Barrier 1：
   - 模块内 reducer 先校验全部叶子 discovery，再形成五份模块 barrier；
   - 建立 interface index 和 target-leaf inbox；
   - 拒绝无效目标、陈旧证据和缺失请求 id。
3. Wave 2：
   - 只调用存在入站请求的目标叶子；
   - 每个非空 leaf inbox 独立提交 `submodule_interface_response_submission`，首次和纠错样例均覆盖当前全部 request ids。
4. Barrier 2：
   - 每个请求必须 answered 或明确 unresolved；
   - 构建 37 份 `SubmoduleCollaborationBundle`，再确定性归并模块 bundle。
   - Wave 1/2 结果仅在 Barrier 通过后提升为 canonical artifact；无效候选移入可恢复 rejected 区，显式 same-run resume 可重新分派。
5. Wave 3：37 个叶子作者并行写作，各自消费自己的 bundle；模块内 reducer 完整归并后继续模块审查、Cross、Chief 和 final gate。
   - 每个叶子只内联自己的 Knowledge 小节、bundle 与核心写作方法；完整模块 Knowledge/Skill 作为同胞共享引用按需读取。
   - 每份 leaf completion 与 specialist subject 记录 bundle、输入和当前补充约束的 fingerprint；上下文变化时不得静默复用旧正文。
   - 部分模块运行显式排除 `query_peer/reply_peer`，不等待未调度同伴。

验收：

- 恰有 37 份有效 leaf discovery artifact 和五份模块 reducer barrier；
- 每个 interface request 都有 response/unresolved disposition；
- Wave 2 Provider 调用模块数不超过实际有入站请求的模块数；
- 每个作者的 envelope 含对应 bundle；
- 不存在五个完整 `_module_pipeline()` 的 `asyncio.gather()`。

## 4. 验证策略

每个 Phase 先运行对应定向测试，再运行相邻 reporting/core 回归；不在实现初期跑全量测试。最终至少完成：

- `git diff --check` 和 `compileall`；
- Prompt、AgentRunner、tool schema、AgentLoop、delivery、version、artifact、review lifecycle、workflow 的定向测试；
- 纯 fake/unit 的边界崩溃恢复、Barrier 无效候选重试和 v1/v2 兼容场景；
- 一个小型本地存储 fixture 的去重与 v1/v2 兼容审计；
- 与 `report-f91bc1714d` 的真实调用/Token 降幅对比留给单独 Provider 运行；本次只验证成本机制和可观测性，不提前声明净下降。

真实 Provider 全流程属于单独的最终运行验证：只有当前代码产生 completed run、五模块/Cross/final 完整、可读 DOCX 和匹配 receipt，才可称为真实跑通。

## 5. 本分支交付边界

本分支已实现 Phase A-D 的代码机制和定向 fake/unit 验证。仍有四项必须与本次实现分开陈述：

- 冻结快照的合并定向回归：`333 passed`；`compileall` 与 `git diff --check` 通过。该数字不包含真实 Provider 或全量测试。

1. 尚未运行真实 Provider，因此不能声明相对 `report-f91bc1714d` 的实际调用数、Token 或金额已经下降。
2. 尚未配置版本化价格表；usage 明确标记 `pricing_status=unconfigured`，不输出虚构金额。
3. retention 仍是 dry-run 规划器；active/pinned/quarantine 的删除执行器和全工作区 allocated-bytes 审计尚未实现。
4. 尚未跑全量测试、当前代码完整 E2E、DOCX 可读性检查和 delivery receipt 验证；这些属于用户批准后的独立验证。
