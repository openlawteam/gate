"""Textual TUI dashboard for Gate.

Full-featured monitoring dashboard with:
- Active reviews with stage pipeline visualization
- Queue, recent reviews, system info, health, log tail
- Modal screens for review detail, completed review detail, help/legend
- Tokyo Night color scheme via Textual Theme API
- Rich Text colored cells for status, stage, decision
- Incremental table updates (no flicker, preserves cursor)
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Header, RichLog, Static

from gate import __version__
from gate.config import get_all_repos, load_config, repo_slug
from gate.logger import live_dir, logs_dir, reviews_jsonl

# ── Tokyo Night Theme ────────────────────────────────────────

GATE_THEME = Theme(
    name="gate",
    primary="#7aa2f7",
    secondary="#bb9af7",
    accent="#7dcfff",
    foreground="#c0caf5",
    background="#1a1b26",
    surface="#24283b",
    panel="#1f2335",
    success="#9ece6a",
    warning="#e0af68",
    error="#f7768e",
    dark=True,
    variables={
        "footer-key-foreground": "#7aa2f7",
        "footer-description-foreground": "#565f89",
    },
)

# ── Constants ────────────────────────────────────────────────

REVIEW_STAGES = ["triage", "build", "architecture", "security", "logic", "verdict"]
FIX_STAGES = ["fix-bootstrap", "fix-session", "fix-build", "fix-rereview", "fix-commit"]

STATUS_ICONS = {
    "running": "●",
    "completed": "✓",
    "error": "✗",
    "cancelled": "⊘",
    "stuck": "◷",
    "queued": "⋯",
    "fixing": "⚒",
}

STATUS_COLORS = {
    "running": "bright_green",
    "completed": "bright_green",
    "error": "bright_red",
    "cancelled": "dim",
    "stuck": "bright_yellow",
    "queued": "bright_black",
    "fixing": "bright_yellow",
}

DECISION_ICONS = {
    "approve": "✓",
    "approve_with_notes": "✓~",
    "request_changes": "✗",
    "error": "!",
    "skip": "—",
    "fix_succeeded": "⚒✓",
    "fix_failed": "⚒✗",
}

DECISION_COLORS = {
    "approve": "bright_green",
    "approve_with_notes": "bright_yellow",
    "request_changes": "bright_red",
    "error": "bright_red",
    "skip": "dim",
    "fix_succeeded": "bright_green",
    "fix_failed": "bright_yellow",
}

STAGE_COLORS = {
    "triage": "bright_cyan",
    "build": "bright_blue",
    "architecture": "bright_magenta",
    "security": "bright_red",
    "logic": "bright_yellow",
    "verdict": "bright_green",
    "fix-bootstrap": "bright_yellow",
    "fix-session": "bright_yellow",
    "fix-build": "bright_yellow",
    "fix-rereview": "bright_yellow",
    "fix-commit": "bright_yellow",
    "fix-senior": "bright_yellow",
}

LOG_PREFIX_COLORS = {
    "orchestrator": "bright_blue",
    "stage": "bright_cyan",
    "setup": "dim",
    "gate": "bright_magenta",
    "fix": "bright_yellow",
}


# ── Helpers ──────────────────────────────────────────────────


def _short_repo(repo: str) -> str:
    """Extract short repo name: 'org/repo' -> 'repo'."""
    return repo.split("/")[-1] if "/" in repo else repo


# ── Rich Text Formatters ────────────────────────────────────


def format_status(status: str) -> Text:
    icon = STATUS_ICONS.get(status, "?")
    color = STATUS_COLORS.get(status, "")
    return Text(icon, style=color)


def format_status_label(status: str) -> Text:
    color = STATUS_COLORS.get(status, "")
    return Text(status, style=color)


def format_stage(stage: str) -> Text:
    color = STAGE_COLORS.get(stage, "")
    return Text(stage, style=color)


def format_decision(decision: str) -> Text:
    icon = DECISION_ICONS.get(decision, "?")
    color = DECISION_COLORS.get(decision, "")
    label = decision.replace("_", " ")
    return Text(f"{icon} {label}", style=color)


def format_decision_icon(decision: str) -> Text:
    icon = DECISION_ICONS.get(decision, "?")
    color = DECISION_COLORS.get(decision, "")
    return Text(icon, style=color)


def format_pipeline(current_stage: str) -> Text:
    text = Text()
    idx = REVIEW_STAGES.index(current_stage) if current_stage in REVIEW_STAGES else -1
    for i, stage in enumerate(REVIEW_STAGES):
        abbrev = stage[:3]
        if i < idx:
            text.append(f" {abbrev} ", style="bright_green")
        elif i == idx:
            text.append(f"[{abbrev}]", style="bright_cyan bold")
        else:
            text.append(f" {abbrev} ", style="dim")
        if i < len(REVIEW_STAGES) - 1:
            text.append(">", style="dim")
    return text


def format_fix_pipeline(current_stage: str) -> Text:
    """Format fix pipeline visualization, same structure as format_pipeline."""
    text = Text()
    mapped = "fix-session" if current_stage == "fix-senior" else current_stage
    idx = FIX_STAGES.index(mapped) if mapped in FIX_STAGES else -1
    for i, stage in enumerate(FIX_STAGES):
        abbrev = stage.replace("fix-", "")[:3]
        if i < idx:
            text.append(f" {abbrev} ", style="bright_green")
        elif i == idx:
            text.append(f"[{abbrev}]", style="bright_yellow bold")
        else:
            text.append(f" {abbrev} ", style="dim")
        if i < len(FIX_STAGES) - 1:
            text.append(">", style="dim")
    return text


def format_elapsed(started_ms: int) -> str:
    if not started_ms:
        return "—"
    elapsed = int((time.time() * 1000 - started_ms) / 1000)
    if elapsed < 0:
        return "—"
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        return f"{elapsed // 60}m{elapsed % 60:02d}s"
    return f"{elapsed // 3600}h{(elapsed % 3600) // 60:02d}m"


def format_uptime(started_ms: int) -> str:
    if not started_ms:
        return "—"
    elapsed = int((time.time() * 1000 - started_ms) / 1000)
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        return f"{elapsed // 60}m"
    hours = elapsed // 3600
    mins = (elapsed % 3600) // 60
    if hours < 24:
        return f"{hours}h{mins:02d}m"
    days = hours // 24
    return f"{days}d{hours % 24}h"


def format_log_line(line: str) -> Text:
    text = Text()
    for prefix, color in LOG_PREFIX_COLORS.items():
        if f"[{prefix}]" in line:
            bracket_start = line.index(f"[{prefix}]")
            text.append(line[:bracket_start], style="dim")
            text.append(f"[{prefix}]", style=color)
            rest = line[bracket_start + len(prefix) + 2 :]
            if "FAILED" in rest or "ERROR" in rest or "error" in rest.lower():
                text.append(rest, style="bright_red")
            else:
                text.append(rest)
            return text
    if "ERROR" in line or "FAILED" in line:
        return Text(line, style="bright_red")
    if "WARNING" in line:
        return Text(line, style="bright_yellow")
    return Text(line)


# ── Data Helpers ─────────────────────────────────────────────


def read_recent_reviews(count: int = 8) -> list[dict]:
    path = reviews_jsonl()
    if not path.exists():
        return []
    try:
        lines = path.read_text().strip().split("\n")
        entries = []
        for line in reversed(lines[-count * 2 :]):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= count:
                break
        return entries
    except OSError:
        return []


def _parse_timestamp(ts) -> float:
    """Convert a timestamp (epoch number or ISO string) to epoch seconds."""
    if isinstance(ts, (int, float)):
        return float(ts) if ts > 1e9 else 0.0
    if isinstance(ts, str):
        from datetime import datetime, timezone

        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(ts, fmt).timestamp()
            except (ValueError, TypeError):
                continue
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc).timestamp()
            except (ValueError, TypeError):
                continue
    return 0.0


def compute_metrics() -> dict:
    path = reviews_jsonl()
    if not path.exists():
        return {"total": 0, "approved_pct": 0, "error_pct": 0, "avg_time": 0}
    try:
        cutoff = time.time() - 86400
        lines = path.read_text().strip().split("\n")
        total = approved = errors = 0
        durations: list[int] = []
        total_findings = 0
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                ts = _parse_timestamp(entry.get("timestamp", 0))
                if ts == 0:
                    continue
                if ts < cutoff:
                    break
                total += 1
                decision = entry.get("decision", "")
                if decision in ("approve", "approve_with_notes"):
                    approved += 1
                elif decision == "error":
                    errors += 1
                dur = entry.get("review_time_seconds", 0)
                if isinstance(dur, (int, float)) and dur > 0:
                    durations.append(int(dur))
                f = entry.get("findings", 0)
                total_findings += f if isinstance(f, (int, float)) else 0
            except json.JSONDecodeError:
                continue
        return {
            "total": total,
            "approved_pct": int(approved / total * 100) if total else 0,
            "error_pct": int(errors / total * 100) if total else 0,
            "avg_time": int(sum(durations) / len(durations)) if durations else 0,
            "avg_findings": round(total_findings / total, 1) if total else 0,
        }
    except OSError:
        return {"total": 0, "approved_pct": 0, "error_pct": 0, "avg_time": 0}


# ── Modal Screens ────────────────────────────────────────────


class HelpScreen(ModalScreen):
    """Help and legend modal."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close", show=False),
    ]

    CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-container {
        width: 64;
        height: auto;
        max-height: 85%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #help-title {
        text-align: center;
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }
    #help-body {
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-container"):
            yield Static("Gate — Help & Legend", id="help-title")
            with VerticalScroll():
                yield Static(self._build_content(), id="help-body")

    def _build_content(self) -> Text:
        t = Text()

        t.append("Keybindings\n", style="bold underline")
        keys = [
            ("q", "Quit"),
            ("r / F5", "Refresh all panels"),
            ("Enter", "Detail for selected review"),
            ("c", "Cancel selected active review"),
            ("l", "Toggle log tail visibility"),
            ("o", "Open selected PR in browser"),
            ("?", "Show this help screen"),
            ("Tab", "Cycle focus between tables"),
            ("Esc", "Dismiss modal / go back"),
        ]
        for key, desc in keys:
            t.append(f"  {key:<10}", style="bright_cyan")
            t.append(f" {desc}\n")

        t.append("\n")
        t.append("Review Status Icons\n", style="bold underline")
        for status, icon in STATUS_ICONS.items():
            color = STATUS_COLORS.get(status, "")
            t.append(f"  {icon}  ", style=color)
            t.append(f"{status}\n")

        t.append("\n")
        t.append("Decision Icons\n", style="bold underline")
        for decision, icon in DECISION_ICONS.items():
            color = DECISION_COLORS.get(decision, "")
            t.append(f"  {icon}  ", style=color)
            t.append(f"{decision.replace('_', ' ')}\n")

        t.append("\n")
        t.append("Stage Colors\n", style="bold underline")
        for stage in REVIEW_STAGES:
            color = STAGE_COLORS.get(stage, "")
            t.append(f"  {stage}\n", style=color)

        t.append("\n")
        t.append("Pipeline Visualization\n", style="bold underline")
        t.append("  Review: ")
        t.append(format_pipeline("architecture"))
        t.append("\n  Fix:    ")
        t.append(format_fix_pipeline("fix-session"))
        t.append("\n  Completed stages are green, current is highlighted, pending is dim.\n")

        return t


