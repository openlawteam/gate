"""Gate CLI — entry point for all gate commands."""

import argparse
import subprocess
import sys
from pathlib import Path

import setproctitle

from gate import __version__

COMMANDS: dict[str, tuple] = {}


def command(name: str, description: str, group: str = "commands"):
    """Decorator to register a CLI command."""

    def decorator(func):
        COMMANDS[name] = (func, description, group)
        return func

    return decorator


class ArgumentError(Exception):
    pass


def make_parser(cmd: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog=f"gate {cmd}",
        description=description,
        exit_on_error=False,
    )


def parse_args(parser: argparse.ArgumentParser, args: list[str]) -> argparse.Namespace:
    try:
        return parser.parse_args(args)
    except argparse.ArgumentError as e:
        raise ArgumentError(str(e)) from e
    except SystemExit:
        raise


def print_help() -> None:
    print(f"gate v{__version__} — AI-powered PR review system")
    print()
    print("Usage: gate <command> [options]")
    print()
    print("Setup:")
    for name, (_, desc, group) in COMMANDS.items():
        if group == "setup":
            print(f"  {name:<16} {desc}")
    print()
    print("Commands:")
    for name, (_, desc, group) in COMMANDS.items():
        if group not in ("internal", "setup"):
            print(f"  {name:<16} {desc}")
    print()
    print("Internal:")
    for name, (_, desc, group) in COMMANDS.items():
        if group == "internal":
            print(f"  {name:<16} {desc}")
    print()
    print("Options:")
    print("  -h, --help     Show this help message")
    print("  --version      Show version number")


@command("init", "Set up Gate for a repository", group="setup")
def cmd_init(args: list[str]) -> int:
    """Interactive setup for Gate — generates config, validates env, creates dirs."""
    import tomllib

    from gate import setup
    from gate.config import data_dir, gate_dir

    parser = make_parser("init", "Set up Gate for a repository.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing config")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Skip prompts (requires --repo and --clone-path)")
    parser.add_argument("--repo", default="", help="Repository (owner/name)")
    parser.add_argument("--clone-path", default="", help="Local clone path")
    parser.add_argument("--branch", default="main", help="Default branch")
    parser.add_argument("--bot", default="", help="Bot account name")
    parser.add_argument("--worktree-base", default="/tmp/gate-worktrees",
                        help="Worktree base directory")
    parser.add_argument("--project-type", default="",
                        help="Project type (node/python/go/rust/none). Auto-detected if omitted.")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    print("Checking prerequisites...\n")
    checks, all_ok = setup.check_prerequisites()
    setup.print_checks(checks)
    print()
    if not all_ok:
        print("Fix the required prerequisites above before continuing.")
        return 1

    if parsed.non_interactive:
        if not parsed.repo:
            print("error: --repo is required in non-interactive mode")
            return 1
        if parsed.repo.count("/") != 1 or not all(parsed.repo.split("/")):
            print("error: --repo must be in owner/name format")
            return 1
        if not parsed.clone_path:
            print("error: --clone-path is required in non-interactive mode")
            return 1
        ok, detail = setup.validate_clone_path(parsed.clone_path)
        if not ok:
            print(f"error: {detail}")
            return 1
        bot = parsed.bot or setup.detect_gh_user() or "gate-bot"
        from gate import profiles
        project_type = parsed.project_type or profiles.detect_project_type(Path(parsed.clone_path))
        repo_config = {
            "name": parsed.repo,
            "clone_path": parsed.clone_path,
            "default_branch": parsed.branch,
            "bot_account": bot,
            "worktree_base": parsed.worktree_base,
            "escalation_reviewers": "",
            "project_type": project_type,
        }
    else:
        print("Configure your repository:\n")
        repo_config = setup.prompt_repo_config()
        print()

    config_path = gate_dir() / "config" / "gate.toml"
    if (config_path.exists()
            and not setup.is_placeholder_config(config_path)
            and not parsed.force):
        print(f"Config already exists at {config_path}")
        print("Use --force to overwrite, or use 'gate add-repo' to add another repository.")
        return 1

    content = setup.format_full_config([repo_config])
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as e:
        print(f"error: generated config is invalid TOML: {e}")
        return 1

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content)
    print(f"Configuration written to {config_path}")

    print("\nEnvironment variables:\n")
    env_checks = setup.validate_env_vars()
    setup.print_checks(env_checks)

    data_root = data_dir()
    for d in ("state", "logs", "logs/live"):
        (data_root / d).mkdir(parents=True, exist_ok=True)
    print(f"\nCreated runtime directories under {data_root}")

    if parsed.non_interactive:
        print("\nTo copy the workflow, run 'gate init' interactively")
        print("or copy workflows/gate-review.yml manually.")
    else:
        print()
        clone = Path(repo_config["clone_path"]).expanduser()
        setup.copy_workflow(clone)

    print(f"""
--- Setup complete ---

Config:     {config_path}
Repository: {repo_config["name"]}
Clone path: {repo_config["clone_path"]}

Next steps:
  1. Set environment variables (see .env.example)
  2. Start Gate: tmux new 'gate up'
  3. Verify: gate doctor
""")
    return 0


