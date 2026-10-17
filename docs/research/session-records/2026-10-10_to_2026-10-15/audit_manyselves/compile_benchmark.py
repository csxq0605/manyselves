"""Local-only paired compiler benchmark. No Provider or customer input."""
import json
import subprocess
import time
import types
from pathlib import Path

from manyselves.capabilities.distribution_reporting import load_distribution_reporting_capability
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_definitions import register_cross_owner_pipeline_specializations
from manyselves.kernel.definitions import DefinitionKind
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler

source = subprocess.run(["git", "show", "a635c3c:manyselves/kernel/workflow/compiler.py"],
                        capture_output=True, check=True, encoding="utf-8").stdout
old = types.ModuleType("manyselves.kernel.workflow.compiler_baseline")
old.__package__ = "manyselves.kernel.workflow"
exec(compile(source, "compiler_baseline.py", "exec"), old.__dict__)
_, registry = load_distribution_reporting_capability()
register_cross_owner_pipeline_specializations(registry)
rows = []
for repeat in range(3):
    for name in ("distribution-cross-joint-review", "distribution-cross-owner-cohort"):
        pair = {}
        arms = [("before", old.WorkflowCompiler), ("after", WorkflowCompiler)]
        if repeat % 2:
            arms.reverse()
        for arm, compiler in arms:
            start, cpu = time.perf_counter(), time.process_time()
            plan = compiler(build_builtin_executor_registry()).compile(
                registry.require(DefinitionKind.WORKFLOW, name), registry)
            row = dict(repeat=repeat, workflow=name, arm=arm,
                       wall_seconds=time.perf_counter()-start, cpu_seconds=time.process_time()-cpu)
            rows.append(row)
            pair[arm] = plan.model_dump(mode="json")
            print(json.dumps(row), flush=True)
        assert pair["before"] == pair["after"]
        Path(__file__).with_name("compile_pair_metrics.json").write_text(
            json.dumps({"scope": "Local compilation only; equal frozen plans; no cProfile, Provider or customer data", "rows": rows}, indent=2),
            encoding="utf-8")
