"""Gate sub-scope checkpoint primitives (hopper-mode fix pipeline).

Exposed via the ``gate checkpoint`` CLI subcommand and called by senior
Claude from inside the review worktree during hopper-mode fix runs. The
senior-facing contract is:

- ``gate checkpoint save --name <name> [--touched-files a,b,c]``
      ``git add -A && git commit`` a lightweight checkpoint commit
      (``gate-checkpoint: <name>``). Runs a *scoped* ``build_verify``
      limited to ``--touched-files`` (plus any other currently-modified
      files) to keep the round-trip fast. On success, prints the new SHA
      to stdout. On failure, prints the scoped error tail to stderr, exits
      non-zero, and leaves the checkpoint commit in place so the senior
      can decide whether to iterate or ``revert``.

- ``gate checkpoint revert --to-last-clean``
      Hard-reset to the previous ``gate-checkpoint:`` SHA (or to the
      pre-fix baseline if no checkpoints exist yet). Used when a sub-scope
      exhausts its iteration budget.

- ``gate checkpoint finalize <<'EOF'  \n<commit body>\n EOF``
      Squash every ``gate-checkpoint:`` commit between the pre-fix
      baseline and HEAD into a single final commit with the supplied body.
      Leaves the working tree clean and ready for ``commit_and_push``.

- ``gate checkpoint list``
      Print one-line summaries of the current sub-scope checkpoints. Used
      by tests and live-log formatters.

The module deliberately stays side-effect-free when imported: all git
work happens inside the subcommand handlers. No Gate-process state is
required — we read the pre-fix baseline SHA from a worktree-local file
(``.gate/pre-fix-sha``) written by the orchestrator at the start of the
fix run.

See ``docs/hopper-pipeline.md`` (Part 3 of the hardening plan) for the
end-to-end protocol.

## Exported helpers

``scoped_build_verify(workspace, touched_files, config)`` is the
package-level entry point used by ``_cmd_save`` and is re-exported for
test monkey-patching. **``config`` is required** — PR #20 removed the
``config=None`` fallback (which silently called ``load_config()``) so
configuration resolution lives at the CLI entry point, not inside the
helper. External wrappers that wrapped the old optional-config
signature will now raise ``TypeError`` at runtime; resolve config
yourself (via ``gate.config.load_config``) and pass it positionally.
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CHECKPOINT_PREFIX = "gate-checkpoint:"
PRE_FIX_SHA_FILE = ".gate/pre-fix-sha"
CONTEXT_FILE = ".gate/context.json"


def _load_context(workspace: Path) -> tuple[int, str]:
    """Return (pr_number, repo) so we can thread progress back to the PR.

    Falls back to ``(0, "")`` when the marker is missing — e.g. during
    local debugging or unit tests. The live-log helper tolerates 0 by
    skipping the write.
    """
    try:
        raw = (workspace / CONTEXT_FILE).read_text()
        data = json.loads(raw or "{}")
        pr_number = int(data.get("pr_number") or 0)
        repo = str(data.get("repo") or "")
        return pr_number, repo
    except (OSError, ValueError, json.JSONDecodeError):
        return 0, ""


def _emit_progress(workspace: Path, message: str) -> None:
    """Append a ``fix:`` prefixed line to the review's live log.

    Thin wrapper so save/revert/finalize all report through the same
    channel. Imported lazily because ``gate.logger`` pulls in quota +
    notify machinery that isn't required for the git-only paths.
    """
    pr_number, repo = _load_context(workspace)
    if not pr_number:
        return
    try:
        from gate.logger import write_live_log

        write_live_log(pr_number, message, prefix="fix", repo=repo)
    except Exception as exc:  # noqa: BLE001 — progress is best-effort
        logger.debug(f"progress write failed: {exc}")


@dataclass
class CheckpointInfo:
    """One row in the current checkpoint log."""

    sha: str
    name: str
    subject: str


# ── low-level git helpers ────────────────────────────────────


def _run(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _rev_parse(cwd: Path, ref: str) -> str:
    r = _run(["git", "rev-parse", ref], cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def _head_sha(cwd: Path) -> str:
    return _rev_parse(cwd, "HEAD")


def _pre_fix_sha(cwd: Path) -> str:
    """Return the baseline SHA captured by the orchestrator at fix start.

    The orchestrator writes ``.gate/pre-fix-sha`` inside the worktree at
    the top of ``ReviewFixer.run()``. If it's missing we fall back to
    ``HEAD~N`` where ``N`` is the number of outstanding checkpoint commits
    — this keeps ``gate checkpoint`` usable when called by hand during
    local debugging, but production flows always have the marker.
    """
    p = cwd / PRE_FIX_SHA_FILE
    try:
        raw = p.read_text().strip()
    except (OSError, UnicodeDecodeError):
        raw = ""
    if raw:
        return raw
    checkpoints = list_checkpoints(cwd)
    if not checkpoints:
        return _head_sha(cwd)
    return _rev_parse(cwd, f"HEAD~{len(checkpoints)}")


def list_checkpoints(cwd: Path) -> list[CheckpointInfo]:
    """Return the current sub-scope checkpoint commits newest-first.

    A commit is a checkpoint iff its subject starts with
    ``gate-checkpoint:``. We walk back from HEAD until we hit either the
    pre-fix baseline or a non-checkpoint commit.
    """
    r = _run(
        ["git", "log", "--pretty=format:%H%x00%s", "-n", "200"],
        cwd,
    )
    if r.returncode != 0:
        return []
    out: list[CheckpointInfo] = []
    baseline = _rev_parse(cwd, "HEAD")
    baseline_file = cwd / PRE_FIX_SHA_FILE
    if baseline_file.exists():
        try:
            baseline = baseline_file.read_text().strip() or baseline
        except (OSError, UnicodeDecodeError):
            pass
    for line in r.stdout.splitlines():
        sha, _, subject = line.partition("\x00")
        if not sha:
            continue
        if sha == baseline:
            break
        if not subject.startswith(CHECKPOINT_PREFIX):
            break
        name = subject[len(CHECKPOINT_PREFIX):].strip()
        out.append(CheckpointInfo(sha=sha, name=name, subject=subject))
    return out


# ── scoped build_verify ──────────────────────────────────────


def _resolve_workspace() -> Path:
    """Return the worktree root the command was invoked inside.

    We rely on ``git rev-parse --show-toplevel`` so senior Claude can call
    ``gate checkpoint`` from anywhere inside the review worktree.
    """
    r = _run(["git", "rev-parse", "--show-toplevel"], Path.cwd())
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(
            "gate checkpoint must be run inside a Gate review worktree"
        )
    return Path(r.stdout.strip())


def _scoped_paths(workspace: Path, touched: list[str]) -> list[str]:
    """Return the concrete file list to scope the build to.

    Combines ``--touched-files`` with whatever ``git diff --name-only
    HEAD~1`` shows (so a fresh checkpoint commit's changes are always
    included even if the senior forgets to pass ``--touched-files``).
    """
    paths: set[str] = set()
    for p in touched:
        p = p.strip()
        if p:
            paths.add(p)
    r = _run(
        ["git", "diff", "--name-only", "HEAD~1"],
        workspace,
    )
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                paths.add(line)
    # Drop paths that no longer exist (e.g. deleted by the sub-scope).
    return sorted(p for p in paths if (workspace / p).exists())


def _scoped_typecheck(workspace: Path, config: dict, files: list[str]) -> tuple[int, str]:
    """Run a scoped typecheck over ``files`` and return (exit, log_tail).

    TypeScript path: we translate the project profile's ``typecheck_cmd``
    into ``tsc --noEmit --incremental`` and let tsc pull in whatever the
    project's ``tsconfig.json`` references — scoping at the file level
    for tsc is a footgun because it bypasses project-wide settings. The
    ``--incremental`` flag keeps repeated runs cheap.

    Non-TS projects: fall back to the unscoped ``typecheck_cmd``.
    """
    from gate import profiles
    from gate.fixer import _run_silent

    repo_cfg = (config or {}).get("repo", {})
    profile = profiles.resolve_profile(repo_cfg, workspace)
    typecheck_cmd = profile.get("typecheck_cmd", "")
    if not typecheck_cmd:
        return 0, ""

    project_type = profile.get("project_type", "")
    cmd: list[str] | str
    if project_type == "typescript" and "tsc" in typecheck_cmd:
        # tsc project-mode with --incremental is the fastest safe scope.
        cmd = shlex.split(typecheck_cmd) + ["--incremental"]
    else:
        cmd = typecheck_cmd

    out, exit_code = _run_silent(cmd, cwd=str(workspace))
    return exit_code, out[-4000:]


# Extension allow-list per linter family. Used to drop non-source files
# (artifacts, JSON, logs) before handing them to a tool that would otherwise
# try to lint them as source code — e.g. ruff reading an unexcluded
# ``postconditions.json`` and reporting 200+ line-length errors (PR #14
# build-verify regression).
_LINT_EXTS: dict[str, tuple[str, ...]] = {
    "ruff": (".py", ".pyi"),
    "flake8": (".py", ".pyi"),
    "eslint": (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
    "biome": (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".jsonc"),
    "tsc": (".ts", ".tsx"),
}


def _lint_family(tool: str) -> str | None:
    """Map a linter executable token to its extension family, if known."""
    for family in _LINT_EXTS:
        if tool == family or family in tool:
            return family
    return None


def _scoped_lint(workspace: Path, config: dict, files: list[str]) -> tuple[int, str]:
    """Run lint scoped to ``files`` where the linter supports it.

    Most JS linters (eslint, biome) accept an explicit file list. Python
    tools (ruff, flake8) also do. For exotic linters we simply don't
    scope; the performance cost is bounded because the senior only calls
    ``gate checkpoint save`` a handful of times per fix run.

    Before forwarding ``files`` to a known linter we filter by extension
    so stray artifacts (e.g. ``postconditions.json``, ``*.codex.log``) that
    slipped past ``git add -A`` excludes can't poison the scoped run.
    """
    from gate import profiles
    from gate.fixer import _run_silent

    repo_cfg = (config or {}).get("repo", {})
    profile = profiles.resolve_profile(repo_cfg, workspace)
    lint_cmd = profile.get("lint_cmd", "")
    if not lint_cmd or not files:
        return 0, ""

    tokens = shlex.split(lint_cmd)
    tool = tokens[0] if tokens else ""
    family = _lint_family(tool)
    if family is None:
        # Unknown linter — just run the full command.
        out, exit_code = _run_silent(lint_cmd, cwd=str(workspace))
        return exit_code, out[-4000:]

    allowed = _LINT_EXTS[family]
    filtered = [f for f in files if f.endswith(allowed)]
    if not filtered:
        return 0, ""

    scoped = tokens + filtered
    out, exit_code = _run_silent(scoped, cwd=str(workspace))
    return exit_code, out[-4000:]


def scoped_build_verify(
    workspace: Path,
    touched_files: list[str],
    config: dict,
) -> dict:
    """Lightweight build_verify for a single sub-scope checkpoint.

    Returns a dict shaped ``{pass: bool, typecheck_exit, typecheck_tail,
    lint_exit, lint_tail, files: [...]}``. The ``tail`` fields are
    already truncated to ~4 KB each so the senior can echo them back into
    an ``implement`` re-prompt without blowing the context window.

    ``config`` is required — callers must resolve it once at the CLI
    entry point (``_cmd_save``) and thread it through, per the
    "config is resolved once at the top level" convention. The prior
    internal ``_load_config`` fallback was removed because calling
    ``load_config()`` deep inside an exported helper froze config at
    an unexpected call site and duplicated what the CLI already does.
    """
    files = _scoped_paths(workspace, touched_files)
    tc_exit, tc_tail = _scoped_typecheck(workspace, config, files)
    lint_exit, lint_tail = _scoped_lint(workspace, config, files)
    passed = tc_exit == 0 and lint_exit == 0
    return {
        "pass": passed,
        "typecheck_exit": tc_exit,
        "typecheck_tail": tc_tail,
        "lint_exit": lint_exit,
        "lint_tail": lint_tail,
        "files": files,
    }


# ── subcommand handlers ──────────────────────────────────────


def _cmd_save(args: argparse.Namespace) -> int:
    workspace = _resolve_workspace()
    name = args.name.strip()
    if not name:
        print("checkpoint name required", file=sys.stderr)
        return 2

    # Stage + commit first so the diff is captured in git history even if
    # the build check later fails — that way the senior can still call
    # ``gate checkpoint revert`` to unwind.
    _run(["git", "add", "-A"], workspace)
    have_staged = _run(
        ["git", "diff", "--cached", "--quiet"], workspace
    ).returncode != 0
    if not have_staged:
        print("no changes to checkpoint", file=sys.stderr)
        return 3

    msg = f"{CHECKPOINT_PREFIX} {name}"
    commit = _run(
        ["git", "-c", "commit.gpgsign=false", "commit", "--no-verify", "-m", msg],
        workspace,
    )
    if commit.returncode != 0:
        print(
            f"checkpoint commit failed: {commit.stderr.strip()}",
            file=sys.stderr,
        )
        return 4
    sha = _head_sha(workspace)

    touched = [p for p in (args.touched_files or "").split(",") if p.strip()]
    _emit_progress(
        workspace,
        f"Sub-scope '{name}' checkpoint {sha[:8]} "
        f"running scoped build_verify ({len(touched) or '?'} files)",
    )
    # Resolve config once at the CLI entry point and thread it through,
    # rather than having scoped_build_verify call load_config() itself.
    from gate.config import load_config
    config = load_config() or {}
    result = scoped_build_verify(workspace, touched, config)
    if not result["pass"]:
        _emit_progress(
            workspace,
            f"Sub-scope '{name}' build failed "
            f"(tsc exit={result['typecheck_exit']}, "
            f"lint exit={result['lint_exit']})",
        )
        # Emit compact JSON to stderr so fix-senior.md's "re-prompt with
        # the build tail" protocol works without parsing free-form logs.
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 5

    _emit_progress(
        workspace,
        f"Sub-scope '{name}' banked at {sha[:8]}",
    )
    print(sha)
    return 0


def _cmd_revert(args: argparse.Namespace) -> int:
    workspace = _resolve_workspace()
    baseline = _pre_fix_sha(workspace)
    checkpoints = list_checkpoints(workspace)
    if args.to_last_clean:
        # Drop the most recent checkpoint only.
        if not checkpoints:
            # No checkpoints exist — reset to baseline so any uncommitted
            # junior edits are wiped, matching the senior's expectation
            # that "revert" always returns a clean tree.
            target = baseline
        else:
            target = checkpoints[0].sha + "^"
            target_sha = _rev_parse(workspace, target)
            target = target_sha or baseline
    elif args.to_baseline:
        target = baseline
    else:
        print("pass --to-last-clean or --to-baseline", file=sys.stderr)
        return 2

    if not target:
        print("could not resolve revert target", file=sys.stderr)
        return 4

    reset = _run(["git", "reset", "--hard", target], workspace)
    if reset.returncode != 0:
        print(
            f"git reset --hard {target[:8]} failed: {reset.stderr.strip()}",
            file=sys.stderr,
        )
        return 5
    _run(["git", "clean", "-fd"], workspace)
    _emit_progress(
        workspace,
        f"Sub-scope reverted to {target[:8]} "
        f"({'baseline' if args.to_baseline else 'last clean checkpoint'})",
    )
    print(target)
    return 0


def _cmd_finalize(args: argparse.Namespace) -> int:
    workspace = _resolve_workspace()
    baseline = _pre_fix_sha(workspace)
    if not baseline:
        print("missing pre-fix baseline; cannot finalize", file=sys.stderr)
        return 4

    # Read the commit body from either --message or stdin (so senior can
    # use a heredoc to supply multi-line bodies without shell-quoting
    # hazards).
    if args.message:
        body = args.message
    else:
        body = sys.stdin.read().strip()
    if not body:
        print("finalize requires a commit message body", file=sys.stderr)
        return 2

    # If HEAD already equals the baseline there's nothing to finalize.
    if _head_sha(workspace) == baseline:
        print("no changes to finalize", file=sys.stderr)
        return 3

    # Snapshot the number of gate-checkpoint commits reachable from HEAD
    # *before* the soft-reset rewinds past them. Post-reset the count is
    # always 0 (the checkpoints aren't reachable from the new HEAD), so
    # the previous "squashed 0 checkpoints" progress line was misleading
    # when triaging PRs (see PR #222 forensics).
    checkpoints_before = len(list_checkpoints(workspace))

    # Squash all commits between baseline and HEAD into a single commit.
    # ``reset --soft`` preserves the working-tree + index state so the
    # follow-up ``git commit`` captures the aggregate diff.
    reset = _run(["git", "reset", "--soft", baseline], workspace)
    if reset.returncode != 0:
        print(
            f"git reset --soft {baseline[:8]} failed: {reset.stderr.strip()}",
            file=sys.stderr,
        )
        return 5

    commit = _run(
        [
            "git", "-c", "commit.gpgsign=false",
            "commit", "--no-verify", "-m", body,
        ],
        workspace,
    )
    if commit.returncode != 0:
        # Nothing to commit can happen if a scoped revert already wiped
        # everything — not an error, just a no-op.
        if "nothing to commit" in (commit.stdout + commit.stderr).lower():
            print("no changes to finalize", file=sys.stderr)
            return 3
        print(
            f"finalize commit failed: {commit.stderr.strip()}",
            file=sys.stderr,
        )
        return 6
    final_sha = _head_sha(workspace)
    _emit_progress(
        workspace,
        f"Hopper pipeline finalized — squashed {checkpoints_before} "
        f"checkpoints into {final_sha[:8]}",
    )
    print(final_sha)
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    workspace = _resolve_workspace()
    checkpoints = list_checkpoints(workspace)
    for cp in checkpoints:
        print(f"{cp.sha[:12]}  {cp.name}")
    return 0


# ── CLI entry point (wired from gate/cli.py) ─────────────────


def cli_main(argv: list[str]) -> int:
    """Entry point for ``gate checkpoint <sub>``."""
    parser = argparse.ArgumentParser(prog="gate checkpoint")
    sub = parser.add_subparsers(dest="sub", required=True)

    p_save = sub.add_parser("save", help="stage + commit a sub-scope checkpoint")
    p_save.add_argument("--name", required=True)
    p_save.add_argument(
        "--touched-files",
        default="",
        help="comma-separated list of files the sub-scope modified",
    )
    p_save.set_defaults(func=_cmd_save)

    p_revert = sub.add_parser(
        "revert", help="revert to the previous clean checkpoint"
    )
    p_revert.add_argument("--to-last-clean", action="store_true")
    p_revert.add_argument("--to-baseline", action="store_true")
    p_revert.set_defaults(func=_cmd_revert)

    p_final = sub.add_parser(
        "finalize", help="squash all sub-scope checkpoints into one commit"
    )
    p_final.add_argument("--message", default="")
    p_final.set_defaults(func=_cmd_finalize)

    p_list = sub.add_parser("list", help="show current checkpoint commits")
    p_list.set_defaults(func=_cmd_list)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)
