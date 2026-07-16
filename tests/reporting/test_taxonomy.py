import pytest

from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY, resolve_submodule


def test_taxonomy_contains_fixed_module_24_submodules() -> None:
    module = REPORT_TAXONOMY["2.4"]

    assert "2.4.2.2" in module.submodules
    assert module.submodules["2.4.2.2"].title == "等电位连接与接地问题"
    assert module.submodules["2.4.4"].title == "末端配电抽查情况"


def test_taxonomy_matches_v2_handoff_for_all_five_modules() -> None:
    assert {module_id: module.title for module_id, module in REPORT_TAXONOMY.items()} == {
        "2.1": "电力系统架构问题",
        "2.2": "环境工况风险",
        "2.3": "针对故障的保护",
        "2.4": "配电设备/元件风险",
        "2.5": "运维管理与风险管控",
    }
    assert REPORT_TAXONOMY["2.1"].submodules["2.1.1"].title == "电力系统负荷分配与过载风险"
    assert REPORT_TAXONOMY["2.2"].submodules["2.2.1.1"].title == "谐波风险情况"
    assert REPORT_TAXONOMY["2.3"].submodules["2.3.2"].title == "零序/漏电的防范"
    assert REPORT_TAXONOMY["2.5"].submodules["2.5.1"].title == "SOP/EOP"
    assert REPORT_TAXONOMY["2.5"].submodules["2.5.3.3"].title == "配电设备维保覆盖"


def test_resolve_submodule_rejects_unknown_identifier() -> None:
    with pytest.raises(ValueError, match="unknown report submodule"):
        resolve_submodule("2.4.99")
