"""Reproducible MiMo-V2.5-Pro Token Plan and API cost conversion."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


PRICING_VERSION = "xiaomi-mimo-2026-08-13-verified"
TOKEN_PLAN_SOURCE = "https://mimo.mi.com/docs/zh-CN/price/tokenplan/subscription"
API_PRICING_SOURCE = "https://mimo.mi.com/models/zh-CN/mimo-v2.5-pro"

_MODEL = "mimo-v2.5-pro"
_BEIJING = ZoneInfo("Asia/Shanghai")
_OFF_PEAK_MULTIPLIER = Decimal("0.8")
_CREDITS_PER_TOKEN = {
    "cached_input": Decimal("2.5"),
    "uncached_input": Decimal("300"),
    "output": Decimal("600"),
}
_TOKEN_PLANS = {
    "lite": {
        "monthly_fee_cny": Decimal("39"),
        "monthly_credits": 4_100_000_000,
        "annual_fee_cny": Decimal("411.84"),
        "annual_credits": 49_200_000_000,
    },
    "standard": {
        "monthly_fee_cny": Decimal("99"),
        "monthly_credits": 11_000_000_000,
        "annual_fee_cny": Decimal("1045.44"),
        "annual_credits": 132_000_000_000,
    },
    "pro": {
        "monthly_fee_cny": Decimal("329"),
        "monthly_credits": 38_000_000_000,
        "annual_fee_cny": Decimal("3474.24"),
        "annual_credits": 456_000_000_000,
    },
}
_API_CNY_PER_MILLION = {
    "cached_input": Decimal("0.025"),
    "uncached_input": Decimal("3"),
    "output": Decimal("6"),
}


def _integer(row: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(row.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _is_supported_model(row: dict[str, Any]) -> bool:
    model = str(row.get("resolved_model") or row.get("model") or "").strip().casefold()
    return model == _MODEL or model.rsplit("/", 1)[-1] == _MODEL


def _provider_request_sent(row: dict[str, Any]) -> bool:
    value = row.get("provider_request_sent", True)
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _token_counts(row: dict[str, Any]) -> tuple[int, int, int]:
    cached = _integer(row, "cached_input_tokens")
    if "uncached_input_tokens" in row:
        uncached = _integer(row, "uncached_input_tokens")
    else:
        uncached = max(
            0,
            _integer(row, "input_tokens")
            - cached
            - _integer(row, "cache_write_input_tokens"),
        )
    # MiMo exposes hit/miss input prices only; cache creation is a miss.
    uncached += _integer(row, "cache_write_input_tokens")
    return cached, uncached, _integer(row, "output_tokens")


def _token_plan_multiplier(row: dict[str, Any]) -> tuple[Decimal, bool]:
    raw = str(row.get("timestamp") or "").strip()
    if not raw:
        return Decimal("1"), False
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return Decimal("1"), False
    if timestamp.tzinfo is None:
        return Decimal("1"), False
    hour = timestamp.astimezone(_BEIJING).hour
    return (_OFF_PEAK_MULTIPLIER if 0 <= hour < 8 else Decimal("1")), True


def calculate_mimo_v25_pro_cost(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Price sent model rows under three Token Plans and domestic API rates."""

    token_totals = {"cached_input": 0, "uncached_input": 0, "output": 0}
    api_cost = Decimal("0")
    credits = Decimal("0")
    undiscounted_credits = Decimal("0")
    priced_attempts = 0
    unpriced_attempts = 0
    unknown_time_attempts = 0

    for row in rows:
        if not _provider_request_sent(row):
            continue
        if not _is_supported_model(row):
            unpriced_attempts += 1
            continue
        priced_attempts += 1
        cached, uncached, output = _token_counts(row)
        counts = {
            "cached_input": cached,
            "uncached_input": uncached,
            "output": output,
        }
        multiplier, timestamp_known = _token_plan_multiplier(row)
        if not timestamp_known:
            unknown_time_attempts += 1
        for kind, count in counts.items():
            token_totals[kind] += count
            row_credits = Decimal(count) * _CREDITS_PER_TOKEN[kind]
            undiscounted_credits += row_credits
            credits += row_credits * multiplier
            api_cost += Decimal(count) * _API_CNY_PER_MILLION[kind] / Decimal(1_000_000)

    if priced_attempts == 0:
        return None

    plans: dict[str, dict[str, Any]] = {}
    for name, plan in _TOKEN_PLANS.items():
        monthly_quota = Decimal(plan["monthly_credits"])
        annual_quota = Decimal(plan["annual_credits"])
        monthly_allocated = credits / monthly_quota * plan["monthly_fee_cny"]
        annual_allocated = credits / annual_quota * plan["annual_fee_cny"]
        plans[name] = {
            "monthly_fee_cny": float(plan["monthly_fee_cny"]),
            "monthly_credits": int(plan["monthly_credits"]),
            "monthly_quota_percent": float(credits / monthly_quota * Decimal("100")),
            "monthly_allocated_cost_cny": float(monthly_allocated),
            "annual_fee_cny": float(plan["annual_fee_cny"]),
            "annual_credits": int(plan["annual_credits"]),
            "annual_quota_percent": float(credits / annual_quota * Decimal("100")),
            "annual_allocated_cost_cny": float(annual_allocated),
        }

    return {
        "pricing_version": PRICING_VERSION,
        "model": _MODEL,
        "currency": "CNY",
        "priced_attempts": priced_attempts,
        "unpriced_attempts": unpriced_attempts,
        "unknown_time_attempts": unknown_time_attempts,
        "tokens": token_totals,
        "token_plan": {
            "credits_used": float(credits),
            "undiscounted_credits": float(undiscounted_credits),
            "off_peak_credits_saved": float(undiscounted_credits - credits),
            "plans": plans,
        },
        "api": {
            "cost_cny": float(api_cost),
            "rates_cny_per_million_tokens": {
                kind: float(rate) for kind, rate in _API_CNY_PER_MILLION.items()
            },
        },
        "sources": {"token_plan": TOKEN_PLAN_SOURCE, "api": API_PRICING_SOURCE},
        "note": (
            "Token Plan is a fixed subscription; allocated cost is the run's "
            "share of plan Credits."
        ),
    }


