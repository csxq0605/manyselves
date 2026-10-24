"""Carry author judgments through existing reporting tasks, without judging them.

Coverage here describes explicit records and exact prose anchors. It is neither
a semantic pass/fail decision nor a new workflow gate. In particular, an author
can declare a wrong judgment and the existing reviewer must still examine it.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models.agentic import (
    ModuleSubmission,
)
from .research.project_evidence import ProjectEvidenceIndex


def observation_identities(workspace: Path, run_id: str, modules: dict[str, ModuleSubmission]) -> str:
    """Runtime-derived object/time for every judged E-ID; Final has no E-* tools."""
    index = ProjectEvidenceIndex(workspace, run_id)
    known = {item.id: item for item in index.items()} if index.path.is_file() else {}
    judged = sorted({
        eid for module in modules.values() for part in module.assessments.values()
        for record in part.records or [] for eid in record.evidence_ids if eid in known
    })
    return json.dumps({
        eid: {"subject": known[eid].subject,
              **({"observed_at": known[eid].observed_at} if known[eid].observed_at else {})}
        for eid in judged
    }, ensure_ascii=False, separators=(",", ":"))