@command("add-repo", "Add a repository to Gate", group="setup")
def cmd_add_repo(args: list[str]) -> int:
    """Add another repository to an existing Gate configuration."""
    import shutil as _shutil
    import tomllib

    from gate import setup
    from gate.config import gate_dir, get_all_repos, load_config

    parser = make_parser("add-repo", "Add a repository to Gate.")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Skip prompts (requires --repo and --clone-path)")
    parser.add_argument("--repo", default="", help="Repository (owner/name)")
    parser.add_argument("--clone-path", default="", help="Local clone path")
    parser.add_argument("--branch", default="main", help="Default branch")
    parser.add_argument("--bot", default="", help="Bot account name")
    parser.add_argument("--worktree-base", default="/tmp/gate-worktrees",
                        help="Worktree base directory")
    parser.add_argument("--project-type", default="",
                        help="Project type (node/python/go/rust/none). Auto-detected if omitted.")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    config_path = gate_dir() / "config" / "gate.toml"
    if not config_path.exists():
        print("No config found. Run 'gate init' first.")
        return 1
    if setup.is_placeholder_config(config_path):
        print("Config contains only placeholders. Run 'gate init' first.")
        return 1

    config = load_config()
    if not config and config_path.exists():
        print(f"error: {config_path} exists but could not be parsed")
        return 1

    if parsed.non_interactive:
        if not parsed.repo:
            print("error: --repo is required in non-interactive mode")
            return 1
        if parsed.repo.count("/") != 1 or not all(parsed.repo.split("/")):
            print("error: --repo must be in owner/name format")
            return 1
        if not parsed.clone_path:
            print("error: --clone-path is required in non-interactive mode")
            return 1
        ok, detail = setup.validate_clone_path(parsed.clone_path)
        if not ok:
            print(f"error: {detail}")
            return 1
        bot = parsed.bot or setup.detect_gh_user() or "gate-bot"
        from gate import profiles
        project_type = parsed.project_type or profiles.detect_project_type(Path(parsed.clone_path))
        new_repo = {
            "name": parsed.repo,
            "clone_path": parsed.clone_path,
            "default_branch": parsed.branch,
            "bot_account": bot,
            "worktree_base": parsed.worktree_base,
            "escalation_reviewers": "",
            "project_type": project_type,
        }
    else:
        print("Configure the new repository:\n")
        new_repo = setup.prompt_repo_config()
        print()

    existing_repos = get_all_repos(config)
    for r in existing_repos:
        if r.get("name") == new_repo["name"]:
            print(f"Repository '{new_repo['name']}' is already configured.")
            return 1

    all_repos = existing_repos + [new_repo]
    content = setup.format_full_config(all_repos, globals_config=config)
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as e:
        print(f"error: generated config is invalid TOML: {e}")
        return 1

    backup_path = config_path.parent / (config_path.name + ".bak")
    _shutil.copy2(config_path, backup_path)
    print(f"Backed up config to {backup_path}")

    config_path.write_text(content)
    print(f"Added {new_repo['name']} to {config_path}")
    print(f"Config now has {len(all_repos)} repositories.")

    clone = Path(new_repo["clone_path"]).expanduser()
    setup.copy_workflow(clone, interactive=not parsed.non_interactive)

    print(f"""
--- Repository added ---

Repository: {new_repo["name"]}
Clone path: {new_repo["clone_path"]}
Total repos: {len(all_repos)}

Restart Gate to pick up the new config: gate up
""")
    return 0


