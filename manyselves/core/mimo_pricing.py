"""Reproducible MiMo-V2.5 model, Token Plan, and API cost conversion."""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PRICING_VERSION = "xiaomi-mimo-2026-08-18-official-docs"
TOKEN_PLAN_SOURCE = "https://mimo.mi.com/docs/zh-CN/price/token-plan"
API_PRICING_SOURCE = "https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go"

_BEIJING = ZoneInfo("Asia/Shanghai")
_OFF_PEAK_MULTIPLIER = Decimal("0.8")
_MODEL_RATES = {
    "mimo-v2.5-pro": {
        "credits_per_token": {
            "cached_input": Decimal("2.5"),
            "uncached_input": Decimal("300"),
            "output": Decimal("600"),
        },
        "api_cny_per_million": {
            "cached_input": Decimal("0.025"),
            "uncached_input": Decimal("3"),
            "output": Decimal("6"),
        },
    },
    "mimo-v2.5": {
        "credits_per_token": {
            "cached_input": Decimal("2"),
            "uncached_input": Decimal("100"),
            "output": Decimal("200"),
        },
        "api_cny_per_million": {
            "cached_input": Decimal("0.02"),
            "uncached_input": Decimal("1"),
            "output": Decimal("2"),
        },
    },
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
    "max": {
        "monthly_fee_cny": Decimal("659"),
        "monthly_credits": 82_000_000_000,
        "annual_fee_cny": Decimal("6959.04"),
        "annual_credits": 984_000_000_000,
    },
}


