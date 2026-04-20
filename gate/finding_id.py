"""Stable finding-id computation.

Lives in its own module so ``state.py`` and ``orchestrator.py`` can
import it without pulling in all of ``fixer.py`` (which would create an
import cycle via ``fixer → state → fixer``).

``fixer.py`` re-exports ``compute_finding_id`` for backward compatibility.
"""

import hashlib


def compute_finding_id(finding: dict) -> str:
    """Return a short stable id for a finding.

    The id is a SHA-1 prefix over the fields that uniquely identify a
    finding from the reviewer's perspective: ``(file, line, source_stage,
    message)``. Stable across re-reviews so the orchestrator can diff
    prior vs current verdicts (new / persisting / resolved findings).
    """
    file = str(finding.get("file", ""))
    line = str(finding.get("line", ""))
    stage = str(finding.get("source_stage", ""))
    message = str(finding.get("message", finding.get("title", "")))
    payload = f"{file}|{line}|{stage}|{message}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]