class ReviewDetailScreen(ModalScreen):
    """Detail modal for an active review."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    CSS = """
    ReviewDetailScreen {
        align: center middle;
    }
    #detail-container {
        width: 76;
        height: auto;
        max-height: 85%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #detail-title {
        text-align: center;
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }
    #detail-body {
        padding: 0 1;
    }
    #detail-pane {
        height: 16;
        background: $background;
        border: solid $panel;
        padding: 0 1;
        margin-top: 1;
    }
    #detail-buttons {
        height: auto;
        align: center middle;
        padding-top: 1;
    }
    #detail-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, review: dict, server=None):
        super().__init__()
        self._review = review
        self._server = server

    def compose(self) -> ComposeResult:
        r = self._review
        pr = r.get("pr_number", "?")
        with Vertical(id="detail-container"):
            yield Static(f"Review Detail — PR #{pr}", id="detail-title")
            yield Static(self._build_info(), id="detail-body")
            yield RichLog(id="detail-pane", max_lines=40, wrap=True)
            with Horizontal(id="detail-buttons"):
                yield Button("Close", id="btn-close", variant="primary")
                yield Button("Cancel Review", id="btn-cancel", variant="error")

    def on_mount(self) -> None:
        self._refresh_pane()
        self.query_one("#btn-close").focus()

    def _build_info(self) -> Text:
        r = self._review
        t = Text()
        t.append("PR:     ", style="dim")
        t.append(f"#{r.get('pr_number', '?')}\n")
        t.append("Repo:   ", style="dim")
        t.append(f"{r.get('repo', '?')}\n")
        t.append("SHA:    ", style="dim")
        t.append(f"{r.get('head_sha', '?')[:12]}\n")
        t.append("Status: ", style="dim")
        t.append(format_status_label(r.get("status", "?")))
        t.append("\n")
        t.append("Stage:  ", style="dim")
        t.append(format_stage(r.get("stage", "?")))
        t.append("\n\n")
        t.append("Pipeline:\n  ")
        if r.get("status") == "fixing":
            t.append(format_fix_pipeline(r.get("stage", "")))
        else:
            t.append(format_pipeline(r.get("stage", "")))
        t.append("\n")
        pane = r.get("tmux_pane", "")
        pid = r.get("pid", "")
        if pane:
            t.append(f"\nPane: {pane}", style="dim")
        if pid:
            t.append(f"  PID: {pid}", style="dim")
        return t

    def _refresh_pane(self) -> None:
        pane_id = self._review.get("tmux_pane", "")
        log_widget = self.query_one("#detail-pane", RichLog)
        if pane_id:
            from gate.tmux import capture_pane

            content = capture_pane(pane_id)
            if content:
                lines = content.rstrip("\n").split("\n")
                while lines and not lines[-1].strip():
                    lines.pop()
                for line in lines[-35:]:
                    log_widget.write(line)
                return
        log_widget.write(Text("No tmux pane attached to this review.", style="dim"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close":
            self.dismiss()
        elif event.button.id == "btn-cancel":
            review_id = self._review.get("id", "")
            if review_id and self._server:
                self._server.enqueue(
                    {"type": "review_cancelled", "review_id": review_id}
                )
                self.notify(f"Cancel requested for {review_id}")
            self.dismiss()

    def on_key(self, event) -> None:
        focused = self.focused
        buttons = list(self.query("#detail-buttons Button"))
        if event.key == "right" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx + 1) % len(buttons)].focus()
        elif event.key == "left" and focused in buttons:
            event.prevent_default()
            event.stop()
            idx = buttons.index(focused)
            buttons[(idx - 1) % len(buttons)].focus()


