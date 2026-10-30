"""What the knowledge context tells a model about sources it does not inline."""

from __future__ import annotations

import hashlib
from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.research.knowledge_context import (
    KnowledgeContextBuilder,
)
from manyselves.capabilities.distribution_reporting.runtime.research.reference_library import (
    ReferenceDocument,
)


def _document(title: str, text: str) -> ReferenceDocument:
    return ReferenceDocument(
        title=title,
        relative_path=f"Knowledge/{title}.md",
        text=text,
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def test_an_unexpanded_source_is_not_offered_through_a_reading_tool(tmp_path: Path) -> None:
    # No turn has a tool that reads a knowledge source, so the text must not
    # tell the model to call one.
    large = _document("变压器温升", "温升 " * (KnowledgeContextBuilder.MAX_MODULE_CHARS))
    builder = KnowledgeContextBuilder(tmp_path, "run-1")
    builder._documents = (large,)

    context = builder._assemble(
        "# 模块知识",
        [("## 2.1 变压器", "温升", [large])],
        "knowledge.md",
        include_catalog=True,
    )

    assert "open_reference" not in context.text
    assert "本次读不到" in context.text
