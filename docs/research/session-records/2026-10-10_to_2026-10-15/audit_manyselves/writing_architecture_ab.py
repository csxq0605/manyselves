"""Synthetic architecture microbenchmark: 2 sizes x 2 repeats x (1+2+2) calls.

Not a production full-report benchmark. Uses production Provider and role bodies,
same model/observations/rules/answer contract. Never reads customer Inputs.
"""
import asyncio
import dataclasses
from decimal import Decimal
import json
from pathlib import Path
import random
import re
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "manyselves"))
import httpx
from loguru import logger
from manyselves.config.schema import Settings
from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import Message
from manyselves.capabilities.distribution_reporting.runtime.template_skill_context import render_template_skill

logger.disable("manyselves")
OUT = ROOT / "audit_manyselves/writing_architecture_ab.json"
IDS = [f"E-{i:04d}" for i in range(1, 9)]
EXPECTED = dict(zip(IDS, ["abnormal", "normal", "normal", "unknown", "abnormal", "unknown", "normal", "unknown"]))
OBSERVATIONS = [
    {"id": IDS[0], "object": "柜甲", "condition": "A", "value": "11.3", "unit": "A"},
    {"id": IDS[1], "object": "柜甲", "condition": "B", "value": "11.3", "unit": "A"},
    {"id": IDS[2], "object": "柜乙", "condition": "steady", "value": "2.3", "unit": "%"},
    {"id": IDS[3], "object": "柜丙", "condition": "thermal", "value": "55", "unit": "C", "ambient": None},
    {"id": IDS[4], "object": "柜丁", "condition": "residual", "value": "0.0113", "unit": "A"},
    {"id": IDS[6], "object": "柜己", "condition": "current_rule", "value": "5", "unit": "mA"},
    {"id": IDS[7], "object": "柜庚", "condition": None, "value": "16", "unit": "A"},
]
RULES = {
    "R-001": {"condition": "A", "unit": "A", "max": "10", "valid": True},
    "R-002": {"condition": "B", "unit": "A", "max": "20", "valid": True},
    "R-003": {"condition": "steady", "unit": "%", "max": "3", "valid": True},
    "R-004": {"condition": "thermal", "unit": "K", "max": "30", "valid": True, "requires_ambient": True},
    "R-005": {"condition": "residual", "unit": "mA", "max": "10", "valid": True},
    "R-006": {"condition": "current_rule", "unit": "mA", "max": "2", "valid": False, "superseded_by": "R-007"},
    "R-007": {"condition": "current_rule", "unit": "mA", "max": "10", "valid": True},
}
EXPECTED_REFS = {IDS[0]: ["R-001"], IDS[1]: ["R-002"], IDS[2]: ["R-003"], IDS[4]: ["R-005"], IDS[6]: ["R-007"]}


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
STATUS = {"type": "string", "enum": ["normal", "abnormal", "unknown"]}
def array(item, count=None):
    return {"type": "array", "items": item, **({"minItems": count, "maxItems": count} if count else {})}


FINAL = obj({"judgments": array(obj({"id": STRING, "status": STATUS, "evidence_ids": array(STRING),
                                     "rule_ids": array(STRING), "text": STRING}), 8),
             "summary": array(obj({"id": STRING, "status": STATUS}), 8),
             "chapters": array(STRING), "limitations": STRING})
SELECTION = obj({"selections": array(obj({"id": STRING, "rule_id": STRING, "reason": STRING}), 8)})


def deterministic_ledger(selection):
    rows = []
    for eid in IDS:
        observation = next((o for o in OBSERVATIONS if o["id"] == eid), None)
        choices = [s for s in selection.get("selections", []) if s.get("id") == eid]
        rid = choices[0].get("rule_id") if len(choices) == 1 else None
        rule = RULES.get(rid)
        status, comparison = "unknown", "资料、规则或适用条件不完整"
        if observation and rule and rule["valid"] and observation["condition"] == rule["condition"]:
            value = Decimal(observation["value"])
            unit = observation["unit"]
            if rule.get("requires_ambient"):
                if observation.get("ambient") is not None:
                    value -= Decimal(str(observation["ambient"]))
                    unit = "K"
            if unit == "A" and rule["unit"] == "mA":
                value *= 1000
                unit = "mA"
            if unit == rule["unit"]:
                status = "abnormal" if value > Decimal(rule["max"]) else "normal"
                comparison = f"{value} {unit} {'>' if status == 'abnormal' else '<='} {rule['max']} {unit}"
        rows.append({"judgment_id": "J-" + eid, "id": eid, "status": status,
                     "evidence_ids": [eid] if observation else [], "rule_ids": [rid] if rule else [],
                     "comparison": comparison, "observation": observation, "rule": rule})
    return rows


def score(result):
    errors = []
    rows = result.get("judgments", [])
    for eid in IDS:
        matched = [row for row in rows if row.get("id") == eid]
        if len(matched) != 1:
            errors.append(f"coverage:{eid}")
            continue
        row = matched[0]
        if row.get("status") != EXPECTED[eid]: errors.append(f"conclusion:{eid}")
        refs = row.get("evidence_ids", [])
        if eid == IDS[5]:
            if refs: errors.append(f"invented_evidence:{eid}")
        elif eid not in refs: errors.append(f"evidence_missing:{eid}")
        if any(ref not in {o['id'] for o in OBSERVATIONS} for ref in refs): errors.append(f"unknown_evidence:{eid}")
        if eid in EXPECTED_REFS and set(row.get("rule_ids", [])) != set(EXPECTED_REFS[eid]): errors.append(f"rule:{eid}")
        if any(ref not in RULES for ref in row.get("rule_ids", [])): errors.append(f"unknown_rule:{eid}")
        summary = [s for s in result.get("summary", []) if s.get("id") == eid]
        if len(summary) != 1 or summary[0].get("status") != row.get("status"): errors.append(f"consistency:{eid}")
    chapter_ids = [re.match(r"^(\d+\.\d+)(?:\s|$)", value) for value in result.get("chapters", [])]
    if {match.group(1) if match else "invalid" for match in chapter_ids} != {"2.4", "3.2"}: errors.append("scope")
    return errors