@command("review", "Enqueue a PR review")
def cmd_review(args: list[str]) -> int:
    """Enqueue a review for a PR. Called by the GHA trigger workflow."""
    parser = make_parser("review", "Enqueue a PR review.")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument("--repo", required=True, help="Repository (owner/name)")
    parser.add_argument("--event", default="synchronize", help="GitHub event action")
    parser.add_argument("--head-sha", required=True, help="Head commit SHA")
    parser.add_argument("--branch", required=True, help="PR branch name")
    parser.add_argument("--labels", default="", help="Comma-separated label names")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    import time as _time

    from gate.client import send_message
    from gate.config import socket_path as _socket_path

    labels = [label.strip() for label in parsed.labels.split(",") if label.strip()]
    socket_path = _socket_path()

    message = {
        "type": "review_request",
        "pr_number": parsed.pr,
        "repo": parsed.repo,
        "head_sha": parsed.head_sha,
        "event": parsed.event,
        "branch": parsed.branch,
        "labels": labels,
    }

    max_retries = 3
    response = None
    for attempt in range(max_retries):
        response = send_message(
            socket_path,
            message,
            timeout=5.0,
            wait_for_response=True,
            expected_types={"review_accepted", "error"},
        )
        if response is not None:
            break
        if attempt < max_retries - 1:
            _time.sleep(2 * (attempt + 1))

    if response is None:
        print("Error: could not reach Gate server. Is it running? (gate up --headless)")
        return 1
    if response.get("type") == "review_accepted":
        print(
            f"Review enqueued: PR #{parsed.pr} ({parsed.repo}) "
            f"sha={parsed.head_sha[:8]} branch={parsed.branch} labels={labels}"
        )
        return 0
    print(f"Server rejected review: {response}")
    return 1


@command("process", "Run a review stage inside tmux", group="internal")
def cmd_process(args: list[str]) -> int:
    """Run Claude for a review stage. Internal — runs inside a tmux window."""
    import logging

    from gate.config import load_config
    from gate.logger import attach_gate_file_handler, runners_dir
    from gate.runner import ReviewRunner

    parser = make_parser("process", "Run a review stage inside a tmux window (internal).")
    parser.add_argument("review_id", help="Review ID (e.g., pr42)")
    parser.add_argument("stage", help="Stage name (architecture, security, logic, fix-senior)")
    parser.add_argument("--workspace", required=True, help="Worktree path")
    parser.add_argument("--socket", default=None, help="Server socket path")
    parser.add_argument("--repo", default="", help="Repository (owner/name) for per-repo config")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    # Attach a per-runner file handler so logger output (including
    # ReviewRunner._emit_state error messages) survives after the tmux
    # window exits. Without this, runner crashes lose all diagnostics
    # because the tmux pane is torn down ~10s after the process dies.
    try:
        runner_log = runners_dir() / f"{parsed.review_id}-{parsed.stage}.log"
        attach_gate_file_handler(runner_log, level=logging.DEBUG)
    except OSError as e:
        print(f"warning: could not attach runner log handler: {e}", file=sys.stderr)

    config = load_config()
    if parsed.repo:
        from gate.config import resolve_repo_config
        config = resolve_repo_config(parsed.repo, config)

    socket_path = Path(parsed.socket) if parsed.socket else None
    runner = ReviewRunner(
        review_id=parsed.review_id,
        stage=parsed.stage,
        workspace=Path(parsed.workspace),
        config=config,
        socket_path=socket_path,
    )
    return runner.run()


