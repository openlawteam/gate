"""Textual Flight Recorder screens shared by live history and demo replay."""

from __future__ import annotations

import json
import webbrowser

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from gate.review_store import (
    ReplayReviewDataSource,
    ReviewArchiveDetail,
    ReviewArchiveSummary,
    ReviewDataSource,
)


def _outcome_text(outcome: str) -> Text:
    styles = {
        "reviewed": "bright_green",
        "blocked": "bright_red bold",
        "error": "bright_red",
        "untrusted": "bright_yellow",
        "cancelled": "dim",
        "running": "bright_cyan",
    }
    icons = {
        "reviewed": "✓",
        "blocked": "!",
        "error": "✗",
        "untrusted": "⊘",
        "cancelled": "—",
        "running": "●",
    }
    return Text(f"{icons.get(outcome, '•')} {outcome}", style=styles.get(outcome, ""))


def _display_outcome(summary: ReviewArchiveSummary) -> str:
    """Derive a human terminal label without changing protocol-v1 outcome values."""
    if summary.outcome == "reviewed" and summary.decision == "request_changes":
        return "blocked"
    return summary.outcome


class FlightRecorderScreen(Screen):
    """Browse active and immutable historical review evidence."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("d", "back", "Dashboard"),
        Binding("q", "quit_recorder", "Quit"),
        Binding("r", "refresh_view", "Refresh"),
        Binding("enter", "open_review", "Open", priority=True),
        Binding("o", "open_pr", "Open PR"),
        Binding("n", "filter_repo", "Repo filter", show=False),
        Binding("f", "filter_outcome", "Outcome filter", show=False),
        Binding("p", "filter_phase", "Phase filter", show=False),
        Binding("x", "clear_filters", "Clear filters", show=False),
        Binding("question_mark", "show_help", "Help"),
        Binding("tab", "next_section", "Next section", show=False, priority=True),
        Binding("shift+tab", "previous_section", "Previous section", show=False, priority=True),
        Binding("space", "toggle_pause", "Pause", show=False, priority=True),
        Binding("left", "previous_event", "Previous", show=False, priority=True),
        Binding("right", "next_event", "Next", show=False, priority=True),
        Binding("plus", "faster", "Faster", show=False, priority=True),
        Binding("minus", "slower", "Slower", show=False, priority=True),
        Binding("home", "reset_replay", "Reset", show=False, priority=True),
        Binding("end", "finish_replay", "Finish", show=False, priority=True),
    ]

    CSS = """
    FlightRecorderScreen { layout: vertical; }
    #recorder-banner {
        height: 3;
        padding: 1 2 0 2;
        background: $surface;
        color: $accent;
        text-style: bold;
    }
    #recorder-filters {
        height: 2;
        padding: 0 2;
        background: $surface;
        color: $text-muted;
    }
    #recorder-main {
        height: 1fr;
        layout: grid;
        grid-size: 3 1;
        grid-columns: 1fr 1.25fr 1.5fr;
    }
    #recorder-main.standard {
        grid-size: 2 2;
        grid-columns: 1fr 1.5fr;
        grid-rows: 1fr 1fr;
    }
    #recorder-main.standard #recorder-evidence { column-span: 2; }
    #recorder-main.narrow {
        grid-size: 1 1;
        grid-columns: 1fr;
        grid-rows: 1fr;
    }
    #recorder-main.narrow #recorder-reviews,
    #recorder-main.narrow #recorder-timeline,
    #recorder-main.narrow #recorder-evidence { display: none; }
    #recorder-main.narrow.section-reviews #recorder-reviews,
    #recorder-main.narrow.section-timeline #recorder-timeline,
    #recorder-main.narrow.section-evidence #recorder-evidence { display: block; }
    .recorder-panel {
        height: 100%;
        border: solid $primary 25%;
        padding: 0 1;
    }
    .panel-title {
        height: 2;
        padding-top: 1;
        color: $primary;
        text-style: bold;
    }
    #recorder-list, #recorder-findings { height: 1fr; }
    #recorder-summary { height: auto; padding: 1 0; }
    #recorder-stages { height: auto; padding-bottom: 1; }
    #recorder-audit {
        height: auto;
        color: $warning;
        text-style: bold;
        padding-bottom: 1;
    }
    #recorder-evidence-body { padding: 0 1 1 1; }
    """

    def __init__(self, source: ReviewDataSource, *, demo: bool = False):
        super().__init__()
        self.source = source
        self.demo = demo
        self._row_ids: list[str] = []
        self._selected_id = ""
        self._repo_filter = ""
        self._outcome_filter = ""
        self._phase_filter = ""
        self._narrow_section = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("GATE  /  REVIEW FLIGHT RECORDER", id="recorder-banner")
        yield Static("FILTERS  repo: all  outcome: all  phase: all", id="recorder-filters")
        with Vertical(id="recorder-main"):
            with Vertical(classes="recorder-panel", id="recorder-reviews"):
                yield Static("REVIEWS", classes="panel-title")
                yield DataTable(id="recorder-list", cursor_type="row")
            with Vertical(classes="recorder-panel", id="recorder-timeline"):
                yield Static("TIMELINE", classes="panel-title")
                yield Static("Select a review", id="recorder-summary")
                yield Static("", id="recorder-stages")
                yield Static("", id="recorder-audit")
                yield DataTable(id="recorder-findings", cursor_type="row")
            with VerticalScroll(classes="recorder-panel", id="recorder-evidence"):
                yield Static("EVIDENCE", classes="panel-title")
                yield Static(
                    "Select a review to inspect its evidence.", id="recorder-evidence-body"
                )
        yield Footer()

    def on_mount(self) -> None:
        reviews = self.query_one("#recorder-list", DataTable)
        reviews.add_columns("Outcome", "Repo / PR", "Phase", "Decision", "Time")
        findings = self.query_one("#recorder-findings", DataTable)
        findings.add_columns("Severity", "Location", "Evidence")
        reviews.focus()
        self._apply_layout(self.size.width)
        self.action_refresh_view()
        if isinstance(self.source, ReplayReviewDataSource):
            self.set_interval(0.2, self._tick_replay)

    def _apply_layout(self, width: int) -> None:
        main = self.query_one("#recorder-main")
        main.set_class(width < 100, "narrow")
        main.set_class(100 <= width < 140, "standard")
        for name in ("reviews", "timeline", "evidence"):
            main.remove_class(f"section-{name}")
        if width < 100:
            main.add_class(f"section-{('reviews', 'timeline', 'evidence')[self._narrow_section]}")

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout(event.size.width)

    def _tick_replay(self) -> None:
        self.source.refresh()
        self._refresh_rows()
        if self._selected_id:
            self._show_detail(self._selected_id)

    def _active_summaries(self) -> list[ReviewArchiveSummary]:
        records: list[ReviewArchiveSummary] = []
        for review in self.source.dashboard().active:
            records.append(
                ReviewArchiveSummary(
                    review_id=str(review.get("id") or ""),
                    repo=str(review.get("repo") or ""),
                    pr_number=int(review.get("pr_number") or 0),
                    head_sha=str(review.get("head_sha") or ""),
                    completed_at="",
                    duration_s=None,
                    outcome=str(review.get("outcome") or "running"),
                    decision=str(review.get("decision") or "pending"),
                    phase="auto-fix" if review.get("status") == "fixing" else "pre-fix",
                    findings_count=int(review.get("findings_count") or 0),
                )
            )
        return records

    def _all_summaries(self) -> list[ReviewArchiveSummary]:
        active = self._active_summaries()
        active_ids = {item.review_id for item in active}
        items = active + [
            item for item in self.source.history(100) if item.review_id not in active_ids
        ]
        return [
            item
            for item in items
            if (not self._repo_filter or item.repo == self._repo_filter)
            and (not self._outcome_filter or item.outcome == self._outcome_filter)
            and (not self._phase_filter or item.phase == self._phase_filter)
        ]

    def _unfiltered_summaries(self) -> list[ReviewArchiveSummary]:
        active = self._active_summaries()
        active_ids = {item.review_id for item in active}
        return active + [
            item for item in self.source.history(100) if item.review_id not in active_ids
        ]

    def _refresh_filter_label(self) -> None:
        replay = self._replay()
        playback = ""
        if replay:
            playback = f"  •  {'PAUSED' if replay.paused else 'PLAYING'} {replay.speed:g}×"
        self.query_one("#recorder-filters", Static).update(
            "FILTERS  "
            f"repo: {self._repo_filter or 'all'}  "
            f"outcome: {self._outcome_filter or 'all'}  "
            f"phase: {self._phase_filter or 'all'}"
            f"{playback}  •  n/f/p cycle  x clears"
        )

    def _refresh_rows(self) -> None:
        self._refresh_filter_label()
        table = self.query_one("#recorder-list", DataTable)
        items = self._all_summaries()
        selected = self._selected_id
        table.clear()
        self._row_ids = []
        for item in items:
            duration = f"{item.duration_s:.1f}s" if item.duration_s is not None else "live"
            table.add_row(
                _outcome_text(_display_outcome(item)),
                f"{item.repo}  #{item.pr_number}",
                item.phase,
                item.decision.replace("_", " "),
                duration,
                key=item.review_id,
            )
            self._row_ids.append(item.review_id)
        if not items:
            table.add_row(Text("Waiting", style="dim"), "No review events yet", "", "", "")
        if selected in self._row_ids:
            table.move_cursor(row=self._row_ids.index(selected))
        elif self._row_ids:
            self._selected_id = self._row_ids[0]
            self._show_detail(self._selected_id)

    def _show_detail(self, review_id: str) -> None:
        detail = self.source.detail(review_id)
        if detail is None:
            return
        self._selected_id = review_id
        summary = detail.summary
        mode = "AUTO-FIX OPT-IN" if summary.phase in ("auto-fix", "post-fix") else "REVIEW ONLY"
        header = Text()
        if self.demo:
            header.append("SYNTHETIC DEMO  •  ", style="bright_magenta bold")
        header.append(f"{summary.repo}  PR #{summary.pr_number}\n", style="bold")
        header.append(f"{mode}  •  {summary.head_sha[:12]}  •  ")
        header.append(_outcome_text(_display_outcome(summary)))
        self.query_one("#recorder-summary", Static).update(header)

        stages = Text()
        for index, stage in enumerate(detail.stages):
            style = "bright_red" if stage.status == "error" else "bright_green"
            stages.append(
                f"{'✓' if stage.status == 'completed' else '!'} {stage.name}", style=style
            )
            if stage.elapsed_s is not None:
                stages.append(f" {stage.elapsed_s:g}s", style="dim")
            if index < len(detail.stages) - 1:
                stages.append("  →  ", style="dim")
        self.query_one("#recorder-stages", Static).update(
            stages or Text("No stage evidence yet", style="dim")
        )

        audit = ""
        if summary.audit_flags:
            audit = "⚠ AUDIT: " + ", ".join(flag.replace("_", " ") for flag in summary.audit_flags)
        self.query_one("#recorder-audit", Static).update(audit)

        table = self.query_one("#recorder-findings", DataTable)
        table.clear()
        for finding in detail.findings:
            location = str(finding.get("file") or "?")
            if finding.get("line"):
                location += f":{finding['line']}"
            table.add_row(
                str(finding.get("severity") or "info").upper(),
                location,
                str(finding.get("evidence_level") or "unknown"),
            )
        if not detail.findings:
            table.add_row("", "No findings recorded", "")

        evidence = Text()
        for finding in detail.findings:
            evidence.append(f"{finding.get('finding_id', 'finding')}\n", style="bright_cyan bold")
            evidence.append(f"{finding.get('message', '')}\n")
            if finding.get("evidence"):
                evidence.append(f"Evidence: {finding['evidence']}\n", style="bright_yellow")
            evidence.append("\n")
        evidence.append("RAW RECORDER DATA\n", style="dim bold")
        evidence.append(json.dumps(detail.evidence, indent=2, sort_keys=True)[:12000], style="dim")
        self.query_one("#recorder-evidence-body", Static).update(evidence)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "recorder-list":
            return
        index = event.cursor_row
        if 0 <= index < len(self._row_ids):
            self._show_detail(self._row_ids[index])

    def action_refresh_view(self) -> None:
        self.source.refresh()
        self._refresh_rows()

    def action_open_review(self) -> None:
        if not self._selected_id:
            return
        detail = self.source.detail(self._selected_id)
        if detail:
            self.app.push_screen(ReviewArchiveDetailScreen(detail, demo=self.demo))

    def action_open_pr(self) -> None:
        if not self._selected_id:
            return
        detail = self.source.detail(self._selected_id)
        if not detail:
            return
        if self.demo:
            self.app.notify("Synthetic demo: no browser or network opened")
            return
        summary = detail.summary
        webbrowser.open(f"https://github.com/{summary.repo}/pull/{summary.pr_number}")

    @staticmethod
    def _next_filter(current: str, values: list[str]) -> str:
        choices = [""] + sorted({value for value in values if value})
        try:
            return choices[(choices.index(current) + 1) % len(choices)]
        except ValueError:
            return ""

    def action_filter_repo(self) -> None:
        self._repo_filter = self._next_filter(
            self._repo_filter, [item.repo for item in self._unfiltered_summaries()]
        )
        self._selected_id = ""
        self._refresh_rows()

    def action_filter_outcome(self) -> None:
        self._outcome_filter = self._next_filter(
            self._outcome_filter, [item.outcome for item in self._unfiltered_summaries()]
        )
        self._selected_id = ""
        self._refresh_rows()

    def action_filter_phase(self) -> None:
        self._phase_filter = self._next_filter(
            self._phase_filter, [item.phase for item in self._unfiltered_summaries()]
        )
        self._selected_id = ""
        self._refresh_rows()

    def action_clear_filters(self) -> None:
        self._repo_filter = self._outcome_filter = self._phase_filter = ""
        self._selected_id = ""
        self._refresh_rows()

    def _move_narrow_section(self, direction: int) -> None:
        main = self.query_one("#recorder-main")
        if not main.has_class("narrow"):
            return
        self._narrow_section = (self._narrow_section + direction) % 3
        self._apply_layout(self.size.width)
        target = ("#recorder-list", "#recorder-findings", "#recorder-evidence-body")[
            self._narrow_section
        ]
        self.query_one(target).focus()

    def action_next_section(self) -> None:
        self._move_narrow_section(1)

    def action_previous_section(self) -> None:
        self._move_narrow_section(-1)

    def action_back(self) -> None:
        if self.demo:
            self.app.exit()
        else:
            self.dismiss()

    def action_quit_recorder(self) -> None:
        self.app.exit()

    def action_show_help(self) -> None:
        self.app.notify("d dashboard • Enter detail • n/f/p filters • x clear • r refresh • q quit")

    def _replay(self) -> ReplayReviewDataSource | None:
        return self.source if isinstance(self.source, ReplayReviewDataSource) else None

    def action_toggle_pause(self) -> None:
        if replay := self._replay():
            replay.toggle_pause()

    def action_previous_event(self) -> None:
        if replay := self._replay():
            replay.step(-1)
            self.action_refresh_view()

    def action_next_event(self) -> None:
        if replay := self._replay():
            replay.step(1)
            self.action_refresh_view()

    def action_faster(self) -> None:
        if replay := self._replay():
            replay.set_speed(replay.speed * 2)

    def action_slower(self) -> None:
        if replay := self._replay():
            replay.set_speed(replay.speed / 2)

    def action_reset_replay(self) -> None:
        if replay := self._replay():
            replay.reset()
            self._selected_id = ""
            self.action_refresh_view()

    def action_finish_replay(self) -> None:
        if replay := self._replay():
            replay.finish()
            self.action_refresh_view()


class ReviewArchiveDetailScreen(Screen):
    """Full-screen evidence view for one immutable review record."""

    BINDINGS = [Binding("escape", "back", "Back"), Binding("q", "back", "Back")]

    CSS = """
    ReviewArchiveDetailScreen { layout: vertical; }
    #detail-identity { height: auto; min-height: 4; padding: 1 2; background: $surface; }
    #detail-audit { height: auto; padding: 0 2 1 2; color: $warning; text-style: bold; }
    #detail-stages { height: auto; max-height: 7; padding: 1 2; border-bottom: solid $primary 25%; }
    #detail-findings { height: 12; }
    #detail-evidence { height: 1fr; padding: 1 2; }
    """

    def __init__(self, detail: ReviewArchiveDetail, *, demo: bool = False):
        super().__init__()
        self.detail = detail
        self.demo = demo
        self._finding_rows: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="detail-identity")
        yield Static(id="detail-audit")
        yield Static(id="detail-stages")
        yield DataTable(id="detail-findings", cursor_type="row")
        yield VerticalScroll(Static(id="detail-evidence"))
        yield Footer()

    def on_mount(self) -> None:
        summary = self.detail.summary
        identity = Text()
        if self.demo:
            identity.append("SYNTHETIC DEMO  •  ", style="bright_magenta bold")
        identity.append(f"{summary.repo}  PR #{summary.pr_number}\n", style="bold")
        identity.append(f"{summary.phase.upper()}  •  {summary.head_sha[:12]}  •  ")
        identity.append(_outcome_text(_display_outcome(summary)))
        if summary.parent_review_id:
            identity.append(
                f"\nLINEAGE  {summary.parent_review_id}  →  {summary.review_id}", style="cyan"
            )
        self.query_one("#detail-identity", Static).update(identity)

        audit = ""
        if summary.audit_flags:
            audit = "⚠ AUDIT CONTRADICTION  •  " + ", ".join(
                flag.replace("_", " ") for flag in summary.audit_flags
            )
        self.query_one("#detail-audit", Static).update(audit)

        stages = Text("STAGE TIMELINE\n", style="bold")
        for index, stage in enumerate(self.detail.stages):
            style = "bright_red" if stage.status == "error" else "bright_green"
            stages.append(f"{'!' if stage.status == 'error' else '✓'} {stage.name}", style=style)
            if stage.elapsed_s is not None:
                stages.append(f" {stage.elapsed_s:g}s", style="dim")
            if index < len(self.detail.stages) - 1:
                stages.append("  →  ", style="dim")
        self.query_one("#detail-stages", Static).update(stages)

        table = self.query_one("#detail-findings", DataTable)
        table.add_columns("Severity", "Category", "File", "Line", "Evidence")
        self._finding_rows = list(self.detail.findings)
        for finding in self._finding_rows:
            table.add_row(
                str(finding.get("severity") or "info").upper(),
                str(finding.get("category") or finding.get("rule_source") or "—"),
                str(finding.get("file") or "—"),
                str(finding.get("line") or "—"),
                str(finding.get("evidence_level") or "unknown"),
            )
        if not self._finding_rows:
            table.add_row("", "", "No findings recorded", "", "")
        self._show_evidence(0)

    def _show_evidence(self, finding_index: int) -> None:
        body = Text()
        if 0 <= finding_index < len(self._finding_rows):
            finding = self._finding_rows[finding_index]
            body.append("SELECTED FINDING\n", style="bright_cyan bold")
            body.append(f"{finding.get('message', '')}\n")
            if finding.get("evidence"):
                body.append(f"Evidence: {finding['evidence']}\n", style="bright_yellow")
            if finding.get("suggestion"):
                body.append(f"Suggestion: {finding['suggestion']}\n", style="green")
            body.append("\n")
        body.append("BUILD, FIX, AUDIT, AND RAW EVIDENCE\n", style="dim bold")
        body.append(json.dumps(self.detail.evidence, indent=2, sort_keys=True)[:24000], style="dim")
        if self.detail.activity:
            body.append("\n\nACTIVITY EXCERPT\n", style="dim bold")
            body.append("\n".join(self.detail.activity), style="dim")
        self.query_one("#detail-evidence", Static).update(body)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "detail-findings":
            self._show_evidence(event.cursor_row)

    def action_back(self) -> None:
        self.dismiss()


class GateDemoTUI(App):
    """Standalone zero-credential replay app."""

    TITLE = "gate demo"

    def __init__(self, source: ReplayReviewDataSource):
        super().__init__()
        self.source = source

    def on_mount(self) -> None:
        from gate.tui import GATE_THEME

        self.register_theme(GATE_THEME)
        self.theme = "gate"
        self.push_screen(FlightRecorderScreen(self.source, demo=True))


def run_demo_tui(source: ReplayReviewDataSource) -> int:
    """Run the interactive deterministic Flight Recorder replay."""
    GateDemoTUI(source).run()
    return 0
