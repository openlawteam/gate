"""Typed provider boundaries used by Gate's orchestrators.

v0.1 intentionally ships only the Claude review runner and Claude/Codex fix
pipeline.  These protocols keep that product decision separate from the
orchestrator's control flow, making test doubles and future adapters possible
without adding a public provider-selection surface yet.
"""

from pathlib import Path
from threading import Event
from typing import Protocol

from gate.schemas import FixResult, StageResult


class StageRunnerProtocol(Protocol):
    """Execute one structured review stage."""

    def run(
        self,
        stage: str,
        prompt_text: str,
        workspace: Path,
        config: dict,
        cancelled: Event | None = None,
    ) -> StageResult: ...


class FixRunnerProtocol(Protocol):
    """Execute Gate's bounded fix pipeline."""

    branch: str
    fix_pane_id: str | None

    def run(self) -> FixResult: ...