@command("up", "Start server and TUI")
def cmd_up(args: list[str]) -> int:
    """Start the Gate server and TUI dashboard."""
    from gate.config import socket_path as _socket_path
    from gate.tmux import get_current_tmux_location, is_inside_tmux

    parser = make_parser("up", "Start the Gate server and TUI.")
    parser.add_argument("--headless", action="store_true", help="Run without TUI")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    socket_path = _socket_path()

    from gate.cleanup import cleanup_orphans
    cleanup_orphans()

    if parsed.headless:
        from gate.server import start_server_headless

        return start_server_headless(socket_path)

    if not is_inside_tmux():
        print("gate up must run inside tmux.")
        print()
        print("Start a new session:")
        print("    tmux new 'gate up'")
        return 1

    from gate.server import start_server_with_tui

    tmux_location = get_current_tmux_location()
    return start_server_with_tui(socket_path, tmux_location=tmux_location)


@command("status", "Print current state")
def cmd_status(args: list[str]) -> int:
    """Print current gate state to stdout.

    Health comes from an in-process ``run_health_check()`` call (same
    path as ``gate health``) rather than the server-side cache — the
    cache is never populated because no code ever sends the server a
    ``health_update`` message. Doing it in-process costs ~50ms per
    invocation and guarantees ``gate status`` reflects the current
    reality rather than a stale dict.

    A latched ``quota_auth`` drift is surfaced as a dedicated top-line
    alert because it's the one degradation users most commonly miss:
    Codex starts erroring under the covers, reviews silently degrade,
    and ``gate status`` would otherwise say nothing.
    """
    from gate.client import list_queue, list_reviews, ping
    from gate.config import socket_path as _socket_path
    from gate.health import run_health_check

    socket_path = _socket_path()
    if not ping(socket_path):
        print("Gate server not running. Start with: gate up")
        return 1

    reviews = list_reviews(socket_path)
    queue_items = list_queue(socket_path)
    health = run_health_check()

    print(f"gate v{__version__}")

    # Quota-auth drift: dedicated top-line alert. Printed BEFORE the
    # normal reviews/queue lines so it can't be lost in a long
    # active-reviews list.
    quota_auth = health.get("quota_auth") if isinstance(health, dict) else None
    if isinstance(quota_auth, dict) and not quota_auth.get("ok", True):
        detail = quota_auth.get("detail", "auth drift latched")
        print()
        print(
            "⛔  quota_auth: " + detail
            + "  — refresh OAuth token; reviews will degrade as Codex requests fail."
        )

    print()
    if reviews:
        print(f"Active reviews ({len(reviews)}):")
        for r in reviews:
            print(f"  PR #{r.get('pr_number', '?')} — {r.get('stage', '')} ({r.get('status', '')})")
    else:
        print("No active reviews")

    if queue_items:
        print(f"\nQueued ({len(queue_items)}):")
        for item in queue_items:
            print(f"  PR #{item.get('pr_number', '?')}")

    # Always print a Health: section — empty-check was hiding
    # everything including degradations that silently affect review
    # quality.
    if isinstance(health, dict) and health:
        failing = [
            (k, v) for k, v in health.items()
            if isinstance(v, dict) and not v.get("ok", True)
        ]
        if failing:
            print(f"\n⚠  Health issues ({len(failing)}):")
            for k, v in failing:
                print(f"  - {k}: {v.get('detail', '')}")
        else:
            print("\nHealth: OK")
    else:
        print("\nHealth: unavailable")

    return 0


@command("cancel", "Cancel an in-progress review")
def cmd_cancel(args: list[str]) -> int:
    """Cancel an in-progress review."""
    parser = make_parser("cancel", "Cancel an in-progress review.")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument("--repo", default="", help="Repository (owner/name)")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    from gate.client import send_message
    from gate.config import socket_path as _socket_path

    socket_path = _socket_path()
    response = send_message(
        socket_path,
        {"type": "cancel_review", "pr_number": parsed.pr, "repo": parsed.repo},
        timeout=5.0,
        wait_for_response=True,
    )
    if response is None:
        print("Error: could not reach Gate server. Is it running?")
        return 1
    if response.get("cancelled"):
        print(f"Cancelled review for PR #{parsed.pr}")
    else:
        print(f"No active review for PR #{parsed.pr}")
    return 0


