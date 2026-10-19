"""Offline synthetic knowledge packet size/correctness comparison; no Provider."""
import json
from pathlib import Path
import subprocess
import sys
import types

from manyselves.capabilities.distribution_reporting.runtime.research.knowledge_context import KnowledgeContextBuilder
from manyselves.capabilities.distribution_reporting.runtime.research.reference_library import ReferenceDocument

ROOT = Path(__file__).parent
name = "manyselves.capabilities.distribution_reporting.runtime.research.knowledge_context_before"
module = types.ModuleType(name)
module.__package__ = name.rsplit(".", 1)[0]
sys.modules[name] = module
source = subprocess.check_output(["git", "show", "3e3ec12:manyselves/capabilities/distribution_reporting/runtime/research/knowledge_context.py"]).decode("utf-8")
exec(compile(source, name, "exec"), module.__dict__)
prefix = "# 2.4.4\n" + "相同前文。" * 250
cases = {
    "short_whole_source": ["# 适用范围\n仅适用于工况甲。\n# 2.4.4\n" + "测量方法。" * 250 + "\n例外：工况乙不适用。"],
    "same_excerpt_distinct_sources": [prefix + "\n甲仅适用于A。", prefix + "\n乙仅适用于B。"],
    "large_source": ["# 2.4.4\n" + "规定。" * 8000 + "\n尾部例外。"],
}
results = {}
for case, texts in cases.items():
    results[case] = {}
    for arm, builder_type in (("before", module.KnowledgeContextBuilder), ("after", KnowledgeContextBuilder)):
        builder = builder_type(ROOT / "knowledge_packet_fixtures" / case / arm, "probe")
        builder._documents = tuple(ReferenceDocument(f"规程{i}", f"Knowledge/rule-{i}.md", text, str(i)) for i, text in enumerate(texts))
        packet = builder.build_module("2.4")
        results[case][arm] = {"packet_chars": len(packet.text), "source_ids": len(packet.source_ids),
                             "full_sources_inline": sum(text in packet.text for text in texts),
                             "explicit_unexpanded": "原文未展开" in packet.text}
(ROOT / "knowledge_packet_metrics_1014.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(results, ensure_ascii=False, indent=2))
