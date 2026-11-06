"""Evidence: every result a finder tool returned, kept under a stable id.

A loss item cites the evidence it was found with.  The id is derived from the
tool and its arguments, so the same question asked twice gets the same id.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .store import RunDataError, SiteData, read_json, write_json

EVIDENCE_ID = re.compile(r"^EV-[0-9a-f]{10}$")


def record_evidence(
    site: SiteData,
    *,
    partition_id: str | None,
    tool: str,
    arguments: dict[str, Any],
    meter_ids: list[str],
    result: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {"tool": tool, "partition": partition_id, "arguments": arguments},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    evidence_id = "EV-" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]
    write_json(
        site.path(f"evidence/{evidence_id}.json"),
        {
            "evidence_id": evidence_id,
            "tool": tool,
            "partition_id": partition_id,
            "arguments": arguments,
            "meter_ids": meter_ids,
            "result": result,
        },
    )
    return evidence_id


def load_evidence(site: SiteData, evidence_id: str) -> dict[str, Any]:
    if not EVIDENCE_ID.match(evidence_id):
        raise RunDataError(f"not an evidence id: {evidence_id!r}")
    return read_json(site.path(f"evidence/{evidence_id}.json"))


def has_evidence(site: SiteData, evidence_id: str) -> bool:
    return bool(EVIDENCE_ID.match(evidence_id)) and site.path(f"evidence/{evidence_id}.json").is_file()


__all__ = ["EVIDENCE_ID", "has_evidence", "load_evidence", "record_evidence"]
