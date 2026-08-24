from pathlib import Path
from uuid import UUID

import pytest

from manyselves.application.workflow_projection import WorkflowProjectionFacade
from manyselves.kernel.definitions import CapabilityCatalog
from manyselves.runtime.capability_binding import (
    CapabilityRunNotFoundError,
    RuntimeBindingCatalog,
)


def _catalog(tmp_path: Path) -> CapabilityCatalog:
    root = tmp_path / "synthetic"
    for location in (
        "agents",
        "contracts",
        "gates",
        "recovery",
        "tasks",
        "tools",
        "workflows",
    ):
        (root / location).mkdir(parents=True, exist_ok=True)
    (root / "capability.yaml").write_text(
        """id: synthetic-capability
version: 1.0.0
description: Synthetic application binding fixture
agents: ./agents
workflows: ./workflows
tasks: ./tasks
contracts: ./contracts
tools: ./tools
gates: ./gates
recovery: ./recovery
entrypoints:
- synthetic-entry
runtime: tests.application.test_workflow_runtime_binding:build_fixture_binding
""",
        encoding="utf-8",
    )
    (root / "contracts" / "payload.yaml").write_text(
        """id: synthetic-payload
version: 1.0.0
description: Synthetic object
adapter: json_schema
schema:
  type: object
""",
        encoding="utf-8",
    )
    (root / "workflows" / "entry.yaml").write_text(
        """id: synthetic-entry
version: 1.0.0
description: Synthetic public workflow
input_contract: synthetic-payload
output_contract: synthetic-payload
actions: []
""",
        encoding="utf-8",
    )
    return CapabilityCatalog.discover([root])


class _FixtureBinding:
    capability_id = "synthetic-capability"

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.inputs: list[dict] = []

    async def start(self, command_id: UUID, workflow_id: str, values: dict) -> dict:
        self.run_id = f"synthetic-run-{command_id.hex}"
        self.inputs.append(values)
        return {"run_id": self.run_id, "task_id": None}

    async def start_detached(
        self,
        command_id: UUID,
        workflow_id: str,
        values: dict,
    ) -> dict:
        return await self.start(command_id, workflow_id, values)

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: dict,
    ) -> dict:
        self._require_run(run_id)
        self.inputs.append(values)
        return {"run_id": run_id, "task_id": None}

    def get_run(self, run_id: str) -> dict:
        self._require_run(run_id)
        return {
            "run": {
                "run_id": run_id,
                "capability_id": self.capability_id,
                "workflow_id": "synthetic-entry",
                "status": "completed",
                "active": False,
                "task_id": None,
            },
            "state": {"input_count": len(self.inputs)},
            "waiting_input": [],
        }

    def get_outputs(self, run_id: str) -> dict:
        self._require_run(run_id)
        return {
            "run_id": run_id,
            "outputs": [{"id": "result", "kind": "value", "value": self.inputs[-1]}],
        }

    def get_cost(self, run_id: str) -> dict:
        self._require_run(run_id)
        return {"run_id": run_id, "usage": {"totals": {"total_tokens": 0}}}

    def _require_run(self, run_id: str) -> None:
        if run_id != self.run_id:
            raise CapabilityRunNotFoundError(run_id)


def build_fixture_binding(**_kwargs) -> _FixtureBinding:
    return _FixtureBinding()


@pytest.mark.asyncio
async def test_facade_dispatches_through_definition_owned_runtime_binding(
    tmp_path: Path,
) -> None:
    binding = _FixtureBinding()
    bindings = RuntimeBindingCatalog()
    bindings.register(binding)
    facade = WorkflowProjectionFacade(
        tmp_path,
        runtime_services=None,
        catalog=_catalog(tmp_path),
        runtime_bindings=bindings,
    )
    command_id = UUID("40000000-0000-4000-8000-000000000001")

    assert facade.list_capabilities()[0]["id"] == "synthetic-capability"
    assert facade.list_workflows() == [
        {
            "id": "synthetic-entry",
            "capability_id": "synthetic-capability",
            "version": "1.0.0",
            "description": "Synthetic public workflow",
            "input_contract": "synthetic-payload",
            "output_contract": "synthetic-payload",
            "runnable": True,
        }
    ]
    accepted = await facade.start(command_id, "synthetic-entry", {"value": 3})
    run_id = accepted["run_id"]

    assert accepted["capability_id"] == "synthetic-capability"
    assert facade.get_run(run_id)["run"]["workflow_id"] == "synthetic-entry"
    assert facade.get_outputs(run_id)["outputs"][0]["value"] == {"value": 3}
    assert facade.get_cost(run_id)["usage"]["totals"]["total_tokens"] == 0

    resumed = await facade.provide_input(
        command_id,
        run_id,
        input_id="input-1",
        values={"value": 4},
    )
    assert resumed["run_id"] == run_id
    assert binding.inputs == [{"value": 3}, {"value": 4}]


def test_workflow_without_a_runtime_binding_is_not_projected_as_runnable(
    tmp_path: Path,
) -> None:
    facade = WorkflowProjectionFacade(
        tmp_path,
        runtime_services=None,
        catalog=_catalog(tmp_path),
        runtime_bindings=RuntimeBindingCatalog(),
    )

    assert facade.list_workflows()[0]["runnable"] is False
