"""Shared test setup."""

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    activate_report_taxonomy,
    example_report_taxonomy,
    reset_report_taxonomy,
)


@pytest.fixture(autouse=True)
def example_report_taxonomy_bound(request):
    """Tests run with the example section tree unless they ask for none.

    A run never has a default tree: it binds the one its 评估信息汇总表 lists.
    """

    if request.node.get_closest_marker("unbound_taxonomy"):
        yield
        return
    token = activate_report_taxonomy(example_report_taxonomy())
    try:
        yield
    finally:
        reset_report_taxonomy(token)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "unbound_taxonomy: run without the example section tree bound"
    )