def _integer(row: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(row.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def normalize_mimo_model(value: Any) -> str | None:
    """Return one supported canonical model name, including routed model IDs."""

    model = str(value or "").strip().casefold().rsplit("/", 1)[-1]
    return model if model in _MODEL_RATES else None


def configured_mimo_model(environment: dict[str, str] | None = None) -> str | None:
    """Resolve the account/process model selected by its dotenv-backed environment."""

    values = os.environ if environment is None else environment
    for key in ("MANYSELVES_BOOTSTRAP_MODEL", "MIMO_MODEL"):
        model = normalize_mimo_model(values.get(key))
        if model is not None:
            return model
    if environment is None and not os.getenv("MANYSELVES_ACCOUNTS_FILE"):
        # Pydantic reads the repository/account dotenv without mutating
        # os.environ. Use the same settings source so CLI cost conversion and
        # legacy model-less ledgers still honor the selected .env model.
        from ..config.schema import Settings

        return normalize_mimo_model(Settings().mimo_model)
    return None


def _provider_request_sent(row: dict[str, Any]) -> bool:
    value = row.get("provider_request_sent", True)
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _token_counts(row: dict[str, Any]) -> tuple[int, int, int, int]:
    cached = _integer(row, "cached_input_tokens")
    cache_write = _integer(row, "cache_write_input_tokens")
    if "uncached_input_tokens" in row:
        api_uncached = _integer(row, "uncached_input_tokens")
    else:
        api_uncached = max(
            0,
            _integer(row, "input_tokens")
            - cached
            - cache_write,
        )
    # Token Plan has no separate cache-write rate, so creation consumes the
    # miss rate. The pay-as-you-go page explicitly marks cache writes free.
    plan_uncached = api_uncached + cache_write
    return cached, plan_uncached, api_uncached, _integer(row, "output_tokens")


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


def calculate_mimo_cost(
    rows: Iterable[dict[str, Any]],
    *,
    default_model: str | None = None,
) -> dict[str, Any] | None:
    """Price supported MiMo rows under four Token Plans and domestic API rates."""

    fallback_model = normalize_mimo_model(default_model) or configured_mimo_model()
    token_totals = {"cached_input": 0, "uncached_input": 0, "output": 0}
    api_token_totals = {"cached_input": 0, "uncached_input": 0, "output": 0}
    model_totals: dict[str, dict[str, Any]] = {}
    api_cost = Decimal("0")
    credits = Decimal("0")
    undiscounted_credits = Decimal("0")
    priced_attempts = 0
    unpriced_attempts = 0
    unknown_time_attempts = 0

    for row in rows:
        if not _provider_request_sent(row):
            continue
        raw_model = row.get("resolved_model") or row.get("model")
        model = normalize_mimo_model(raw_model)
        if not str(raw_model or "").strip():
            model = fallback_model
        if model is None:
            unpriced_attempts += 1
            continue
        priced_attempts += 1
        cached, uncached, api_uncached, output = _token_counts(row)
        counts = {
            "cached_input": cached,
            "uncached_input": uncached,
            "output": output,
        }
        multiplier, timestamp_known = _token_plan_multiplier(row)
        if not timestamp_known:
            unknown_time_attempts += 1
        rates = _MODEL_RATES[model]
        model_bucket = model_totals.setdefault(
            model,
            {
                "priced_attempts": 0,
                "tokens": {"cached_input": 0, "uncached_input": 0, "output": 0},
                "api_billable_tokens": {
                    "cached_input": 0,
                    "uncached_input": 0,
                    "output": 0,
                },
                "credits_used": Decimal("0"),
                "api_cost_cny": Decimal("0"),
            },
        )
        model_bucket["priced_attempts"] += 1
        for kind, count in counts.items():
            token_totals[kind] += count
            model_bucket["tokens"][kind] += count
            row_credits = Decimal(count) * rates["credits_per_token"][kind]
            charged_credits = row_credits * multiplier
            api_count = api_uncached if kind == "uncached_input" else count
            api_token_totals[kind] += api_count
            model_bucket["api_billable_tokens"][kind] += api_count
            row_api_cost = (
                Decimal(api_count)
                * rates["api_cny_per_million"][kind]
                / Decimal(1_000_000)
            )
            undiscounted_credits += row_credits
            credits += charged_credits
            api_cost += row_api_cost
            model_bucket["credits_used"] += charged_credits
            model_bucket["api_cost_cny"] += row_api_cost

    if priced_attempts == 0:
        return None

    plans: dict[str, dict[str, Any]] = {}
    for name, plan in _TOKEN_PLANS.items():
        monthly_quota = Decimal(plan["monthly_credits"])
        annual_quota = Decimal(plan["annual_credits"])
        plans[name] = {
            "monthly_fee_cny": float(plan["monthly_fee_cny"]),
            "monthly_credits": int(plan["monthly_credits"]),
            "monthly_quota_percent": float(credits / monthly_quota * Decimal("100")),
            "monthly_allocated_cost_cny": float(
                credits / monthly_quota * plan["monthly_fee_cny"]
            ),
            "annual_fee_cny": float(plan["annual_fee_cny"]),
            "annual_credits": int(plan["annual_credits"]),
            "annual_quota_percent": float(credits / annual_quota * Decimal("100")),
            "annual_allocated_cost_cny": float(
                credits / annual_quota * plan["annual_fee_cny"]
            ),
        }

    serialized_models: dict[str, dict[str, Any]] = {}
    for model, bucket in sorted(model_totals.items()):
        rates = _MODEL_RATES[model]
        serialized_models[model] = {
            "priced_attempts": bucket["priced_attempts"],
            "tokens": bucket["tokens"],
            "api_billable_tokens": bucket["api_billable_tokens"],
            "credits_used": float(bucket["credits_used"]),
            "credits_per_token": {
                kind: float(rate) for kind, rate in rates["credits_per_token"].items()
            },
            "api_cost_cny": float(bucket["api_cost_cny"]),
            "api_rates_cny_per_million_tokens": {
                kind: float(rate)
                for kind, rate in rates["api_cny_per_million"].items()
            },
        }

    model_names = list(serialized_models)
    api_summary: dict[str, Any] = {
        "cost_cny": float(api_cost),
        "billable_tokens": api_token_totals,
    }
    if len(model_names) == 1:
        api_summary["rates_cny_per_million_tokens"] = serialized_models[
            model_names[0]
        ]["api_rates_cny_per_million_tokens"]

    return {
        "pricing_version": PRICING_VERSION,
        "model": model_names[0] if len(model_names) == 1 else "mixed",
        "models": serialized_models,
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
        "api": api_summary,
        "sources": {"token_plan": TOKEN_PLAN_SOURCE, "api": API_PRICING_SOURCE},
        "note": (
            "Token Plan is a fixed coding-tool subscription; this is an allocation "
            "comparison, not authorization to use a Token Plan key from an application backend."
        ),
    }


def calculate_mimo_run_cost(
    workspace: Path,
    run_id: str,
    *,
    default_model: str | None = None,
) -> dict[str, Any] | None:
    """Load one run's usage ledger and calculate model-aware MiMo cost."""

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
    return calculate_mimo_cost(rows, default_model=default_model)


def format_mimo_cost(report: dict[str, Any]) -> str:
    """Render a compact Chinese model/rate comparison for a terminal message."""

    plans = report["token_plan"]["plans"]
    labels = {"lite": "Lite", "standard": "Standard", "pro": "Pro", "max": "Max"}
    models = "、".join(report["models"])
    lines = [f"MiMo 成本换算（{models}；{report['pricing_version']}）："]
    for name in ("lite", "standard", "pro", "max"):
        plan = plans[name]
        lines.append(
            f"- Token Plan {labels[name]}：月付折算 ¥{plan['monthly_allocated_cost_cny']:.2f} "
            f"（额度 {plan['monthly_quota_percent']:.2f}%）；"
            f"年付折算 ¥{plan['annual_allocated_cost_cny']:.2f}"
        )
    lines.append(
        f"- API 按量计费：¥{report['api']['cost_cny']:.2f}（仅模型 Token，不含联网插件）"
    )
    for model, details in report["models"].items():
        rates = details["api_rates_cny_per_million_tokens"]
        lines.append(
            f"- {model} API 单价（缓存/未缓存/输出，每百万 Token）："
            f"¥{rates['cached_input']:g}/¥{rates['uncached_input']:g}/¥{rates['output']:g}"
        )
    saved = report["token_plan"]["off_peak_credits_saved"]
    if saved > 0:
        lines.append(f"- 非高峰优惠：已少扣 {saved:,.0f} Credits")
    if report["unpriced_attempts"]:
        lines.append(f"- 未计价：{report['unpriced_attempts']} 次非受支持 MiMo 调用")
    if report["unknown_time_attempts"]:
        lines.append(f"- 时间未知：{report['unknown_time_attempts']} 次按高峰 Credits 估算")
    lines.append("Token Plan 仅作固定订阅额度折算；自定义服务端应遵守其 Coding 场景限制。")
    return "\n".join(lines)


def calculate_mimo_v25_pro_cost(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Backward-compatible alias for older callers."""

    return calculate_mimo_cost(rows, default_model="mimo-v2.5-pro")


def calculate_mimo_v25_pro_run_cost(workspace: Path, run_id: str) -> dict[str, Any] | None:
    """Backward-compatible alias for older callers."""

    return calculate_mimo_run_cost(workspace, run_id, default_model="mimo-v2.5-pro")


def format_mimo_v25_pro_cost(report: dict[str, Any]) -> str:
    """Backward-compatible alias for older callers."""

    return format_mimo_cost(report)
