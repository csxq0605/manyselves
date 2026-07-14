from __future__ import annotations

import re

from pds_report.domain.models import ReportRequest


class RequestValidationError(ValueError):
    pass


MODULE_PATTERN = re.compile(r"2\.[1-5]")


def parse_report_request(message: str) -> ReportRequest:
    text = message.strip()
    if not text:
        raise RequestValidationError("报告请求不能为空")
    if "报告" not in text or not any(verb in text for verb in ("写", "生成", "编制")):
        raise RequestValidationError("当前消息不是报告生成请求")

    mentioned = list(dict.fromkeys(MODULE_PATTERN.findall(text)))
    skipped = list(
        dict.fromkeys(
            match.group(1)
            for match in re.finditer(r"(?:跳过|不做|忽略)\s*(2\.[1-5])", text)
        )
    )
    target_modules = [module for module in mentioned if module not in skipped]
    if not target_modules:
        target_modules = ["2.4"]

    requirements: list[str] = []
    if "深度思考" in text or "深度推理" in text:
        requirements.append("deep_reasoning")

    return ReportRequest(
        task="write_report",
        target_modules=target_modules,  # type: ignore[arg-type]
        execution_requirements=requirements,
        temporary_constraints=[f"skip:{module}" for module in skipped],
        allow_pending_evidence="待核实稿" in text or "允许待核实" in text,
    )

