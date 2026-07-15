import pytest

from autoreport.core.reporting.taxonomy import REPORT_TAXONOMY, resolve_submodule


def test_taxonomy_contains_fixed_module_24_submodules() -> None:
    module = REPORT_TAXONOMY["2.4"]

    assert "2.4.2.2" in module.submodules
    assert module.submodules["2.4.2.2"].title == "等电位连接与接地问题"
    assert module.submodules["2.4.4"].title == "末端配电抽查情况"


def test_resolve_submodule_rejects_unknown_identifier() -> None:
    with pytest.raises(ValueError, match="unknown report submodule"):
        resolve_submodule("2.4.99")