@command("cleanup-pr", "Clean up state for a closed PR")
def cmd_cleanup_pr(args: list[str]) -> int:
    """Clean up state and worktrees for a closed PR."""
    parser = make_parser("cleanup-pr", "Clean up state for a closed PR.")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument("--repo", default="", help="Repository (owner/name)")
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    from gate.cleanup import cleanup_pr_worktrees
    from gate.state import cleanup_pr_state

    cleanup_pr_state(parsed.pr, parsed.repo)
    cleanup_pr_worktrees(parsed.pr, parsed.repo)
    print(f"Cleaned up state for PR #{parsed.pr}")
    return 0


@command("inspect-pr", "Inspect persisted review state for a PR")
def cmd_inspect_pr(args: list[str]) -> int:
    """Pretty-print findings and stage results from a PR's persisted state.

    Reads from ``state_dir()/<repo>/pr<N>/`` — the same location
    ``persist_review_state`` writes to. Useful for post-mortem /
    operator inspection without hand-parsing verdict.json.
    """
    import json as _json

    parser = make_parser("inspect-pr", "Inspect persisted review state for a PR.")
    parser.add_argument("pr", type=int, help="PR number")
    parser.add_argument("--repo", default="", help="Repository (owner/name)")
    parser.add_argument(
        "--stage",
        default="all",
        choices=["all", "verdict", "build", "architecture", "security", "logic", "triage"],
        help="Only show one stage's output (default: all)",
    )
    parser.add_argument(
        "--raw", action="store_true",
        help="Dump raw JSON instead of the rich-table view.",
    )
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    from gate.state import get_pr_state_dir

    state_path = get_pr_state_dir(parsed.pr, parsed.repo)
    if not state_path.exists():
        suffix = f" in {parsed.repo}" if parsed.repo else ""
        print(f"No persisted state for PR #{parsed.pr}{suffix}")
        return 1

    stage_map = {
        "verdict": state_path / "verdict.json",
        "triage": state_path / "triage.json",
        "build": state_path / "build.json",
        "architecture": state_path / "architecture.json",
        "security": state_path / "security.json",
        "logic": state_path / "logic.json",
    }
    if parsed.stage != "all":
        stage_map = {parsed.stage: stage_map[parsed.stage]}

    if parsed.raw:
        out: dict = {}
        for name, path in stage_map.items():
            if path.exists():
                try:
                    out[name] = _json.loads(path.read_text())
                except (OSError, _json.JSONDecodeError) as e:
                    out[name] = {"_error": str(e)}
        print(_json.dumps(out, indent=2))
        return 0

    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        print("rich not installed — install with: pip install -e '.[dev]'")
        return 1

    from gate.schemas import Finding

    console = Console()
    console.print(
        f"[bold]PR #{parsed.pr}[/bold]"
        + (f" in [cyan]{parsed.repo}[/cyan]" if parsed.repo else "")
        + f"  [dim]state: {state_path}[/dim]"
    )

    verdict_path = stage_map.get("verdict") or state_path / "verdict.json"
    if verdict_path and verdict_path.exists():
        try:
            verdict = _json.loads(verdict_path.read_text())
        except (OSError, _json.JSONDecodeError) as e:
            console.print(f"[red]verdict.json could not be read: {e}[/red]")
            verdict = None
        if verdict:
            console.print(
                f"\n[bold]Verdict:[/bold] {verdict.get('decision', '?')} "
                f"([dim]confidence={verdict.get('confidence', '?')}[/dim])"
            )
            if verdict.get("summary"):
                console.print(f"  {verdict['summary']}")

            findings = verdict.get("findings") or []
            if findings:
                table = Table(
                    title=f"Findings ({len(findings)})",
                    show_header=True, header_style="bold",
                )
                table.add_column("Sev")
                table.add_column("Stage")
                table.add_column("Location")
                table.add_column("Rule")
                table.add_column("Message")
                table.add_column("Suggestion")
                for raw in findings:
                    try:
                        f = Finding.from_dict(raw)
                    except (ValueError, TypeError):
                        table.add_row(
                            "?", "?", "?", "?",
                            f"[red]malformed:[/red] {str(raw)[:80]}",
                            "",
                        )
                        continue
                    primary = f.primary_location()
                    loc = primary.file or "?"
                    if primary.line:
                        loc += f":{primary.line}"
                    if len(f.locations) > 1:
                        loc += f" [dim](+{len(f.locations) - 1})[/dim]"
                    table.add_row(
                        f.severity,
                        f.source_stage or "?",
                        loc,
                        f.rule_source or "-",
                        (f.message or "")[:120],
                        (f.suggestion or "")[:120],
                    )
                console.print(table)
            else:
                console.print("  [dim]No findings.[/dim]")

    if parsed.stage == "all":
        # Summarise other stages — exact rendering depends on stage, so
        # keep it compact: stage name, top-level keys, a preview.
        for name, path in stage_map.items():
            if name == "verdict" or not path.exists():
                continue
            try:
                doc = _json.loads(path.read_text())
            except (OSError, _json.JSONDecodeError) as e:
                console.print(f"\n[bold]{name}:[/bold] [red]{e}[/red]")
                continue
            if not isinstance(doc, dict):
                console.print(f"\n[bold]{name}:[/bold] {str(doc)[:200]}")
                continue
            console.print(f"\n[bold]{name}:[/bold]")
            keys = sorted(doc.keys())
            for k in keys:
                v = doc[k]
                if isinstance(v, list):
                    console.print(f"  {k}: [dim]list({len(v)})[/dim]")
                elif isinstance(v, dict):
                    console.print(f"  {k}: [dim]dict({len(v)})[/dim]")
                else:
                    preview = str(v)
                    if len(preview) > 100:
                        preview = preview[:100] + "…"
                    console.print(f"  {k}: {preview}")

    return 0


