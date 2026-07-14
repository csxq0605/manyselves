import pytest

from pds_report.app.request_parser import RequestValidationError, parse_report_request


def test_deep_reasoning_is_execution_requirement_not_task() -> None:
    request = parse_report_request("写作配电报告，要求深度思考，先做2.4")

    assert request.task == "write_report"
    assert request.target_modules == ["2.4"]
    assert request.execution_requirements == ["deep_reasoning"]


def test_parser_keeps_multiple_explicit_modules_in_order() -> None:
    request = parse_report_request("生成配电报告，先分析2.2和2.4")

    assert request.target_modules == ["2.2", "2.4"]


def test_parser_records_pending_draft_permission() -> None:
    request = parse_report_request("生成配电报告，资料不足时允许待核实稿")

    assert request.allow_pending_evidence is True


def test_parser_records_skipped_module_as_temporary_constraint() -> None:
    request = parse_report_request("生成配电报告，处理2.2和2.4，但跳过2.2")

    assert request.target_modules == ["2.4"]
    assert request.temporary_constraints == ["skip:2.2"]


@pytest.mark.parametrize("message", ["", "   ", "今天天气怎么样"])
def test_parser_rejects_blank_or_non_report_request(message: str) -> None:
    with pytest.raises(RequestValidationError):
        parse_report_request(message)