async def main():
    live = "--live" in sys.argv
    typed = "--typed" in sys.argv
    if typed:
        for observation in OBSERVATIONS:
            observation["quantity"] = "measured_temperature" if observation["id"] == IDS[3] else "measurement"
    output = OUT if live else OUT.with_name("writing_architecture_preflight.json")
    if typed: output = output.with_name(output.stem + "_typed.json")
    report = {"synthetic_only": True, "production_workflow": False, "max_calls": 20,
              "oracle": EXPECTED, "cases": [], "attempts": 0, "explicit_quantity": typed}
    def save(): output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    provider = None
    if live:
        config = Settings().load_config()
        route = next(c for c in config.providers.configurations if c.id == config.providers.active)
        if route.provider != "anthropic" or urlsplit(route.api_base).hostname != "api.anthropic.com":
            raise RuntimeError("Synthetic trial requires the configured official Anthropic endpoint")
        provider = AnthropicProvider(api_key=route.api_key, api_base=route.api_base, model=route.default_model, extra_headers=route.extra_headers)
        provider.client = provider.client.with_options(timeout=httpx.Timeout(60, connect=10), max_retries=0)
        report["model"] = route.default_model
    role = (ROOT / "manyselves/manyselves/capabilities/distribution_reporting/agents/module-2.4-specialist.md").read_text(encoding="utf-8").split("---", 2)[2]
    boundary = render_template_skill("author-2.4", "旧模板示例：必须含第四章，所有缺陷必须三级分类并预测劣化时间。")
    contract = (
        "这是完全虚构的合成测试，不是实际电气规则。只写2.4和3.2两个章节。"
        "按指定8个观测ID输出简短判断；摘要必须对应同一观测判断。只使用给定规则，严格大于max为异常。"
        "不同工况不得合并；缺观测、缺温升参考、未知工况都为unknown，不编造风险等级、寿命或来源。"
        "E-0006是要求检查但没有记录的对象，不能把该ID作为存在的证据引用。"
        "必须调用submit_result提交本阶段结构，单项text控制在60字以内。"
    )
    async def call(entry, stage, body, schema):
        report["attempts"] += 1
        assert report["attempts"] <= 20
        messages = [Message(role="system", content=role), Message(role="user", content=contract + "\n" + body)]
        event = {"stage": stage, "request_chars": sum(len(m.content) for m in messages)}
        entry["requests"].append(event)
        if not live: return {}
        start = time.perf_counter()
        try:
            response = await provider.chat(messages, tools=[{
                "name": "submit_result", "description": "提交本阶段结果", "input_schema": schema}],
                max_tokens=3000, reasoning_enabled=False)
            event.update(seconds=time.perf_counter()-start, usage=response.usage,
                         response=dataclasses.asdict(response))
            calls = [c for c in response.tool_calls if c.name == "submit_result"]
            return calls[0].arguments if len(calls) == 1 else {}
        except Exception as exc:
            event.update(seconds=time.perf_counter()-start, error_type=type(exc).__name__, status_code=getattr(exc,"status_code",None))
            report["stopped_on_provider_error"] = True
            save()
            raise
        finally: save()

    rng = random.Random(1014)
    try:
        for repeat in range(2):
            for size in ("short", "long"):
                arms = ["direct", "write_review", "judgment_first"]
                rng.shuffle(arms)
                filler = ("背景说明：不同工况不能代替对应观测，以下说明不是检测结果。\n" * 200) if size == "long" else ""
                source = json.dumps({"requested_ids": IDS, "observations": OBSERVATIONS, "rules": RULES}, ensure_ascii=False)
                full = boundary + "\n" + filler + "\n完整合成资料：\n" + source
                for arm in arms:
                    entry = {"repeat": repeat, "size": size, "arm": arm, "requests": []}
                    report["cases"].append(entry)
                    start = time.perf_counter()
                    if arm == "judgment_first":
                        selection = await call(entry, "select_rules", full + "\n先为每个观测选择适用规则ID，无法确定填unknown，不写报告。", SELECTION)
                        ledger = deterministic_ledger(selection)
                        entry["ledger"] = ledger
                        result = await call(entry, "write_from_ledger", boundary + "\n程序检查的共享判断清单（需保留同一ID、工况、来源和结论；不重新判断）：\n" + json.dumps(ledger,ensure_ascii=False), FINAL)
                    else:
                        result = await call(entry, "write", full, FINAL)
                        if arm == "write_review":
                            result = await call(entry, "review_and_correct", full + "\n独立检查候选报告的事实、计算、适用规则与摘要一致性，提交完整修正结果：\n" + json.dumps(result,ensure_ascii=False), FINAL)
                    entry.update(result=result, errors=score(result) if live else [], wall_seconds=time.perf_counter()-start)
                    save()
                    print(json.dumps({k: entry[k] for k in ("repeat", "size", "arm", "errors", "wall_seconds")},ensure_ascii=False), flush=True)
        report["complete"] = True
    finally:
        save()
        if provider: await provider.client.close()


if __name__ == "__main__": asyncio.run(main())
