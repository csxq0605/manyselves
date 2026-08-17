# Manyselves V2 提交前变更审计

日期：2026-08-11
分支：`cost-control-experiments`
提交前基线：`aa63771f9fd1346cb89e136065fc5d3ee2a99a29`

## 结论

用户指定的原始工作树包含 65 个 tracked 修改文件和 30 个 untracked 新文件。审计期间
仅新增两项必要变更：更新 `uv.lock` 以锁定新增的 `server` extra，以及新增本审计文件。
所有原始变更共同组成一个跨层 V2 实现；核心文件之间存在直接导入和合同依赖，不适合
拆成会暂时无法导入、无法测试的中间提交，因此按一个原子 implementation commit 提交。

未发现应排除的运行目录、Provider 凭据、DOCX 交付物、缓存、日志或临时文件。唯一新增
HTML 是 2026-08-09 成本审计材料，大小约 68 KiB，属于交接证据而非运行输出。

## 分组

### A. 并发、身份、Provider 与执行运行时

职责：task/attempt/result identity、MessageBus QoS、identity/project lease、journal、
Provider 歧义恢复、工作记忆/工具结果控制、execution profile、调度和使用量计量。

实现文件：

- `manyselves/core/loops/agent_loop.py`
- `manyselves/core/loops/bus.py`
- `manyselves/core/loops/manager.py`
- `manyselves/core/providers/__init__.py`
- `manyselves/core/providers/anthropic_provider.py`
- `manyselves/core/providers/base.py`
- `manyselves/core/providers/openai_provider.py`
- `manyselves/core/reporting/agent_runner.py`
- `manyselves/core/reporting/benchmark.py`
- `manyselves/core/reporting/distributed_runtime.py`
- `manyselves/core/reporting/execution_runtime.py`
- `manyselves/core/reporting/parallel_runtime.py`
- `manyselves/core/reporting/scheduling.py`
- `manyselves/core/runtime_errors.py`
- `manyselves/core/tools/registry.py`
- `manyselves/core/usage_ledger.py`
- `manyselves/interfaces/types.py`
- `scripts/benchmark_reporting_orchestration.py`

对应测试：

- `tests/core/loops/test_agent_loop_retry.py`
- `tests/reporting/test_agent_runner.py`
- `tests/reporting/test_benchmark.py`
- `tests/reporting/test_distributed_runtime.py`
- `tests/reporting/test_execution_runtime.py`
- `tests/reporting/test_parallel_runtime.py`
- `tests/reporting/test_scheduling.py`
- `tests/test_agent_loop.py`
- `tests/test_bus.py`
- `tests/test_manager.py`
- `tests/test_providers.py`
- `tests/test_runtime_errors.py`
- `tests/test_usage_ledger.py`

### B. 三波协作、37-leaf 逻辑任务与审查编排

职责：模块共享 Agent batch/session、逐叶 typed artifact/completion、Wave 1/2 barriers、
Wave 3 逐叶落盘、module/Cross-owner lanes、delta recheck 和 same-reviewer closure。

实现文件：

- `manyselves/core/reporting/agentic_models.py`
- `manyselves/core/reporting/capabilities.py`
- `manyselves/core/reporting/input_contracts.py`
- `manyselves/core/reporting/models.py`
- `manyselves/core/reporting/module_collaboration.py`
- `manyselves/core/reporting/review_lifecycle.py`
- `manyselves/core/reporting/service.py`
- `manyselves/core/reporting/store.py`
- `manyselves/core/reporting/submission_contracts.py`
- `manyselves/core/reporting/workflow.py`
- `manyselves/core/tools/reporting_collaboration_tools.py`
- `manyselves/core/tools/reporting_tool.py`
- `manyselves/templates/agents/main_agent.md`
- `manyselves/templates/reporting/agents/main-agent.md`
- `manyselves/utils/agent_labels.py`

对应测试：

