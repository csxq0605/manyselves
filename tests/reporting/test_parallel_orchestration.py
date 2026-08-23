from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY


def test_taxonomy_subsections_remain_document_parts_not_runtime_tasks() -> None:
    """All terminal headings remain available to module rendering/coverage."""

    assert set(REPORT_TAXONOMY) == {"2.1", "2.2", "2.3", "2.4", "2.5"}
    assert sum(len(module.submodules) for module in REPORT_TAXONOMY.values()) == 37
    assert all(module.submodules for module in REPORT_TAXONOMY.values())
