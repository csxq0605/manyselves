#!/usr/bin/env python3
"""Calculate MiMo-V2.5-Pro Lite/Standard/Pro/API cost for a report run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from manyselves.core.mimo_pricing import (  # noqa: E402
    calculate_mimo_v25_pro_cost,
    calculate_mimo_v25_pro_run_cost,
    format_mimo_v25_pro_cost,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="换算 mimo-v2.5-pro 的 Lite、Standard、Pro 和国内 API 人民币成本。"
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--run-id", help="从 .manyselves/usage/<run-id>.jsonl 读取用量")
    parser.add_argument("--cached-input-tokens", type=int, default=0)
    parser.add_argument("--uncached-input-tokens", type=int, default=0)
    parser.add_argument("--output-tokens", type=int, default=0)
    parser.add_argument(
        "--off-peak",
        action="store_true",
        help="直接输入 Token 时按北京时间 0:00-8:00 的 0.8 倍 Credits 计算",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def main() -> int:
    args = _parser().parse_args()
    explicit_tokens = args.cached_input_tokens + args.uncached_input_tokens + args.output_tokens
    if bool(args.run_id) == bool(explicit_tokens):
        raise SystemExit("必须且只能提供 --run-id，或提供一组非零 Token 数。")
    if args.run_id:
        report = calculate_mimo_v25_pro_run_cost(args.workspace, args.run_id)
    else:
        timestamp = (
            "2026-01-01T01:00:00+08:00"
            if args.off_peak
            else "2026-01-01T12:00:00+08:00"
        )
        report = calculate_mimo_v25_pro_cost(
            [{
                "timestamp": timestamp,
                "model": "mimo-v2.5-pro",
                "cached_input_tokens": args.cached_input_tokens,
                "uncached_input_tokens": args.uncached_input_tokens,
                "output_tokens": args.output_tokens,
            }]
        )
    if report is None:
        raise SystemExit("没有找到可计价的 mimo-v2.5-pro usage 记录。")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_mimo_v25_pro_cost(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