- `tests/reporting/test_agent_workflow.py`
- `tests/reporting/test_capabilities.py`
- `tests/reporting/test_collaboration_tools.py`
- `tests/reporting/test_cost_control.py`
- `tests/reporting/test_models.py`
- `tests/reporting/test_module_collaboration.py`
- `tests/reporting/test_service_boundary.py`
- `tests/reporting/test_three_wave_workflow.py`

### C. 输入快照、确定性准备、检索、CAS 与交付

职责：queued-run 输入冻结、per-file worker/ordered reducer、Knowledge/Evidence index、
SourceLedger batch、trusted blob、CAS 视图、delivery/version provenance 和 render fence。

实现文件：

- `manyselves/core/artifacts/content_store.py`
- `manyselves/core/reporting/artifact_ports.py`
- `manyselves/core/reporting/delivery.py`
- `manyselves/core/reporting/input_snapshot.py`
- `manyselves/core/reporting/intake/manifest.py`
- `manyselves/core/reporting/preparation.py`
- `manyselves/core/reporting/rendering/contracts.py`
- `manyselves/core/reporting/rendering/pds_docx_renderer.py`
- `manyselves/core/reporting/research/knowledge_context.py`
- `manyselves/core/reporting/research/project_evidence.py`
- `manyselves/core/reporting/research/reference_library.py`
- `manyselves/core/reporting/source_ledger.py`
- `manyselves/core/reporting/versions.py`
- `manyselves/core/tools/document_tool.py`

对应测试：

- `tests/core/artifacts/test_content_store.py`
- `tests/reporting/rendering/test_pds_docx_renderer.py`
- `tests/reporting/research/test_knowledge_context.py`
- `tests/reporting/research/test_project_evidence.py`
- `tests/reporting/research/test_reference_library.py`
- `tests/reporting/test_artifact_ports.py`
- `tests/reporting/test_delivery.py`
- `tests/reporting/test_input_snapshot.py`
- `tests/reporting/test_preparation.py`
- `tests/reporting/test_source_ledger.py`
- `tests/reporting/test_versions.py`

### D. Headless、持久队列、Web 与生产端口

职责：Qt-independent bootstrap、durable job queue、API/worker 隔离、授权、事件 replay、
secret/egress/audit/backup 接口、可选对象存储和容器启动参考。

实现及配置文件：

- `manyselves/core/reporting/headless_runtime.py`
- `manyselves/core/reporting/job_runtime.py`
- `manyselves/core/reporting/production_runtime.py`
- `manyselves/core/reporting/web_runtime.py`
- `manyselves/headless_service.py`
- `deployment/headless/Dockerfile`
- `deployment/headless/README.md`
- `pyproject.toml`
- `uv.lock`

对应测试：

- `tests/reporting/test_headless_runtime.py`
- `tests/reporting/test_production_runtime.py`
- `tests/reporting/test_web_runtime.py`
- `tests/test_branding.py`

### E. 交接、完成审计与成本证据

- `docs/experimental-cost-control-handoff.md`
- `docs/experimental-cost-parallel-agent-orchestration-plan-v2.md`
- `docs/experimental-parallel-orchestration-completion-audit.md`
- `docs/experimental-v2-change-audit.md`
- `docs/audits/manyselves-holistic-cost-control-20260809.html`

## 锁文件审计

`pyproject.toml` 新增：

- `server = ["uvicorn>=0.34.0,<1.0.0"]`
- `manyselves-headless = "manyselves.headless_service:main"`

原 `uv.lock` 未包含 `server` extra。已使用项目 `uv` 重新解析并加入 `uvicorn`、其依赖、
wheel/sdist hash 和 `provides-extras = ["dev", "server"]`；`uv lock --check` 通过。

## 提交门槛

- `QT_QPA_PLATFORM=offscreen ... pytest -q -m 'not integration'`
- `python -m compileall -q manyselves`
- `git diff --check`
- `uv lock --check`
- 提交后工作树必须为空，并以新 HEAD 重新执行验证；真实 Provider run 只能引用该 HEAD。