@command("health", "Run health checks")
def cmd_health(args: list[str]) -> int:
    """Run all health checks and report results."""

    from gate.health import run_health_check

    parser = make_parser("health", "Run health checks.")
    parser.add_argument(
        "--since-restart",
        action="store_true",
        help="Also report how long any latched quota/auth alert has been unresolved.",
    )
    try:
        parsed = parse_args(parser, args)
    except SystemExit:
        return 0
    except ArgumentError as e:
        print(f"error: {e}")
        parser.print_usage()
        return 1

    results = run_health_check()
    errors = {k: v for k, v in results.items() if isinstance(v, dict) and not v.get("ok", True)}

    since_line = ""
    if parsed.since_restart:
        since_line = _render_latched_since()

    if errors:
        print(f"HEALTH CHECK: {len(errors)} issue(s)")
        for key, val in errors.items():
            print(f"  ❌ {key}: {val.get('detail', '')}")
        if since_line:
            print(since_line)
        return 1
    else:
        print("HEALTH CHECK: all OK")
        if since_line:
            print(since_line)
        return 0


def _render_latched_since() -> str:
    """Render how long any latched auth-drift alert has been unresolved.

    Reads the marker file ``gate.quota._maybe_alert_auth_drift`` writes
    when it fires a quota-auth-drift alert. Empty string when no latch
    is present so the caller can simply concatenate.
    """
    from gate.quota import _auth_drift_marker_path

    marker = _auth_drift_marker_path()
    if not marker.exists():
        return ""
    try:
        ts = float(marker.read_text().strip() or "0")
    except (OSError, ValueError):
        return ""
    import time as _t

    age_s = int(_t.time() - ts)
    if age_s <= 0:
        return ""
    if age_s < 60:
        age = f"{age_s}s"
    elif age_s < 3600:
        age = f"{age_s // 60}m"
    elif age_s < 86400:
        age = f"{age_s // 3600}h {age_s % 3600 // 60}m"
    else:
        age = f"{age_s // 86400}d {age_s % 86400 // 3600}h"
    return f"  ⏱  quota_auth latched for {age} (since unresolved)"


