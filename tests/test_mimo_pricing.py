import pytest

from manyselves.core.mimo_pricing import (
    calculate_mimo_v25_pro_cost,
    calculate_mimo_v25_pro_run_cost,
    format_mimo_v25_pro_cost,
)


def test_mimo_pricing_compares_lite_standard_pro_and_api() -> None:
    report = calculate_mimo_v25_pro_cost(
        [{
            "timestamp": "2026-08-13T11:06:27+08:00",
            "model": "mimo-v2.5-pro",
            "cached_input_tokens": 1_000_000,
            "uncached_input_tokens": 2_000_000,
            "output_tokens": 500_000,
        }]
    )
    assert report is not None
    assert report["token_plan"]["credits_used"] == 902_500_000
    plans = report["token_plan"]["plans"]
    assert plans["lite"]["monthly_allocated_cost_cny"] == pytest.approx(8.5847560976)
    assert plans["standard"]["monthly_allocated_cost_cny"] == pytest.approx(8.1225)
    assert plans["pro"]["monthly_allocated_cost_cny"] == pytest.approx(7.81375)
    assert plans["pro"]["annual_allocated_cost_cny"] == pytest.approx(6.8761)
    assert report["api"]["cost_cny"] == pytest.approx(9.025)
    rendered = format_mimo_v25_pro_cost(report)
    assert all(label in rendered for label in (
        "Token Plan Lite", "Token Plan Standard", "Token Plan Pro", "API 按量计费"
    ))


def test_mimo_pricing_applies_off_peak_credit_multiplier_per_attempt() -> None:
    report = calculate_mimo_v25_pro_cost(
        [
            {"timestamp": "2026-08-13T01:00:00+08:00", "model": "mimo-v2.5-pro", "uncached_input_tokens": 1_000_000},
            {"timestamp": "2026-08-13T12:00:00+08:00", "model": "mimo-v2.5-pro", "uncached_input_tokens": 1_000_000},
        ]
    )
    assert report is not None
    assert report["token_plan"]["undiscounted_credits"] == 600_000_000
    assert report["token_plan"]["credits_used"] == 540_000_000
    assert report["api"]["cost_cny"] == 6


def test_mimo_pricing_reads_run_ledger_and_skips_other_models(tmp_path) -> None:
    ledger = tmp_path / ".manyselves" / "usage" / "report-cost.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "\n".join((
            '{"timestamp":"2026-08-13T12:00:00+08:00","model":"mimo-v2.5-pro","input_tokens":150,"cached_input_tokens":40,"cache_write_input_tokens":10,"output_tokens":20}',
            '{"timestamp":"2026-08-13T12:00:00+08:00","model":"other-model","input_tokens":999,"output_tokens":999}',
            "",
        )),
        encoding="utf-8",
    )
    report = calculate_mimo_v25_pro_run_cost(tmp_path, "report-cost")
    assert report is not None
    assert report["tokens"] == {"cached_input": 40, "uncached_input": 110, "output": 20}
    assert report["priced_attempts"] == 1
    assert report["unpriced_attempts"] == 1
