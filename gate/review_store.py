"""Typed data sources for Gate's live and replay Flight Recorder views."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from gate.config import repo_slug, resource_path, state_dir
from gate.logger import live_dir


@dataclass(frozen=True)
class StageSummary:
    name: str
    status: str = "pending"
    elapsed_s: float | None = None
    findings_count: int | None = None
    evidence: str = ""


@dataclass(frozen=True)
class ReviewArchiveSummary:
    review_id: str
    repo: str
    pr_number: int
    head_sha: str
    completed_at: str
    duration_s: float | None
    outcome: str
    decision: str
    phase: str
    findings_count: int
    parent_review_id: str | None = None
    audit_flags: tuple[str, ...] = ()
    path: Path | None = None


@dataclass(frozen=True)
class ReviewArchiveDetail:
    summary: ReviewArchiveSummary
    stages: tuple[StageSummary, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    activity: tuple[str, ...] = ()


@dataclass(frozen=True)
class DashboardSnapshot:
    active: tuple[dict[str, Any], ...] = ()
    queue: tuple[dict[str, Any], ...] = ()
    health: dict[str, Any] = field(default_factory=dict)
    recent: tuple[ReviewArchiveSummary, ...] = ()


class ReviewDataSource(Protocol):
    """Synchronous data surface consumed by Textual views."""

    def dashboard(self) -> DashboardSnapshot: ...

    def history(self, limit: int = 100) -> list[ReviewArchiveSummary]: ...

    def detail(self, review_id: str) -> ReviewArchiveDetail | None: ...

    def refresh(self, now_ms: int | None = None) -> None: ...


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _summary_from_log(path: Path, data: dict[str, Any]) -> ReviewArchiveSummary:
    """Normalize both v0 and v1 stage logs without rewriting history."""
    name = str(data.get("review_id") or path.parent.name)
    name_parts = name.split("-", 2)
    phase = str(data.get("phase") or (name_parts[2] if len(name_parts) > 2 else "pre-fix"))
    sha = str(data.get("head_sha") or data.get("sha") or "")
    audit_flags = [str(flag) for flag in (data.get("audit_flags") or [])]
    try:
        contradiction_dir = path.parents[2] / "contradictions"
        for contradiction in contradiction_dir.glob("*.json"):
            record = _load_json(contradiction)
            if not sha or str(record.get("sha") or "") == sha:
                audit_flags.append("external_check_contradiction")
                break
    except IndexError:
        pass
    return ReviewArchiveSummary(
        review_id=name,
        repo=str(data.get("repo") or ""),
        pr_number=int(data.get("pr_number") or 0),
        head_sha=sha,
        completed_at=str(data.get("completed_at") or data.get("written_at") or ""),
        duration_s=_number(data.get("duration_s")),
        outcome=str(data.get("outcome") or "reviewed"),
        decision=str(data.get("decision") or "unknown"),
        phase=phase,
        findings_count=int(data.get("findings_count") or 0),
        parent_review_id=(str(data["parent_review_id"]) if data.get("parent_review_id") else None),
        audit_flags=tuple(dict.fromkeys(audit_flags)),
        path=path.parent,
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _detail_from_path(summary: ReviewArchiveSummary, archive: Path) -> ReviewArchiveDetail:
    stage_log = _load_json(archive / "stage_log.json")
    verdict = _load_json(archive / "verdict.json")
    stages: list[StageSummary] = []
    evidence: dict[str, Any] = {}
    for filename, stage_data in (stage_log.get("stages") or {}).items():
        if not isinstance(stage_data, dict):
            continue
        stage_name = str(filename).removesuffix(".json")
        status = "skipped" if stage_data.get("skipped") else "completed"
        if stage_data.get("error") or stage_data.get("pass") is False:
            status = "error"
        stages.append(
            StageSummary(
                name=stage_name,
                status=status,
                elapsed_s=_number(stage_data.get("elapsed_s")),
                findings_count=(
                    int(stage_data["findings_count"])
                    if stage_data.get("findings_count") is not None
                    else None
                ),
            )
        )
    for path in sorted(archive.glob("*.json")):
        if path.name != "stage_log.json":
            evidence[path.stem] = _load_json(path)
    contradiction_dir = archive.parent.parent / "contradictions"
    contradictions = []
    for path in sorted(contradiction_dir.glob("*.json")):
        record = _load_json(path)
        if not summary.head_sha or str(record.get("sha") or "") == summary.head_sha:
            contradictions.append(record)
    if contradictions:
        evidence["contradictions"] = contradictions
    lineage_by_sha = _load_json(archive.parent.parent / "fix_lineage.json")
    if isinstance(lineage_by_sha.get(summary.head_sha), dict):
        evidence["fix_lineage"] = lineage_by_sha[summary.head_sha]
    findings = verdict.get("findings") or []
    activity: tuple[str, ...] = ()
    if archive.is_relative_to(state_dir()):
        live_log = live_dir() / repo_slug(summary.repo) / f"pr{summary.pr_number}.log"
        try:
            activity = tuple(live_log.read_text(encoding="utf-8").splitlines()[-50:])
        except OSError:
            pass
    return ReviewArchiveDetail(
        summary=summary,
        stages=tuple(stages),
        findings=tuple(item for item in findings if isinstance(item, dict)),
        evidence=evidence,
        activity=activity,
    )


class LiveReviewDataSource:
    """Combine the in-process server snapshot with durable local archives."""

    def __init__(self, server: Any = None):
        self.server = server

    def refresh(self, now_ms: int | None = None) -> None:
        return None

    def history(self, limit: int = 100) -> list[ReviewArchiveSummary]:
        records: list[ReviewArchiveSummary] = []
        root = state_dir()
        if not root.exists():
            return records
        for stage_log in root.glob("**/pr*/reviews/*/stage_log.json"):
            data = _load_json(stage_log)
            if not data:
                continue
            summary = _summary_from_log(stage_log, data)
            # Backfill repo/PR for legacy archives from their directory names.
            if not summary.repo or not summary.pr_number:
                pr_dir = stage_log.parents[2]
                try:
                    pr_number = int(pr_dir.name.removeprefix("pr"))
                except ValueError:
                    pr_number = 0
                repo = summary.repo
                if not repo and pr_dir.parent != root:
                    repo = pr_dir.parent.name.replace("-", "/", 1)
                summary = ReviewArchiveSummary(
                    **{
                        **summary.__dict__,
                        "repo": repo,
                        "pr_number": pr_number,
                    }
                )
            records.append(summary)
        records.sort(key=lambda item: (item.completed_at, item.review_id), reverse=True)
        return records[:limit]

    def dashboard(self) -> DashboardSnapshot:
        active = tuple(dict(item) for item in (getattr(self.server, "reviews", None) or []))
        queue = tuple(dict(item) for item in (getattr(self.server, "review_queue", None) or []))
        health = dict(getattr(self.server, "health", None) or {})
        return DashboardSnapshot(
            active=active, queue=queue, health=health, recent=tuple(self.history(8))
        )

    def detail(self, review_id: str) -> ReviewArchiveDetail | None:
        history = self.history(500)
        for summary in history:
            if summary.review_id == review_id and summary.path:
                detail = _detail_from_path(summary, summary.path)
                if summary.parent_review_id:
                    parent = next(
                        (item for item in history if item.review_id == summary.parent_review_id),
                        None,
                    )
                    if parent:
                        detail.evidence["lineage"] = {
                            "parent_review_id": parent.review_id,
                            "parent_sha": parent.head_sha,
                            "parent_decision": parent.decision,
                            "parent_findings_count": parent.findings_count,
                            "post_fix_findings_count": summary.findings_count,
                        }
                return detail
        for review in self.dashboard().active:
            if review.get("id") == review_id:
                summary = ReviewArchiveSummary(
                    review_id=review_id,
                    repo=str(review.get("repo") or ""),
                    pr_number=int(review.get("pr_number") or 0),
                    head_sha=str(review.get("head_sha") or ""),
                    completed_at="",
                    duration_s=None,
                    outcome=str(review.get("outcome") or "running"),
                    decision=str(review.get("decision") or "pending"),
                    phase="auto-fix" if review.get("status") == "fixing" else "pre-fix",
                    findings_count=0,
                )
                return ReviewArchiveDetail(
                    summary=summary,
                    stages=(StageSummary(str(review.get("stage") or "triage"), "running"),),
                )
        return None


class ReplayReviewDataSource:
    """Deterministic, read-only protocol replay used by ``gate demo``."""

    def __init__(self, scenario: str = "risky-auth-refactor", speed: float = 1.0):
        root = resource_path("demo", scenario)
        self.scenario = scenario
        self.manifest = _load_json(root / "manifest.json")
        self._archive = root / "archive"
        self._events: list[dict[str, Any]] = []
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    self._events.append(value)
        self.speed = speed
        self.paused = False
        self._started_ms = int(time.monotonic() * 1000)
        self._paused_at_ms: int | None = None
        self._index = 0
        self._active: dict[str, dict[str, Any]] = {}
        self._queue: list[dict[str, Any]] = []
        self._terminal: list[ReviewArchiveSummary] = []

    @property
    def complete(self) -> bool:
        return self._index >= len(self._events)

    def _apply(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        review = event.get("review") or {}
        review_id = str(review.get("id") or event.get("review_id") or "")
        if event_type == "queue_updated":
            self._queue = [dict(item) for item in (event.get("queue") or [])]
        elif event_type in ("review_started", "review_updated") and review_id:
            current = self._active.setdefault(review_id, {})
            current.update(review)
        elif event_type == "review_stage_update" and review_id:
            current = self._active.setdefault(review_id, {"id": review_id})
            current.update(
                {
                    "stage": event.get("stage", ""),
                    "status": event.get("status", "running"),
                    "updated_at": event.get("ts", 0),
                }
            )
        elif event_type == "review_completed" and review_id:
            current = self._active.pop(review_id, {"id": review_id})
            current.update(review)
            current["outcome"] = event.get("outcome", "reviewed")
            current["decision"] = event.get("decision", "")
            self._terminal = [self._archive_summary()]

    def refresh(self, now_ms: int | None = None) -> None:
        if self.paused:
            return
        now = now_ms if now_ms is not None else int(time.monotonic() * 1000)
        elapsed = int((now - self._started_ms) * self.speed)
        while self._index < len(self._events):
            item = self._events[self._index]
            if int(item.get("at_ms") or 0) > elapsed:
                break
            self._apply(dict(item.get("event") or {}))
            self._index += 1

    def step(self, direction: int) -> None:
        target = max(0, min(len(self._events), self._index + direction))
        self.reset()
        while self._index < target:
            self._apply(dict(self._events[self._index].get("event") or {}))
            self._index += 1
        self.paused = True
        self._paused_at_ms = int(time.monotonic() * 1000)

    def reset(self) -> None:
        self._index = 0
        self._active.clear()
        self._queue.clear()
        self._terminal.clear()
        self._started_ms = int(time.monotonic() * 1000)
        self.paused = False
        self._paused_at_ms = None

    def finish(self) -> None:
        while self._index < len(self._events):
            self._apply(dict(self._events[self._index].get("event") or {}))
            self._index += 1

    def toggle_pause(self) -> None:
        now = int(time.monotonic() * 1000)
        if self.paused:
            if self._paused_at_ms is not None:
                self._started_ms += now - self._paused_at_ms
            self._paused_at_ms = None
        else:
            self._paused_at_ms = now
        self.paused = not self.paused

    def set_speed(self, speed: float) -> None:
        new_speed = max(0.25, min(4.0, speed))
        now = self._paused_at_ms or int(time.monotonic() * 1000)
        elapsed = max(0, now - self._started_ms) * self.speed
        self._started_ms = now - int(elapsed / new_speed)
        self.speed = new_speed

    def _archive_summary(self, phase: str = "post-fix") -> ReviewArchiveSummary:
        archive = self._archive / "pre-fix" if phase == "pre-fix" else self._archive
        stage_log = archive / "stage_log.json"
        return _summary_from_log(stage_log, _load_json(stage_log))

    def dashboard(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            active=tuple(dict(item) for item in self._active.values()),
            queue=tuple(dict(item) for item in self._queue),
            health={"demo": {"ok": True, "detail": "synthetic replay; no network"}},
            recent=tuple(self._terminal),
        )

    def history(self, limit: int = 100) -> list[ReviewArchiveSummary]:
        if not self._terminal:
            return []
        return [self._archive_summary(), self._archive_summary("pre-fix")][:limit]

    def detail(self, review_id: str) -> ReviewArchiveDetail | None:
        expected = str(self.manifest.get("review_id") or "")
        post = self._archive_summary()
        pre = self._archive_summary("pre-fix")
        if review_id == expected and expected in self._active:
            active = self._active[expected]
            current_stage = str(active.get("stage") or "triage")
            in_fix = current_stage.startswith("fix-") or current_stage == "external-check"
            pre_detail = _detail_from_path(pre, self._archive / "pre-fix")
            post_detail = _detail_from_path(post, self._archive)
            base = post_detail if in_fix else pre_detail
            stage_names = [stage.name for stage in base.stages]
            current_index = (
                stage_names.index(current_stage)
                if current_stage in stage_names
                else max(0, len(stage_names) - 1)
            )
            visible_stages = tuple(
                replace(stage, status="running") if index == current_index else stage
                for index, stage in enumerate(base.stages[: current_index + 1])
            )
            summary = replace(
                base.summary,
                review_id=expected,
                head_sha=str(active.get("head_sha") or base.summary.head_sha),
                outcome="running",
                decision="pending",
                phase="auto-fix" if in_fix else "pre-fix",
                audit_flags=(),
            )
            if current_stage == "external-check":
                return replace(base, summary=summary, stages=visible_stages)
            visible_names = {stage.name for stage in visible_stages}
            evidence = dict(pre_detail.evidence)
            evidence.update(
                {
                    name: value
                    for name, value in post_detail.evidence.items()
                    if name.startswith("fix-") and name in visible_names
                }
            )
            return replace(
                base,
                summary=summary,
                stages=visible_stages,
                findings=pre_detail.findings,
                evidence=evidence,
            )
        if review_id in (expected, post.review_id):
            detail = _detail_from_path(post, self._archive)
            detail.evidence["lineage"] = {
                "parent_review_id": pre.review_id,
                "parent_sha": pre.head_sha,
                "parent_decision": pre.decision,
                "parent_findings_count": pre.findings_count,
                "post_fix_findings_count": post.findings_count,
            }
            return detail
        if review_id == pre.review_id:
            return _detail_from_path(pre, self._archive / "pre-fix")
        return None

    def validation_result(self) -> dict[str, Any]:
        expected = str(self.manifest.get("expected_outcome") or "")
        actual = "incomplete"
        if self._terminal:
            terminal = self._terminal[0]
            actual = (
                "blocked"
                if terminal.decision == "request_changes"
                or "external_check_contradiction" in terminal.audit_flags
                else terminal.outcome
            )
        return {
            "scenario": self.scenario,
            "outcome": actual,
            "network_calls": 0,
            "valid": self.complete and actual == expected,
        }