class CompletedDetailScreen(ModalScreen):
    """Detail modal for a completed review from reviews.jsonl."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    CSS = """
    CompletedDetailScreen {
        align: center middle;
    }
    #completed-container {
        width: 64;
        height: auto;
        max-height: 85%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #completed-title {
        text-align: center;
        text-style: bold;
        color: $text;
        padding-bottom: 1;
    }
    #completed-body {
        padding: 0 1;
    }
    #completed-buttons {
        height: auto;
        align: center middle;
        padding-top: 1;
    }
    """

    def __init__(self, entry: dict):
        super().__init__()
        self._entry = entry

    def compose(self) -> ComposeResult:
        pr = self._entry.get("pr", self._entry.get("pr_number", "?"))
        with Vertical(id="completed-container"):
            yield Static(f"Review Result — PR #{pr}", id="completed-title")
            with VerticalScroll():
                yield Static(self._build_info(), id="completed-body")
            with Horizontal(id="completed-buttons"):
                yield Button("Close", id="btn-close", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#btn-close").focus()

    def _build_info(self) -> Text:
        e = self._entry
        t = Text()

        repo = e.get("repo", "")
        if repo:
            t.append("Repo:       ", style="dim")
            t.append(f"{repo}\n")

        if e.get("is_fix_followup"):
            return self._build_fix_info(e, t)

        decision = e.get("decision", "?")
        t.append("Decision:   ", style="dim")
        t.append(format_decision(decision))
        t.append("\n")
        t.append("Confidence: ", style="dim")
        t.append(f"{e.get('confidence', '?')}\n")
        t.append("Risk:       ", style="dim")
        t.append(f"{e.get('risk_level', '?')}\n")
        t.append("Duration:   ", style="dim")
        dur = e.get("review_time_seconds", "?")
        t.append(f"{dur}s\n" if isinstance(dur, (int, float)) else "?\n")
        t.append("Stages:     ", style="dim")
        t.append(f"{e.get('stages_run', '?')}\n")
        t.append("Build:      ", style="dim")
        build_ok = e.get("build_pass", True)
        build_style = "bright_green" if build_ok else "bright_red"
        t.append(
            "pass\n" if build_ok else "fail\n", style=build_style,
        )
        t.append("Fast-track: ", style="dim")
        t.append(f"{'yes' if e.get('fast_track_eligible') else 'no'}\n")

        t.append("\n")
        t.append("Findings\n", style="bold underline")
        total = e.get("findings", 0)
        severity = e.get("findings_by_severity", {})
        t.append(f"  Total:    {total}\n")
        crit = severity.get("critical", 0)
        errs = severity.get("error", 0)
        warns = severity.get("warning", 0)
        infos = severity.get("info", 0)
        if crit:
            t.append(f"  Critical: {crit}\n", style="bright_red bold")
        if errs:
            t.append(f"  Error:    {errs}\n", style="bright_red")
        if warns:
            t.append(f"  Warning:  {warns}\n", style="bright_yellow")
        if infos:
            t.append(f"  Info:     {infos}\n")
        t.append(f"  Resolved: {e.get('resolved_count', 0)}\n")

        categories = e.get("finding_categories", [])
        if categories:
            t.append("\n")
            t.append("Categories: ", style="dim")
            t.append(", ".join(categories) + "\n")

        ts = e.get("timestamp", "")
        if ts:
            t.append(f"\nTimestamp: {ts}\n", style="dim")

        return t

    def _build_fix_info(self, e: dict, t: Text) -> Text:
        """Render fix-specific fields for CompletedDetailScreen."""
        decision = e.get("decision", "?")
        t.append("Decision:   ", style="dim")
        t.append(format_decision(decision))
        t.append("\n")
        t.append("Original:   ", style="dim")
        t.append(f"{e.get('original_decision', '?')}\n")
        dur = e.get("review_time_seconds", "?")
        t.append("Duration:   ", style="dim")
        t.append(f"{dur}s\n" if isinstance(dur, (int, float)) else "?\n")

        t.append("\n")
        t.append("Fix Summary\n", style="bold underline")
        t.append(f"  {e.get('fix_summary', 'No summary')}\n")

        ts = e.get("timestamp", "")
        if ts:
            t.append(f"\nTimestamp: {ts}\n", style="dim")
        return t

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


# ── Main App ─────────────────────────────────────────────────


class GateTUI(App):
    """Gate monitoring TUI application."""

    TITLE = "gate"

    CSS = """
    Screen {
        layout: vertical;
    }

    #main {
        height: 1fr;
    }

    #left-panel {
        width: 2fr;
    }

    #right-panel {
        width: 1fr;
        border-left: solid $primary 20%;
        padding: 0 1;
    }

    .section-label {
        text-style: bold;
        padding: 0 1;
        color: $primary;
        margin-top: 1;
    }

    .section-label-first {
        text-style: bold;
        padding: 0 1;
        color: $primary;
    }

    .subsection-label {
        text-style: bold;
        padding: 0 1;
        color: $secondary;
        margin-top: 1;
    }

    .info-text {
        padding: 0 2;
        color: $text;
    }

    .dim-text {
        padding: 0 2;
        color: $text 60%;
    }

    DataTable {
        height: auto;
        max-height: 12;
    }

    DataTable > .datatable--header {
        text-style: bold;
    }

    DataTable:focus > .datatable--cursor {
        background: $accent 15%;
    }

    DataTable > .datatable--cursor {
        background: $surface 30%;
    }

    #reviews-table {
        max-height: 8;
    }

    #queue-table {
        max-height: 5;
    }

    #recent-table {
        max-height: 10;
    }

    #log-container {
        height: 1fr;
        border-top: solid $primary 20%;
    }

    #log-label {
        text-style: bold;
        padding: 0 1;
        color: $primary;
        height: 1;
    }

    #log-panes {
        height: 1fr;
    }

    .log-pane-container {
        width: 1fr;
        height: 100%;
    }

    .log-pane-title {
        height: 1;
        padding: 0 1;
        color: $accent;
        text-style: bold;
    }

    .log-pane-widget {
        height: 1fr;
        padding: 0 1;
    }

    .log-pane-separator {
        width: 1;
        height: 100%;
        background: $primary 20%;
    }

    #system-info {
        padding: 0 1;
    }

    #health-content {
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("f5", "refresh", "Refresh", show=False),
        Binding("c", "cancel_review", "Cancel"),
        Binding("enter", "detail", "Detail"),
        Binding("l", "toggle_log", "Log"),
        Binding("o", "open_pr", "Open PR"),
        Binding("question_mark", "help", "Help"),
        Binding("tab", "focus_next", "Next", show=False),
        Binding("shift+tab", "focus_previous", "Prev", show=False),
    ]

    def __init__(self, server=None):
        super().__init__()
        self.server = server
        self._log_visible = True
        self._log_panes: dict[str, dict] = {}
        self._active_row_keys: dict[str, object] = {}
        self._recent_entries: list[dict] = []
        self._window_title = "gate"
        self._last_jsonl_mtime: float = 0.0
        self._config: dict = load_config()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left-panel"):
                yield Static("Active Reviews", classes="section-label-first")
                yield DataTable(id="reviews-table", cursor_type="row")
                yield Static("Queue", classes="section-label")
                yield DataTable(id="queue-table", cursor_type="none")
                yield Static("Recent Reviews", classes="section-label")
                yield DataTable(id="recent-table", cursor_type="row")
                with Vertical(id="log-container"):
                    yield Static("Log", id="log-label")
                    yield Horizontal(id="log-panes")
            with Vertical(id="right-panel"):
                yield Static("System", classes="section-label-first")
                yield Static("", id="system-info")
                yield Static("24h Metrics", classes="subsection-label")
                yield Static("", id="metrics-info")
                yield Static("Health", classes="subsection-label")
                yield Static("", id="health-content")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(GATE_THEME)
        self.theme = "gate"

        reviews_table = self.query_one("#reviews-table", DataTable)
        self._reviews_cols = reviews_table.add_columns(
            "", "Repo", "PR", "Stage", "Pipeline", "Status", "Elapsed",
        )

        queue_table = self.query_one("#queue-table", DataTable)
        queue_table.add_columns("#", "PR", "Repo")

        recent_table = self.query_one("#recent-table", DataTable)
        recent_table.add_columns("", "Repo", "PR", "Decision", "Findings", "Time")

        reviews_table.focus()

        self._update_title()
        self.set_interval(3.0, self._poll_server)
        self.set_interval(10.0, self._update_recent_and_metrics)
        self._update_recent_and_metrics()
        self._update_system_info()
        self._poll_log()

    # ── Polling ──────────────────────────────────────────────

    def _poll_server(self) -> None:
        if not self.server:
            return
        self._refresh_reviews_table()
        self._refresh_queue_table()
        self._refresh_health()
        self._update_title()
        self._update_system_info()
        self._poll_log()

    def _update_recent_and_metrics(self) -> None:
        try:
            mtime = reviews_jsonl().stat().st_mtime
        except OSError:
            return
        if mtime == self._last_jsonl_mtime:
            return
        self._last_jsonl_mtime = mtime
        self._recent_entries = read_recent_reviews(8)
        self._refresh_recent_table()
        self._refresh_metrics()

    # ── Active Reviews Table (incremental) ───────────────────

    def _refresh_reviews_table(self) -> None:
        table = self.query_one("#reviews-table", DataTable)
        reviews = self.server.reviews if self.server else []
        current_ids = {r.get("id", "") for r in reviews}
        tracked_ids = set(self._active_row_keys.keys())

        for rid in tracked_ids - current_ids:
            row_key = self._active_row_keys.pop(rid, None)
            if row_key is not None:
                try:
                    table.remove_row(row_key)
                except Exception:
                    pass

        cols = self._reviews_cols
        for r in reviews:
            rid = r.get("id", "")
            status = r.get("status", "")
            stage = r.get("stage", "")
            pr = str(r.get("pr_number", "?"))
            repo_short = _short_repo(r.get("repo", ""))
            elapsed = format_elapsed(r.get("started_at", 0))

            pipeline = format_fix_pipeline(stage) if status == "fixing" else format_pipeline(stage)

            if rid in self._active_row_keys:
                row_key = self._active_row_keys[rid]
                try:
                    table.update_cell(row_key, cols[0], format_status(status))
                    table.update_cell(row_key, cols[1], repo_short)
                    table.update_cell(row_key, cols[2], pr)
                    table.update_cell(row_key, cols[3], format_stage(stage))
                    table.update_cell(row_key, cols[4], pipeline)
                    table.update_cell(row_key, cols[5], format_status_label(status))
                    table.update_cell(row_key, cols[6], elapsed)
                except Exception:
                    pass
            else:
                row_key = table.add_row(
                    format_status(status),
                    repo_short,
                    pr,
                    format_stage(stage),
                    pipeline,
                    format_status_label(status),
                    elapsed,
                )
                self._active_row_keys[rid] = row_key

    # ── Queue Table ──────────────────────────────────────────

    def _refresh_queue_table(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        queue = self.server.review_queue if self.server else []
        table.clear()
        if queue:
            for i, item in enumerate(queue, 1):
                table.add_row(
                    str(i),
                    str(item.get("pr_number", "?")),
                    item.get("repo", ""),
                )
        else:
            table.add_row("", "", Text("Queue empty", style="dim"))

    # ── Recent Reviews Table ─────────────────────────────────

    def _refresh_recent_table(self) -> None:
        table = self.query_one("#recent-table", DataTable)
        table.clear()
        if self._recent_entries:
            for e in self._recent_entries:
                decision = e.get("decision", "?")
                findings = e.get("findings", e.get("total_findings", 0))
                if isinstance(findings, dict):
                    findings = 0
                elapsed = e.get("review_time_seconds", e.get("elapsed_s", 0))
                elapsed_str = f"{elapsed}s" if isinstance(elapsed, (int, float)) else "?"
                pr = str(e.get("pr", e.get("pr_number", "?")))
                repo_short = _short_repo(e.get("repo", ""))
                table.add_row(
                    format_decision_icon(decision),
                    repo_short,
                    pr,
                    format_decision(decision),
                    str(findings),
                    elapsed_str,
                )
        else:
            table.add_row(
                Text("", style="dim"),
                "",
                "",
                Text("No reviews yet", style="dim"),
                "",
                "",
            )

    # ── System Info Panel ────────────────────────────────────

    def _update_system_info(self) -> None:
        t = Text()

        uptime = "—"
        if self.server:
            uptime = format_uptime(self.server.started_at)
        t.append("Server:     ", style="dim")
        t.append(f"up {uptime}\n", style="bright_green")

        t.append("Version:    ", style="dim")
        t.append(f"v{__version__}\n")

        t.append("PID:        ", style="dim")
        t.append(f"{os.getpid()}\n")

        wt_count = 0
        seen_bases: set[Path] = set()
        for repo_cfg in get_all_repos(self._config):
            wt_base = Path(repo_cfg.get("worktree_base", "/tmp/gate-worktrees"))
            if wt_base not in seen_bases and wt_base.exists():
                seen_bases.add(wt_base)
                wt_count += sum(1 for p in wt_base.iterdir() if p.is_dir())
        t.append("Worktrees:  ", style="dim")
        t.append(f"{wt_count}\n")

        try:
            usage = shutil.disk_usage("/")
            free_gb = usage.free / (1024**3)
            t.append("Disk free:  ", style="dim")
            if free_gb > 10:
                color = "bright_green"
            elif free_gb > 2:
                color = "bright_yellow"
            else:
                color = "bright_red"
            t.append(f"{free_gb:.0f} GB\n", style=color)
        except OSError:
            pass

        try:
            self.query_one("#system-info", Static).update(t)
        except Exception:
            pass

    # ── Metrics Panel ────────────────────────────────────────

    def _refresh_metrics(self) -> None:
        m = compute_metrics()
        t = Text()
        total = m.get("total", 0)
        if total == 0:
            t.append("No reviews in last 24h", style="dim")
        else:
            t.append("Reviews:    ", style="dim")
            t.append(f"{total}\n")
            t.append("Approved:   ", style="dim")
            t.append(f"{m['approved_pct']}%\n", style="bright_green")
            t.append("Errors:     ", style="dim")
            err_pct = m.get("error_pct", 0)
            t.append(f"{err_pct}%\n", style="bright_green" if err_pct == 0 else "bright_red")
            t.append("Avg time:   ", style="dim")
            avg = m.get("avg_time", 0)
            t.append(f"{avg}s\n" if avg else "—\n")
            avg_f = m.get("avg_findings", 0)
            t.append("Avg finds:  ", style="dim")
            t.append(f"{avg_f}\n")
        try:
            self.query_one("#metrics-info", Static).update(t)
        except Exception:
            pass

    # ── Health Panel ─────────────────────────────────────────

    def _refresh_health(self) -> None:
        from gate.config import socket_path as _socket_path

        health = self.server.health if self.server else {}
        t = Text()
        if not health:
            sock = _socket_path()
            if sock.exists():
                t.append("✓ ", style="bright_green")
                t.append("Server socket\n")
            else:
                t.append("✗ ", style="bright_red")
                t.append("Server socket missing\n")
            t.append("  Run ", style="dim")
            t.append("gate health", style="bright_cyan")
            t.append(" for full check", style="dim")
        else:
            for key, val in health.items():
                if isinstance(val, dict):
                    ok = val.get("ok", val.get("pass", True))
                    icon = "✓" if ok else "✗"
                    color = "bright_green" if ok else "bright_red"
                    detail = val.get("detail", val.get("message", ""))
                    t.append(f"{icon} ", style=color)
                    t.append(f"{key}")
                    if detail:
                        t.append(f": {detail}", style="dim")
                    t.append("\n")
                else:
                    t.append(f"  {key}: {val}\n", style="dim")
        try:
            self.query_one("#health-content", Static).update(t)
        except Exception:
            pass

    # ── Log Tail (multi-pane) ────────────────────────────────

    def _poll_log(self) -> None:
        if not self._log_visible:
            return

        label_widget = self.query_one("#log-label", Static)
        panes_container = self.query_one("#log-panes", Horizontal)

        active_reviews: list[tuple[str, int]] = []
        if self.server and self.server.reviews:
            active_reviews = [
                (r.get("repo", ""), r.get("pr_number"))
                for r in self.server.reviews
                if r.get("pr_number")
            ]

        desired_keys: set[str] = set()
        for repo, pr in active_reviews:
            slug = repo_slug(repo) if repo else ""
            key = f"{slug}:{pr}" if slug else str(pr)
            desired_keys.add(key)
        if not desired_keys:
            desired_keys = {"activity"}
        current_keys = set(self._log_panes.keys())

        if current_keys != desired_keys:
            switching_category = ("activity" in current_keys) != ("activity" in desired_keys)

            if switching_category:
                for child in list(panes_container.children):
                    child.remove()
                self._log_panes.clear()
            else:
                for key in current_keys - desired_keys:
                    pane_info = self._log_panes.pop(key, None)
                    if pane_info:
                        try:
                            w = self.query_one(f"#{pane_info['widget_id']}", RichLog)
                            container = w.parent
                            prev_sep = container.previous_sibling if container else None
                            if prev_sep and "log-pane-separator" in (prev_sep.classes or set()):
                                prev_sep.remove()
                            elif container:
                                next_sep = container.next_sibling
                                if next_sep and "log-pane-separator" in (next_sep.classes or set()):
                                    next_sep.remove()
                            if container:
                                container.remove()
                        except Exception:
                            pass

            if "activity" in desired_keys and "activity" not in self._log_panes:
                label_widget.update("Log [activity]")
                pane_id = "log-pane-activity"
                widget = RichLog(id=pane_id, max_lines=50, wrap=True, classes="log-pane-widget")
                panes_container.mount(
                    Vertical(widget, classes="log-pane-container")
                )
                self._log_panes["activity"] = {"widget_id": pane_id, "file_pos": 0}
            elif "activity" not in desired_keys:
                keys_to_add = (
                    desired_keys if switching_category
                    else desired_keys - current_keys
                )
                for repo, pr in active_reviews:
                    slug = repo_slug(repo) if repo else ""
                    key = f"{slug}:{pr}" if slug else str(pr)
                    if key not in keys_to_add:
                        continue
                    if self._log_panes:
                        panes_container.mount(Static("", classes="log-pane-separator"))
                    safe_id = key.replace(":", "-")
                    pane_id = f"log-pane-{safe_id}"
                    short = _short_repo(repo) if repo else ""
                    title_text = f"{short}/#{pr}" if short else f"PR #{pr}"
                    title = Static(title_text, classes="log-pane-title")
                    widget = RichLog(id=pane_id, max_lines=50, wrap=True, classes="log-pane-widget")
                    panes_container.mount(
                        Vertical(title, widget, classes="log-pane-container")
                    )
                    self._log_panes[key] = {"widget_id": pane_id, "file_pos": 0}
                has_multi_repos = len(set(r for r, _ in active_reviews if r)) > 1
                if has_multi_repos:
                    pr_list = " ".join(
                        f"{_short_repo(r)}/#{p}" for r, p in active_reviews
                    )
                else:
                    pr_list = " ".join(f"#{p}" for _, p in active_reviews)
                label_widget.update(f"Log [following {pr_list}]")

        logs = logs_dir()
        live = live_dir()
        for key, pane_info in self._log_panes.items():
            if key == "activity":
                log_file = logs / "activity.log"
            elif ":" in key:
                slug, pr_str = key.split(":", 1)
                log_file = live / slug / f"pr{pr_str}.log"
            else:
                log_file = live / f"pr{key}.log"

            try:
                widget = self.query_one(f"#{pane_info['widget_id']}", RichLog)
            except Exception:
                continue

            self._tail_file_into(log_file, widget, pane_info)

    def _tail_file_into(self, log_file: Path, widget: RichLog, pane_info: dict) -> None:
        if not log_file.exists():
            return
        is_activity = log_file.name == "activity.log"
        try:
            size = log_file.stat().st_size
            pos = pane_info.get("file_pos", 0)
            if size <= pos:
                if size < pos:
                    pane_info["file_pos"] = 0
                    widget.clear()
                return
            with open(log_file, "r") as f:
                if pos == 0:
                    content = f.read()
                    lines = content.strip().split("\n")[-20:]
                    if is_activity:
                        lines = [ln for ln in lines if " DEBUG " not in ln]
                    widget.clear()
                    for line in lines:
                        widget.write(format_log_line(line))
                    pane_info["file_pos"] = size
                else:
                    f.seek(pos)
                    new_data = f.read()
                    pane_info["file_pos"] = f.tell()
                    if new_data.strip():
                        for line in new_data.strip().split("\n"):
                            if is_activity and " DEBUG " in line:
                                continue
                            widget.write(format_log_line(line))
        except OSError:
            pass

    # ── Title ────────────────────────────────────────────────

    def _update_title(self) -> None:
        if not self.server:
            self.title = "gate"
            return
        reviews = self.server.reviews
        if not reviews:
            target = "gate"
        elif any(r.get("status") == "stuck" for r in reviews):
            target = "gateJAM"
        elif any(r.get("status") == "error" for r in reviews):
            target = "gateERR"
        elif any(r.get("status") == "fixing" for r in reviews):
            target = "fixing"
        else:
            target = "gating"

        self.title = target
        if target != self._window_title:
            self._window_title = target
            self._rename_tmux_window(target)

        if self.server:
            uptime = format_uptime(self.server.started_at)
            active = len(reviews)
            parts = [f"v{__version__}"]
            if active:
                parts.append(f"{active} active")
            parts.append(f"up {uptime}")
            self.sub_title = " | ".join(parts)

    def _rename_tmux_window(self, name: str) -> None:
        if not self.server or not getattr(self.server, "tmux_location", None):
            return
        try:
            from gate.tmux import rename_window

            pane = self.server.tmux_location.get("pane", "")
            if pane:
                rename_window(pane, name)
        except Exception:
            pass

    # ── Actions ──────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._poll_server()
        self._update_recent_and_metrics()
        self._update_system_info()
        self.notify("Refreshed", timeout=1)

    def action_cancel_review(self) -> None:
        if not self.server or not self.server.reviews:
            self.notify("No active reviews to cancel", severity="warning")
            return
        table = self.query_one("#reviews-table", DataTable)
        try:
            from textual.widgets._data_table import Coordinate
            row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        except Exception:
            self.notify("Cannot identify review", severity="warning")
            return
        review_id = None
        for rid, rk in self._active_row_keys.items():
            if rk == row_key:
                review_id = rid
                break
        if review_id:
            self.server.enqueue(
                {"type": "review_cancelled", "review_id": review_id}
            )
            self.notify(f"Cancel requested for {review_id}")
        else:
            self.notify("Cannot identify review", severity="warning")

    def action_detail(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            if focused.id == "reviews-table":
                self._show_active_detail()
            elif focused.id == "recent-table":
                self._show_completed_detail()

    def _show_active_detail(self) -> None:
        if not self.server or not self.server.reviews:
            self.notify("No active reviews", severity="warning")
            return
        table = self.query_one("#reviews-table", DataTable)
        try:
            from textual.widgets._data_table import Coordinate
            row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        except Exception:
            return
        review_id = None
        for rid, rk in self._active_row_keys.items():
            if rk == row_key:
                review_id = rid
                break
        if review_id:
            review = next((r for r in self.server.reviews if r.get("id") == review_id), None)
            if review:
                self.push_screen(ReviewDetailScreen(review, server=self.server))

    def _show_completed_detail(self) -> None:
        if not self._recent_entries:
            self.notify("No recent reviews", severity="warning")
            return
        table = self.query_one("#recent-table", DataTable)
        row_idx = table.cursor_row
        if 0 <= row_idx < len(self._recent_entries):
            self.push_screen(CompletedDetailScreen(self._recent_entries[row_idx]))

    def action_toggle_log(self) -> None:
        self._log_visible = not self._log_visible
        container = self.query_one("#log-container", Vertical)
        container.display = self._log_visible
        if self._log_visible:
            for pane_info in self._log_panes.values():
                pane_info["file_pos"] = 0
            self._poll_log()

    def action_open_pr(self) -> None:
        pr_num = self._get_selected_pr()
        repo = self._get_selected_repo()
        if not pr_num:
            self.notify("No PR selected — focus a row first", severity="warning")
            return
        try:
            subprocess.Popen(
                ["gh", "pr", "view", str(pr_num), "--repo", repo, "--web"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.notify(f"Opening PR #{pr_num} in browser")
        except Exception:
            self.notify("Failed to open PR", severity="error")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def _get_selected_pr(self) -> int | None:
        focused = self.focused
        if not isinstance(focused, DataTable):
            return None
        row_idx = focused.cursor_row
        if focused.id == "reviews-table" and self.server:
            reviews = self.server.reviews
            if 0 <= row_idx < len(reviews):
                return reviews[row_idx].get("pr_number")
        elif focused.id == "recent-table":
            if 0 <= row_idx < len(self._recent_entries):
                return self._recent_entries[row_idx].get(
                    "pr", self._recent_entries[row_idx].get("pr_number")
                )
        return None

    def _get_selected_repo(self) -> str:
        focused = self.focused
        if isinstance(focused, DataTable) and focused.id == "reviews-table" and self.server:
            reviews = self.server.reviews
            row_idx = focused.cursor_row
            if 0 <= row_idx < len(reviews):
                return reviews[row_idx].get("repo", "")
        return self._config.get("repo", {}).get("name", "")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_detail()


def run_tui(server=None) -> int:
    """Run the TUI application. Returns exit code."""
    # TUI always needs color — override NO_COLOR if set by environment
    os.environ.pop("NO_COLOR", None)
    os.environ["FORCE_COLOR"] = "1"
    app = GateTUI(server=server)
    app.run()
    return 0