@command("cleanup", "Run log rotation and worktree pruning")
def cmd_cleanup(args: list[str]) -> int:
    """Run all cleanup tasks."""
    from gate.cleanup import run_cleanup

    run_cleanup()
    print("Cleanup complete")
    return 0


@command("prune", "Remove stale worktrees without touching state/logs")
def cmd_prune(args: list[str]) -> int:
    """Targeted worktree-only cleanup (Group 5D).

    ``gate cleanup`` rotates logs, trims reviews.jsonl, cleans up old
    state dirs, AND prunes worktrees — useful as a full maintenance
    cycle but overkill when a review crashed mid-flight and you just
    want to reclaim disk space. ``gate prune`` does the worktree step
    only so operators can unwedge a machine without perturbing audit
    state.
    """
    parser = make_parser("gate prune", "Remove stale gate worktrees.")
    parser.add_argument(
        "--max-age-hours", type=int, default=24,
        help="Worktrees older than this are removed (default: 24).",
    )
    parser.add_argument(
        "--aggressive", action="store_true",
        help="Also prune worktrees newer than max-age-hours whose PR "
             "has no active review marker.",
    )
    parsed = parse_args(parser, args)

    from gate.cleanup import cleanup_worktrees

    cleanup_worktrees(max_age_hours=parsed.max_age_hours)
    if parsed.aggressive:
        cleanup_worktrees(max_age_hours=0)
    print("Prune complete")
    return 0


@command("digest", "Send daily metrics digest")
def cmd_digest(args: list[str]) -> int:
    """Send daily metrics digest via ntfy and Discord."""
    from gate.cleanup import daily_digest

    daily_digest()
    print("Digest sent")
    return 0