def calculate_mimo_v25_pro_run_cost(workspace: Path, run_id: str) -> dict[str, Any] | None:
    """Load one run's usage ledger and calculate its MiMo cost."""

    safe_run = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)
    ledger_path = Path(workspace).resolve() / ".manyselves" / "usage" / f"{safe_run}.jsonl"
    if not ledger_path.is_file():
        return None
    rows: list[dict[str, Any]] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return calculate_mimo_v25_pro_cost(rows)


def format_mimo_v25_pro_cost(report: dict[str, Any]) -> str:
    """Render a compact Chinese comparison for a successful terminal message."""

    plans = report["token_plan"]["plans"]
    labels = {"lite": "Lite", "standard": "Standard", "pro": "Pro"}
    lines = [f"MiMo-V2.5-Pro 成本换算（{report['pricing_version']}）："]
    for name in ("lite", "standard", "pro"):
        plan = plans[name]
        lines.append(
            f"- Token Plan {labels[name]}：月付折算 ¥{plan['monthly_allocated_cost_cny']:.2f} "
            f"（额度 {plan['monthly_quota_percent']:.2f}%）；"
            f"年付折算 ¥{plan['annual_allocated_cost_cny']:.2f}"
        )
    lines.append(
        f"- API 按量计费：¥{report['api']['cost_cny']:.2f}（仅模型 Token，不含联网插件）"
    )
    saved = report["token_plan"]["off_peak_credits_saved"]
    if saved > 0:
        lines.append(f"- 非高峰优惠：已少扣 {saved:,.0f} Credits")
    if report["unpriced_attempts"]:
        lines.append(f"- 未计价：{report['unpriced_attempts']} 次非 mimo-v2.5-pro 调用")
    if report["unknown_time_attempts"]:
        lines.append(f"- 时间未知：{report['unknown_time_attempts']} 次按高峰 Credits 估算")
    lines.append("Token Plan 金额为固定订阅费按本 run Credits 占比分摊，并非额外扣款。")
    return "\n".join(lines)
