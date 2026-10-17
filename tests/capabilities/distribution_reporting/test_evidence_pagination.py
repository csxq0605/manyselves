"""Paginated fact recall must distinguish a search miss from missing data."""


import pytest

from manyselves.capabilities.distribution_reporting import load_distribution_reporting_capability
from manyselves.capabilities.distribution_reporting.runtime.research.evidence_memory import (
    EvidenceResearchMemory,
)
from manyselves.capabilities.distribution_reporting.runtime.research_tools import (
    SearchProjectEvidenceTool,
)
from manyselves.kernel.definitions import DefinitionKind
from manyselves.runtime.tools.registry import ToolRegistry
from tests.capabilities.distribution_reporting.test_project_fact_packet import _facts, _write_facts


@pytest.mark.asyncio
async def test_empty_query_enumerates_scope_through_last_abnormal_row(tmp_path):
    _write_facts(tmp_path, _facts())
    tool = SearchProjectEvidenceTool(tmp_path, run_id="facts")
    first = await tool(query="", submodule_id="2.4.3.1", limit=12)
    assert first["retrieval_mode"] == "enumerate"
    assert first["total_matches"] == 14
    assert first["has_more"] is True
    assert first["next_offset"] == 12
    last = await tool(query="", submodule_id="2.4.3.1", offset=first["next_offset"], limit=12)
    assert last["next_offset"] is None and last["has_more"] is False
    assert [hit["id"] for hit in first["hits"] + last["hits"]] == [item.id for item in _facts()]
    assert last["hits"][-1]["value"] == 11.3


@pytest.mark.asyncio
async def test_cached_pages_keep_scope_offset_limit_and_total_across_restart(tmp_path):
    _write_facts(tmp_path, _facts())
    memory = EvidenceResearchMemory(tmp_path, "facts", "2.4")
    tool = SearchProjectEvidenceTool(tmp_path, run_id="facts", evidence_memory=memory)
    first = await tool(query="实测", limit=12)
    small = await tool(query="实测", limit=1)
    assert len(small["hits"]) == 1
    second = await tool(query="实测", limit=12, offset=12)
    assert len(second["hits"]) == 2
    scoped_out = await tool(query="实测", module_id="2.3")
    assert scoped_out["hits"] == []
    resumed = SearchProjectEvidenceTool(
        tmp_path, run_id="facts",
        evidence_memory=EvidenceResearchMemory(tmp_path, "facts", "2.4"),
        research_guard=lambda: pytest.fail("a cached page must not re-enter research"),
    )
    replayed = await resumed(query="实测", limit=12, offset=12)
    assert replayed["cache_hit"] is True
    assert replayed["hits"] == second["hits"]
    assert replayed["total_matches"] == first["total_matches"] == 14


@pytest.mark.asyncio
async def test_old_top_k_cache_cannot_claim_the_full_match_count(tmp_path):
    _write_facts(tmp_path, _facts())
    memory = EvidenceResearchMemory(tmp_path, "facts", "2.4")
    memory.remember(_facts()[:3], query="实测", applied_limit=3)
    result = await SearchProjectEvidenceTool(
        tmp_path, run_id="facts", evidence_memory=memory,
    )(query="实测", limit=3)
    assert result["cache_hit"] is False
    assert result["total_matches"] == 14
    assert result["next_offset"] == 3


@pytest.mark.asyncio
async def test_no_match_and_unavailable_snapshot_are_different_results(tmp_path):
    absent = await SearchProjectEvidenceTool(tmp_path, run_id="absent")(query="未知设备")
    _write_facts(tmp_path, _facts())
    no_match = await SearchProjectEvidenceTool(tmp_path, run_id="facts")(query="未知设备")
    assert absent["status"] == "snapshot_unavailable"
    assert no_match["status"] == "no_matches"
    assert "does not establish" in no_match["guidance"]
    assert absent["hits"] == no_match["hits"] == []


@pytest.mark.asyncio
async def test_snapshot_becoming_available_does_not_keep_an_empty_index(tmp_path):
    tool = SearchProjectEvidenceTool(tmp_path, run_id="facts")
    assert (await tool(query=""))["status"] == "snapshot_unavailable"
    _write_facts(tmp_path, _facts())
    result = await tool(query="")
    assert result["total_matches"] == 14
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_past_end_is_not_reported_as_no_matching_evidence(tmp_path):
    _write_facts(tmp_path, _facts())
    result = await SearchProjectEvidenceTool(tmp_path, run_id="facts")(query="实测", offset=100)
    assert result["status"] == "ok"
    assert result["total_matches"] == 14 and result["hits"] == []
    assert result["next_offset"] is None


def test_pagination_is_exposed_by_file_contract_and_provider_tool_schema(tmp_path):
    _capability, registry = load_distribution_reporting_capability()
    definition = registry.require(DefinitionKind.TOOL, "search_project_evidence")
    contract = registry.require(DefinitionKind.CONTRACT, definition.input_contract)
    expected = {"query", "limit", "offset", "module_id", "submodule_id"}
    assert expected <= set(contract.schema_["properties"])
    tools = ToolRegistry()
    tools.register(SearchProjectEvidenceTool(tmp_path, run_id="facts"))
    assert expected <= set(tools.get_definitions()[0]["input_schema"]["properties"])


@pytest.mark.asyncio
async def test_zero_numeric_value_is_searchable_when_not_repeated_in_fact(tmp_path):
    items = _facts(1)
    items[0] = items[0].model_copy(update={
        "id": "E-only", "subject": "meter", "fact": "measured", "value": 0,
        "source": items[0].source.model_copy(update={"cell": "J1"}),
    })
    _write_facts(tmp_path, items)
    result = await SearchProjectEvidenceTool(tmp_path, run_id="facts")(query="0")
    assert [hit["id"] for hit in result["hits"]] == ["E-only"]