@command("doctor", "Verify all prerequisites")
def cmd_doctor(args: list[str]) -> int:
    """Check that all prerequisites are met for Gate to run."""
    import os
    import shutil

    from gate.client import ping
    from gate.config import gate_dir, load_config

    checks: list[tuple[str, bool, str]] = []

    def _check_cmd(name: str, cmd: list[str], extract_version: bool = True) -> None:
        path = shutil.which(cmd[0])
        if not path:
            checks.append((name, False, "not found in PATH"))
            return
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            version = result.stdout.strip().split("\n")[0] if extract_version else ""
            checks.append((name, True, version))
        except (subprocess.SubprocessError, OSError) as e:
            checks.append((name, False, str(e)))

    _check_cmd("claude CLI", ["claude", "--version"])
    _check_cmd("gh CLI", ["gh", "--version"])
    _check_cmd("codex CLI", ["codex", "--version"])
    _check_cmd("tmux", ["tmux", "-V"])

    env_vars = [
        ("GATE_PAT", False),
        ("GATE_NTFY_TOPIC", True),
    ]
    for var, show_value in env_vars:
        val = os.environ.get(var)
        if val:
            display = val if show_value else f"set ({len(val)} chars)"
            checks.append((var, True, display))
        else:
            checks.append((var, False, "not set"))

    claude_oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not claude_oauth:
        from gate.quota import read_keychain_token
        claude_oauth = read_keychain_token() or ""
    if claude_oauth:
        checks.append(("Claude OAuth", True, f"token ({len(claude_oauth)} chars)"))
    else:
        checks.append(("Claude OAuth", False, "not in env or Keychain"))

    from gate.setup import check_codex_auth
    checks.append(check_codex_auth())

    gate_root = gate_dir()
    config_path = gate_root / "config" / "gate.toml"
    if config_path.exists():
        try:
            load_config()
            checks.append(("gate.toml", True, "parsed OK"))
        except Exception as e:
            checks.append(("gate.toml", False, str(e)))
    else:
        checks.append(("gate.toml", False, f"not found at {config_path}"))

    prompts_dir = gate_root / "prompts"
    if prompts_dir.exists():
        prompt_count = len(list(prompts_dir.glob("*.md")))
        checks.append(("prompts/", True, f"{prompt_count} files"))
    else:
        checks.append(("prompts/", False, "directory not found"))

    try:
        from gate import __version__ as gv

        checks.append(("pip install", True, f"gate {gv}"))
    except Exception:
        checks.append(("pip install", False, "gate package not installed"))

    config = load_config()
    from gate.config import get_all_repos
    all_repos = get_all_repos(config)
    if not all_repos:
        checks.append(("clone_path", False, "no repos configured in gate.toml"))
    for repo_cfg in all_repos:
        repo_name = repo_cfg.get("name", "?")
        clone_path_str = repo_cfg.get("clone_path", "")
        if not clone_path_str:
            checks.append((f"clone_path ({repo_name})", False, "not configured"))
        else:
            clone_path = Path(clone_path_str).expanduser()
            if clone_path.exists():
                checks.append((f"clone_path ({repo_name})", True, str(clone_path)))
            else:
                checks.append((f"clone_path ({repo_name})", False, f"{clone_path} does not exist"))

    runner_path_str = config.get("runner", {}).get("path", "")
    if runner_path_str:
        runner_path = Path(runner_path_str)
        runner_name = runner_path.name
        if runner_path.exists():
            checks.append((runner_name, True, str(runner_path)))
        else:
            checks.append((runner_name, False, f"{runner_path} not found"))
    else:
        checks.append(("runner", True, "not configured (optional)"))

    from gate.config import socket_path as _socket_path
    sock = _socket_path()
    if sock.exists() and ping(sock, timeout=2.0):
        checks.append(("server socket", True, str(sock)))
    else:
        checks.append(("server socket", False, "not running"))

    max_label = max(len(c[0]) for c in checks)
    has_failures = False
    for label, ok, detail in checks:
        dots = "." * (max_label + 4 - len(label))
        status = "OK" if ok else "FAIL"
        marker = "  " if ok else "  "
        line = f"{marker}{label} {dots} {status}"
        if detail:
            line += f" ({detail})"
        print(line)
        if not ok:
            has_failures = True

    print()
    if has_failures:
        print("Some checks failed. Fix the issues above before running Gate.")
        return 1
    else:
        print("All checks passed. Gate is ready.")
        return 0


@command("update", "Pull latest code and reinstall")
def cmd_update(args: list[str]) -> int:
    """Pull latest gate code and reinstall the package."""
    from gate.config import gate_dir

    gate_root = gate_dir()
    try:
        subprocess.run(["git", "-C", str(gate_root), "pull"], check=True, timeout=120)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", "."],
            cwd=str(gate_root),
            check=True,
            timeout=120,
        )
        print("Gate updated successfully.")
        return 0
    except subprocess.TimeoutExpired:
        print("Update timed out.")
        return 1
    except subprocess.CalledProcessError as e:
        print(f"Update failed: {e}")
        return 1


@command(
    "checkpoint",
    "Hopper-mode sub-scope checkpoint helpers (save/revert/finalize/list)",
    group="internal",
)
def cmd_checkpoint(args: list[str]) -> int:
    """Dispatch to ``gate.checkpoint.cli_main``.

    Registered here (not in ``checkpoint.py``) so ``gate.cli.COMMANDS``
    remains the single source of truth for ``gate <cmd>`` dispatch.
    """
    from gate import checkpoint as _checkpoint
    return _checkpoint.cli_main(args)


def main() -> int:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print_help()
        return 0

    if args[0] == "--version":
        print(f"gate {__version__}")
        return 0

    cmd = args[0]
    cmd_args = args[1:]

    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}")
        print()
        print_help()
        return 1

    setproctitle.setproctitle(f"gate:{cmd}")

    handler, *_ = COMMANDS[cmd]
    return handler(cmd_args)
