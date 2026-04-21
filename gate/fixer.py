"""Fix pipeline orchestrator.

Single continuous Claude session with Codex delegation.
"""

import json
import logging
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from gate import builder, github, notify, prompt, state
from gate.codex import bootstrap_codex
from gate.config import build_claude_env, gate_dir
from gate.finding_id import compute_finding_id  # re-exported for back-compat
from gate.logger import write_live_log
from gate.runner import StructuredRunner, run_with_retry
from gate.schemas import FixResult

__all__ = ["compute_finding_id"]

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 2
FIX_SESSION_MAX_TURNS = 300
RESUME_MAX_TURNS = 100

SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}

# Gate artifacts written to the worktree during review/fix.  Removed before
# commit as a safety net (the primary defense is the per-worktree git exclude
# configured in workspace._setup_artifact_exclusions).
#
# Keep in sync with GATE_EXCLUDE_PATTERNS in gate/workspace.py.
GATE_ARTIFACT_FILES = {
    "diff.txt", "changed_files.txt", "file_count.txt",
    "diff_stats.txt", "lines_changed.txt",
    "pr-metadata.json", "prior-review.json",
    "build.json", "triage.json", "architecture.json",
    "security.json", "logic.json", "verdict.json",
    "fix-build.json", "fix-diff.txt", "fix-env.json",
    "fix-resume-prompt.md", "fix-rereview.json",
    "no-codex.txt", "fix.json",
    # Polish-loop / hopper-pipeline state files (PR #217 regression
    # guard — these must never land in PR commits).
    "fix-polish.json", "fix-decomposition.json",
    # Scratch file senior Claude writes before each `gate-code <stage>
    # < gate-directions.md` invocation (Fix 3: replaces the deprecated
    # heredoc pattern). Never commit to the PR.
    "gate-directions.md",
}

GATE_ARTIFACT_GLOBS = [
    "*-findings.json", "*-result.json",
    "*-session-id.txt", "*-raw.json",
    "*.in.md", "*.out.md", "*.in.md.tmp",
    "*.codex.log",
]


# ── Fix Helpers ──────────────────────────────────────────────


def cleanup_artifacts(workspace: Path) -> list[str]:
    """Remove Gate review/fix artifact files from the worktree.

    Safety net before commit — the primary defense is the per-worktree
    git exclude configured in workspace._setup_artifact_exclusions().
    Returns the list of paths that were removed.
    """
    removed: list[str] = []

    # Static filenames
    for name in GATE_ARTIFACT_FILES:
        p = workspace / name
        if p.exists():
            p.unlink(missing_ok=True)
            removed.append(name)

    # Glob patterns (root-level only — Path.glob without ** does not recurse)
    for pattern in GATE_ARTIFACT_GLOBS:
        for p in workspace.glob(pattern):
            if p.is_file():
                name = p.name
                p.unlink(missing_ok=True)
                if name not in removed:
                    removed.append(name)

    # fix-build/ directory
    build_dir = workspace / "fix-build"
    if build_dir.is_dir():
        shutil.rmtree(build_dir, ignore_errors=True)
        removed.append("fix-build/")

    # .gate/ hopper-mode baseline marker directory (never ship to PRs)
    gate_marker = workspace / ".gate"
    if gate_marker.is_dir():
        shutil.rmtree(gate_marker, ignore_errors=True)
        removed.append(".gate/")

    # Unstage any removed paths still in the git index
    if removed:
        subprocess.run(
            ["git", "reset", "HEAD", "--"] + removed,
            capture_output=True,
            cwd=str(workspace),
            timeout=30,
        )
        logger.info(f"Cleaned up {len(removed)} artifact files: {removed}")

    return removed


def _match_glob(filepath: str, pattern: str) -> bool:
    """Match a file path against a glob pattern.

    Ported from matchGlob() in fix-loop-helpers.js.
    """
    if "*" not in pattern:
        return filepath == pattern

    if pattern.endswith("/**"):
        dir_prefix = pattern[:-3]
        return filepath == dir_prefix or filepath.startswith(dir_prefix + "/")

    escaped = re.escape(pattern).replace(r"\*", "[^/]*")
    return bool(re.match(f"^{escaped}$", filepath))


def enforce_blocklist(workspace: Path, config: dict | None = None) -> list[str]:
    """Revert changes to blocklisted files.

    Ported from enforceBlocklist() in fix-loop-helpers.js.
    """
    repo_cfg = (config or {}).get("repo", {})
    blocklist_path_str = repo_cfg.get("fix_blocklist", "")
    if blocklist_path_str:
        blocklist_path = Path(blocklist_path_str)
    else:
        blocklist_path = gate_dir() / "config" / "fix-blocklist.txt"
    try:
        content = blocklist_path.read_text()
    except OSError:
        return []

    patterns = [
        line.strip()
        for line in content.split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    if not patterns:
        return []

    changed = _get_changed_files(workspace)
    violations = []
    for filepath in changed:
        for pattern in patterns:
            if _match_glob(filepath, pattern):
                violations.append(filepath)
                _revert_file(workspace, filepath)
                break

    if violations:
        logger.info(f"Blocklist reverted {len(violations)} files: {violations}")
    return violations


def cleanup_gate_tests(workspace: Path) -> None:
    """Remove test files created by gate agents.

    Ported from cleanupGateTests() in fix-loop-helpers.js.
    """
    cwd = str(workspace)

    # Remove tests/gate/ directory
    gate_tests = workspace / "tests" / "gate"
    if gate_tests.exists():
        import shutil

        shutil.rmtree(gate_tests, ignore_errors=True)

    # Find and remove __gate_test_* and __gate_fix_test_* files
    for pattern in ("**/__gate_test_*", "**/__gate_fix_test_*"):
        for f in workspace.glob(pattern):
            try:
                f.unlink()
            except OSError:
                pass

    # Unstage any gate test files from the index
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            if "tests/gate/" in line or "__gate_test_" in line or "__gate_fix_test_" in line:
                subprocess.run(
                    ["git", "reset", "HEAD", "--", line],
                    capture_output=True,
                    cwd=cwd,
                )
                try:
                    (workspace / line).unlink(missing_ok=True)
                except OSError:
                    pass


def build_verify(
    workspace: Path,
    original_build: dict | None = None,
    config: dict | None = None,
) -> dict:
    """Run build and compare to original for pre-existing failure detection.

    Uses the project profile to determine which commands to run.
    Returns dict with pass, typecheck_errors, lint_errors, test_failures,
    typecheck_log, lint_log, and typecheck_tool.
    """
    from gate import profiles

    repo_cfg = (config or {}).get("repo", {})
    profile = profiles.resolve_profile(repo_cfg, workspace)
    project_type = profile.get("project_type", "none")

    typecheck_cmd = profile.get("typecheck_cmd", "")
    lint_cmd = profile.get("lint_cmd", "")
    test_cmd = profile.get("test_cmd", "")

    if not typecheck_cmd and not lint_cmd and not test_cmd:
        return {
            "pass": True,
            "typecheck_errors": 0,
            "lint_errors": 0,
            "test_failures": 0,
            "typecheck_log": "",
            "lint_log": "",
            "typecheck_tool": "",
        }

    cwd = str(workspace)
    build_dir = workspace / "fix-build"
    build_dir.mkdir(exist_ok=True)

    typecheck_tool = shlex.split(typecheck_cmd)[0] if typecheck_cmd else ""
    lint_tool = shlex.split(lint_cmd)[0] if lint_cmd else ""
    test_tool = shlex.split(test_cmd)[0] if test_cmd else ""

    if typecheck_cmd:
        tc_out, tc_exit = _run_silent(typecheck_cmd, cwd=cwd)
    else:
        tc_out, tc_exit = "", 0
    (build_dir / "typecheck.log").write_text(tc_out)

    if lint_cmd:
        lint_out, lint_exit = _run_silent(lint_cmd, cwd=cwd)
    else:
        lint_out, lint_exit = "", 0
    (build_dir / "lint.log").write_text(lint_out)

    if test_cmd:
        test_out, test_exit = _run_silent(test_cmd, cwd=cwd)
    else:
        test_out, test_exit = "", 0
    (build_dir / "test.log").write_text(test_out)

    build_result = builder.compile_build(
        typecheck_log=tc_out,
        typecheck_exit=tc_exit,
        lint_log=lint_out,
        lint_exit=lint_exit,
        test_log=test_out,
        test_exit=test_exit,
        project_type=project_type,
        typecheck_tool=typecheck_tool,
        lint_tool=lint_tool,
        test_tool=test_tool,
    )

    fix_build_path = workspace / "fix-build.json"
    fix_build_path.write_text(json.dumps(build_result, indent=2))

    passed = build_result.get("overall_pass", False)

    if not passed and original_build:
        compared = builder.compare_builds(original_build, build_result)
        passed = compared.get("overall_pass", False)
        if passed:
            logger.info("Pre-existing failures accepted")

    return {
        "pass": passed,
        "typecheck_errors": build_result.get("typecheck", {}).get("error_count", 0),
        "lint_errors": build_result.get("lint", {}).get("error_count", 0),
        "test_failures": build_result.get("tests", {}).get("failed", 0),
        "typecheck_log": tc_out[:5000],
        "lint_log": lint_out[:5000],
        "typecheck_tool": typecheck_tool,
    }


def write_diff(workspace: Path) -> None:
    """Write git diff to fix-diff.txt for re-review consumption.

    Ported from writeDiff() in run-fix-phases.js.
    """
    diff, _ = _run_silent(["git", "diff", "HEAD"], cwd=str(workspace))
    (workspace / "fix-diff.txt").write_text(diff or "(no changes)")


def sort_findings_by_severity(findings: list[dict]) -> list[dict]:
    """Sort findings by severity (critical first).

    Ported from sortFindingsBySeverity() in fix-loop-helpers.js.
    """
    return sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity", ""), 99))


def _get_changed_files(workspace: Path) -> list[str]:
    """Get list of changed, staged, and untracked files.

    Bounded by a 60s timeout so a hung git (e.g. a stuck index.lock) can't
    wedge the fix pipeline. Returns an empty list on timeout or missing git.
    """
    try:
        result = subprocess.run(
            ["sh", "-c",
             "{ git diff --name-only 2>/dev/null; "
             "git diff --cached --name-only 2>/dev/null; "
             "git ls-files --others --exclude-standard 2>/dev/null; } | sort -u"],
            capture_output=True,
            text=True,
            cwd=str(workspace),
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"_get_changed_files failed: {e}")
        return []
    return [f for f in result.stdout.strip().split("\n") if f]


def _revert_file(workspace: Path, filepath: str) -> None:
    """Revert a single file to its state in HEAD.

    For tracked files: restores the HEAD version (does not delete).
    For untracked files: removes the file from disk.

    Each git call is bounded by a 30s timeout; on timeout or missing git
    we log and return so the fix pipeline is never blocked by a hung git.
    """
    cwd = str(workspace)
    try:
        tracked = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{filepath}"],
            capture_output=True,
            cwd=cwd,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"_revert_file({filepath}) cat-file failed: {e}")
        return

    if tracked.returncode == 0:
        try:
            subprocess.run(
                ["git", "checkout", "HEAD", "--", filepath],
                capture_output=True,
                cwd=cwd,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"_revert_file({filepath}) checkout failed: {e}")
    else:
        full_path = workspace / filepath
        if full_path.exists():
            full_path.unlink(missing_ok=True)


def _revert_all(workspace: Path) -> None:
    """Revert all changes in the workspace.

    Each git call is bounded by a 30s timeout; on timeout or missing git
    we log and move on so the fix pipeline is never blocked by a hung git.
    """
    cwd = str(workspace)
    for cmd in (["git", "checkout", "--", "."], ["git", "clean", "-fd"]):
        try:
            subprocess.run(cmd, capture_output=True, cwd=cwd, timeout=30)
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"_revert_all {' '.join(cmd)} failed: {e}")


def _run_silent(cmd: str | list[str], cwd: str | None = None) -> tuple[str, int]:
    """Run a command silently, returning (combined output, exit_code). Never raises.

    ``cmd`` may be either a string (parsed with ``shlex.split``) or a
    pre-tokenized argv list. This replaces the earlier ``shell=True`` path so
    build/lint/test commands from config can never be interpreted as a shell
    script. ``stderr`` is redirected into ``stdout`` via
    ``stderr=subprocess.STDOUT`` so the kernel merges both streams in
    chronological emission order — true ``2>&1`` semantics.
    """
    try:
        args = cmd if isinstance(cmd, list) else shlex.split(cmd)
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            timeout=600,
        )
        return result.stdout, result.returncode
    except (subprocess.SubprocessError, OSError, ValueError):
        return "", 1


# ── Prompt Builders ──────────────────────────────────────────


def _build_build_error_prompt(build_result: dict) -> str:
    """Build the resume prompt for build errors.

    Ported from buildBuildErrorPrompt() in run-fix-loop.js.
    """
    tool_name = build_result.get("typecheck_tool") or "Type Check"
    parts = [
        "# Build Errors After Fix",
        "",
        "The external build verification found errors after your fix session.",
        "You still have full context of what you changed. Fix these errors.",
        "",
    ]
    if build_result.get("typecheck_errors", 0) > 0:
        parts.append(f"## {tool_name} Errors ({build_result['typecheck_errors']})")
        parts.append("```")
        parts.append(build_result.get("typecheck_log", "(no log captured)"))
        parts.append("```")
        parts.append("")
    if build_result.get("lint_errors", 0) > 0:
        parts.append(f"## Lint Errors ({build_result['lint_errors']})")
        parts.append("```")
        parts.append(build_result.get("lint_log", "(no log captured)"))
        parts.append("```")
        parts.append("")
    parts.append("Use `gate-code implement` to fix these errors. Then run verification again.")
    return "\n".join(parts)


def _build_rereview_feedback_prompt(rereview_result: dict) -> str:
    """Build the resume prompt for re-review feedback.

    Ported from buildRereviewFeedbackPrompt() in run-fix-loop.js.
    """
    parts = [
        "# Re-Review Feedback",
        "",
        "An independent reviewer found issues with your fix. Fix them.",
        "",
        "```json",
        json.dumps(rereview_result, indent=2)[:3000],
        "```",
        "",
        "Address each issue, then output your updated JSON summary.",
    ]
    return "\n".join(parts)


def _build_codex_bootstrap_prompt() -> str:
    """Load the gate-junior.md prompt for Codex bootstrap."""
    try:
        return prompt.load("gate-junior")
    except FileNotFoundError:
        return "You are a junior engineer. Follow the senior's directions precisely."


# ── Finding helpers (audit A2, 1E.ii, 4B) ────────────────────

# Keywords used to heuristically classify a finding's fixability when the
# triage agent has not annotated it. Kept intentionally narrow: false
# negatives (→ "unknown") are safe; false positives (→ "trivial") would
# encourage the senior to attempt broad fixes under a tight per-finding
# budget, which is the exact failure mode the polish loop is designed to
# avoid.
_TRIVIAL_KEYWORDS = (
    "add comment", "add a comment", "explanatory comment",
    "justification comment", "missing jsdoc", "add jsdoc",
    "missing import", "unused import", "rename ", "typo",
    "add type annotation", "missing type annotation",
    "eslint-disable", "no-img-element",
    "without an explanatory", "without justification",
)
_BROAD_KEYWORDS = (
    "split file", "extract ", "duplicate ", "refactor",
    "move to separate", "introduce abstraction", "rewrite ",
    "line ceiling", "exceeding the", "exceeds the",
    "over the ", "hard ceiling", "soft target",
)


def classify_fixability(finding: dict) -> str:
    """Classify a finding by how mechanical a fix is likely to be.

    Returns one of ``"trivial" | "scoped" | "broad" | "unknown"``. The
    classification is intentionally heuristic — triage may override it
    later by setting ``finding["fixability"]`` directly.
    """
    if isinstance(finding.get("fixability"), str):
        val = finding["fixability"].lower()
        if val in ("trivial", "scoped", "broad", "unknown"):
            return val

    message = str(finding.get("message", "")).lower()
    title = str(finding.get("title", "")).lower()
    text = f"{title} {message}"

    # Broad signals dominate: "over the line ceiling" findings explicitly
    # require splitting files, and we do not want the senior to attempt
    # those in polish mode.
    for kw in _BROAD_KEYWORDS:
        if kw in text:
            return "broad"
    for kw in _TRIVIAL_KEYWORDS:
        if kw in text:
            return "trivial"
    return "unknown"


# Natural-language cues that the reviewer was uncertain about the intended
# behavior. Heuristic v1 — a later version could replace this with a
# small structured LLM classifier call. Keep the list short and
# high-precision to avoid over-flagging unambiguous findings.
_AMBIGUITY_HIGH_KEYWORDS: tuple[str, ...] = (
    "could mean",
    "ambiguous",
    "unclear whether",
    "either ",
    "intended behavior",
    "it is unclear",
    "may be",
    "might mean",
    "interpret",
)


def classify_ambiguity(finding: dict) -> str:
    """Classify how much interpretation a finding requires.

    Returns one of ``"none" | "low" | "high"``:

    - ``"high"`` — the finding's text signals multiple plausible
      interpretations (uses cues like "could mean", "ambiguous"),
      meaning a mechanical fix is likely to silently pick the wrong
      interpretation. Fix pipelines MUST halt on these and ask the
      author unless ``halt_on_ambiguity`` is disabled.
    - ``"low"`` — the reviewer did not propose a concrete suggestion.
      A fix can still be attempted but the senior should pick the
      safest interpretation consistent with existing tests.
    - ``"none"`` — unambiguous finding, proceed with the normal flow.

    Safe to override per-finding by setting ``finding["ambiguity"]``
    directly (e.g. from a future LLM classifier stage).

    Mirrors the structure of :func:`classify_fixability` so both tags
    are driven by the same ``tag_findings`` pass.
    """
    if isinstance(finding.get("ambiguity"), str):
        val = finding["ambiguity"].lower()
        if val in ("none", "low", "high"):
            return val
    message = str(finding.get("message", "")).lower()
    title = str(finding.get("title", "")).lower()
    text = f"{title} {message}"
    for kw in _AMBIGUITY_HIGH_KEYWORDS:
        if kw in text:
            return "high"
    suggestion = finding.get("suggestion")
    if not isinstance(suggestion, str) or not suggestion.strip():
        return "low"
    return "none"


def tag_findings(findings: list[dict]) -> list[dict]:
    """Annotate each finding with a stable ``finding_id``, ``fixability``,
    and ``ambiguity``.

    Returns a new list of dicts (does not mutate the input). Missing fields
    default to safe values so callers never have to defend against
    ``None``.

    **Single-site tagging invariant (Phase 4):** this is the only place
    that computes ``ambiguity``. Both the polish path
    (``fixer_polish.run_polish_loop``) and the monolithic fix-senior
    path read the tag off the finding dict rather than recomputing,
    so heuristic changes ship in one commit.
    """
    tagged: list[dict] = []
    for f in findings:
        enriched = dict(f)
        enriched.setdefault("finding_id", compute_finding_id(f))
        if "fixability" not in enriched:
            enriched["fixability"] = classify_fixability(f)
        if "ambiguity" not in enriched:
            enriched["ambiguity"] = classify_ambiguity(f)
        tagged.append(enriched)
    return tagged


def fixability_summary(findings: list[dict]) -> str:
    """Return a one-line human-readable count, e.g. ``5 trivial, 3 scoped, 2 broad, 0 unknown``."""
    buckets = {"trivial": 0, "scoped": 0, "broad": 0, "unknown": 0}
    for f in findings:
        cls = f.get("fixability") or "unknown"
        if cls not in buckets:
            cls = "unknown"
        buckets[cls] += 1
    parts = [f"{buckets[k]} {k}" for k in ("trivial", "scoped", "broad", "unknown")]
    return ", ".join(parts)


def _validate_fix_json(
    data: dict | None,
    findings: list[dict] | None = None,
) -> tuple[list[str], dict]:
    """Validate and normalize a ``fix.json`` / ``fix-senior-findings.json`` payload.

    Contract (audit 4B, A15):

    - ``None`` and ``{}`` are treated identically — both return a warning
      and an empty normalized shape.
    - Every ``fixed[]`` entry is guaranteed to have ``file``, ``line``,
      ``finding_id``, ``finding_message`` and a non-placeholder
      ``fix_description``. Missing values are synthesized.
    - Every ``not_fixed[]`` entry is guaranteed to have ``reason`` and a
      non-empty ``detail``.

    ``findings`` is the original tagged finding list. When provided, the
    normalizer will resolve a missing ``finding_id`` on an entry by
    matching on ``(file, line)``.
    """
    warnings: list[str] = []
    if not data:
        warnings.append("fix.json missing or empty")
        return warnings, {"fixed": [], "not_fixed": [], "stats": {}}

    findings_by_fl: dict[tuple[str, int], dict] = {}
    if findings:
        for f in findings:
            try:
                key = (str(f.get("file", "")), int(f.get("line", 0) or 0))
            except (TypeError, ValueError):
                key = (str(f.get("file", "")), 0)
            findings_by_fl[key] = f

    def _lookup_finding_id(entry: dict) -> str:
        fid = entry.get("finding_id")
        if isinstance(fid, str) and fid:
            return fid
        try:
            key = (str(entry.get("file", "")), int(entry.get("line", 0) or 0))
        except (TypeError, ValueError):
            key = (str(entry.get("file", "")), 0)
        match = findings_by_fl.get(key)
        if match and isinstance(match.get("finding_id"), str):
            return match["finding_id"]
        # Fall back to computing an id from the entry itself so dedup
        # still has something stable to key on.
        return compute_finding_id(entry)

    fixed_raw = data.get("fixed") or []
    if not isinstance(fixed_raw, list):
        warnings.append("fix.json `fixed` is not a list")
        fixed_raw = []

    not_fixed_raw = data.get("not_fixed") or []
    if not isinstance(not_fixed_raw, list):
        warnings.append("fix.json `not_fixed` is not a list")
        not_fixed_raw = []

    normalized_fixed: list[dict] = []
    for entry in fixed_raw:
        if not isinstance(entry, dict):
            warnings.append("fixed[] entry is not a dict — dropping")
            continue
        out = dict(entry)
        out["finding_id"] = _lookup_finding_id(out)
        out.setdefault("file", "?")
        out.setdefault("line", 0)
        out.setdefault("finding_message", "(no finding_message)")
        desc = out.get("fix_description")
        if not isinstance(desc, str) or not desc.strip() or desc.strip().lower() == "fixed":
            warnings.append(
                f"fixed[] entry for {out.get('file')}:{out.get('line')} has no fix_description"
            )
            out["fix_description"] = (
                f"fix-senior modified {out.get('file', '?')} but did not describe the change"
            )
            out["_description_synthesized"] = True
        normalized_fixed.append(out)

    normalized_not_fixed: list[dict] = []
    for entry in not_fixed_raw:
        if not isinstance(entry, dict):
            warnings.append("not_fixed[] entry is not a dict — dropping")
            continue
        out = dict(entry)
        out["finding_id"] = _lookup_finding_id(out)
        out.setdefault("file", "?")
        out.setdefault("line", 0)
        out.setdefault("finding_message", "(no finding_message)")
        out.setdefault("reason", "deferred")
        detail = out.get("detail")
        if not isinstance(detail, str) or not detail.strip():
            warnings.append(
                f"not_fixed[] entry for {out.get('file')}:{out.get('line')} has no detail"
            )
            out["detail"] = "fix-senior did not provide a detail"
            out["_detail_synthesized"] = True
        normalized_not_fixed.append(out)

    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}

    # Hopper-mode extensions (Part 3 of the hardening plan). The legacy
    # shape stays unchanged; new keys default to empty so callers on the
    # polish_legacy path don't need to care.
    sub_scope_log_raw = data.get("sub_scope_log") or []
    if not isinstance(sub_scope_log_raw, list):
        warnings.append("fix.json `sub_scope_log` is not a list - ignoring")
        sub_scope_log_raw = []

    _valid_outcomes = {"committed", "reverted", "empty"}
    normalized_sub_scope_log: list[dict] = []
    for entry in sub_scope_log_raw:
        if not isinstance(entry, dict):
            warnings.append("sub_scope_log entry is not a dict - dropping")
            continue
        out = dict(entry)
        name = out.get("name")
        if not isinstance(name, str) or not name.strip():
            warnings.append("sub_scope_log entry missing name - dropping")
            continue
        out["name"] = name.strip()

        finding_ids = out.get("finding_ids") or []
        if not isinstance(finding_ids, list):
            warnings.append(
                f"sub_scope_log '{out['name']}' finding_ids is not a list"
            )
            finding_ids = []
        out["finding_ids"] = [str(x) for x in finding_ids if x]

        try:
            out["iterations"] = int(out.get("iterations") or 0)
        except (TypeError, ValueError):
            warnings.append(
                f"sub_scope_log '{out['name']}' iterations not an int"
            )
            out["iterations"] = 0

        outcome = out.get("outcome")
        if outcome not in _valid_outcomes:
            warnings.append(
                f"sub_scope_log '{out['name']}' outcome "
                f"'{outcome}' not in {sorted(_valid_outcomes)}"
            )
            out["outcome"] = "committed" if outcome is None else str(outcome)
        normalized_sub_scope_log.append(out)

    final_commit_message = data.get("final_commit_message")
    if final_commit_message is not None and not isinstance(final_commit_message, str):
        warnings.append("fix.json `final_commit_message` is not a string")
        final_commit_message = None
    if isinstance(final_commit_message, str):
        final_commit_message = final_commit_message.strip()

    return warnings, {
        "fixed": normalized_fixed,
        "not_fixed": normalized_not_fixed,
        "stats": stats,
        "sub_scope_log": normalized_sub_scope_log,
        "final_commit_message": final_commit_message or "",
    }


def _dedup_fixed(entries: list[dict]) -> list[dict]:
    """Dedup ``fixed[]`` entries by ``finding_id`` (preferring later entries).

    Later iterations see post-rereview feedback and usually produce better
    descriptions, so we keep the most recent entry for each id. Synthesized
    entries (audit A3) are keyed on a separate ``synth:<file>`` id so they
    never collapse into each other when multiple files were reconstructed
    from the diff.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for entry in entries:
        fid = entry.get("finding_id") or compute_finding_id(entry)
        if fid not in by_id:
            order.append(fid)
        by_id[fid] = entry
    return [by_id[fid] for fid in order]


# ── Fix Pipeline ─────────────────────────────────────────────


class FixPipeline:
    """Run the fix pipeline: senior Claude + junior Codex.

    Single continuous Claude session that delegates to Codex via gate-code.
    """

    def __init__(
        self,
        pr_number: int,
        repo: str,
        workspace: Path,
        verdict: dict,
        build: dict,
        config: dict,
        check_run_id: int | None = None,
        cancelled: threading.Event | None = None,
        socket_path: Path | None = None,
        review_id: str = "",
    ):
        self.pr_number = pr_number
        self.repo = repo
        self.workspace = workspace
        self.verdict = verdict
        self.build = build
        self.config = config
        self.check_run_id = check_run_id
        self._cancelled = cancelled or threading.Event()
        self.socket_path = socket_path
        self.review_id = review_id
        self.session_id: str | None = None
        self.codex_thread_id: str = ""
        self.branch = ""
        self.fix_pane_id: str | None = None
        self.is_polish = verdict.get("decision") == "approve_with_notes"
        self._state_dir = state.get_pr_state_dir(pr_number, repo)
        # Captured when ``run()`` starts, used by ``_commit_and_finish`` to
        # squash any intermediate polish/checkpoint commits. Without this,
        # ``gate-polish checkpoint`` commits leaked onto PR #217's
        # branch (see Part 1B / Part 2E of the hardening plan).
        self._pre_fix_sha: str = ""
        # Hopper-mode metrics — populated opportunistically so the
        # orchestrator can forward them to ``log_fix_result``. Defaults
        # are chosen so polish_legacy callers serialise nothing new.
        self._fix_start_monotonic: float = 0.0
        self._runaway_guard_hit: bool = False

    def _emit_fix_stage(self, stage: str) -> None:
        """Emit a fix stage update to the server (no-op if no connection)."""
        if self._connection:
            self._connection.emit(
                "review_stage_update",
                review_id=self.review_id,
                stage=stage,
                status="fixing",
            )

    def _start_watchdog(self) -> None:
        """Launch the hopper-mode runaway guard.

        Polls ``time.monotonic()`` once per 30 s on a daemon thread. When
        the overall wall-clock budget elapses, sets ``self._cancelled``
        (which every stage already respects) and flips
        ``self._runaway_guard_hit`` so the final ``FixResult`` + the
        reviews.jsonl entry carry the signal.

        Only active in hopper mode — polish_legacy has its own per-finding
        timeouts and doesn't need a global wall-clock cap.
        """
        from gate.config import (
            get_fix_pipeline_max_wall_clock_s,
            get_fix_pipeline_mode,
            get_fix_pipeline_senior_session_timeout_s,
        )

        if get_fix_pipeline_mode(self.config) != "hopper":
            return

        wall_cap = get_fix_pipeline_max_wall_clock_s(self.config)
        session_cap = get_fix_pipeline_senior_session_timeout_s(self.config)
        cap = max(wall_cap, session_cap)
        if cap <= 0:
            return

        start = self._fix_start_monotonic or time.monotonic()

        def _loop() -> None:
            while not self._cancelled.is_set():
                elapsed = time.monotonic() - start
                if elapsed >= cap:
                    logger.warning(
                        f"PR #{self.pr_number}: hopper watchdog tripped "
                        f"after {int(elapsed)}s (cap {cap}s) — cancelling"
                    )
                    self._runaway_guard_hit = True
                    self._cancelled.set()
                    try:
                        from gate.logger import write_live_log as _wll
                        _wll(
                            self.pr_number,
                            f"Hopper watchdog tripped at {int(elapsed)}s "
                            f"(cap {cap}s) — cancelling fix run",
                            prefix="fix", repo=self.repo,
                        )
                    except Exception:  # noqa: BLE001 — best-effort
                        pass
                    return
                # Bounded sleep so cancellation propagates within 30 s.
                self._cancelled.wait(timeout=min(30.0, cap - elapsed))

        t = threading.Thread(
            target=_loop,
            name=f"gate-hopper-watchdog-pr{self.pr_number}",
            daemon=True,
        )
        t.start()

    def run(self) -> FixResult:
        """Execute the fix pipeline."""
        if self._cancelled.is_set():
            return FixResult(success=False, summary="Cancelled before start")

        self._fix_start_monotonic = time.monotonic()
        self._start_watchdog()
        self._connection = None
        if self.socket_path and self.review_id:
            from gate.client import GateConnection
            self._connection = GateConnection(self.socket_path)
            self._connection.start()

        # Snapshot HEAD before any fix attempt so ``_commit_and_finish``
        # can ``git reset --soft`` over intermediate checkpoint commits.
        try:
            self._pre_fix_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.workspace),
                capture_output=True, text=True, check=True, timeout=10,
            ).stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning(
                f"PR #{self.pr_number}: could not snapshot pre-fix HEAD: {exc}"
            )
            self._pre_fix_sha = ""

        # Publish the baseline for ``gate checkpoint`` invoked by senior
        # Claude from inside the worktree. Hopper-mode subcommands read
        # ``.gate/pre-fix-sha`` to know what the "clean baseline" is
        # during save/revert/finalize and ``.gate/context.json`` to
        # route live-log / progress lines back to the correct PR.
        if self._pre_fix_sha:
            try:
                gate_dir_marker = self.workspace / ".gate"
                gate_dir_marker.mkdir(exist_ok=True)
                (gate_dir_marker / "pre-fix-sha").write_text(
                    self._pre_fix_sha + "\n"
                )
                (gate_dir_marker / "context.json").write_text(
                    json.dumps({
                        "pr_number": self.pr_number,
                        "repo": self.repo,
                    }) + "\n"
                )
            except OSError as exc:
                logger.warning(
                    f"PR #{self.pr_number}: could not write "
                    f".gate/pre-fix-sha marker: {exc}"
                )

        findings = self.verdict.get("findings", [])
        actionable = [f for f in findings if f.get("severity", "") != "info"]
        finding_count = len(actionable)

        # Check fix attempt limits
        allowed, reason = state.check_fix_limits(self.pr_number, self.config, repo=self.repo)
        if not allowed:
            github.comment_pr(
                self.repo,
                self.pr_number,
                f"**Gate Auto-Fix: Attempt limit reached** — {reason}",
            )
            return FixResult(success=False, reason=reason, summary=reason)

        triage = self._read_json("triage.json") or {}
        risk_level = triage.get("risk_level", "medium")
        notify.fix_started(self.pr_number, finding_count, risk_level, self.repo)
        write_live_log(self.pr_number, "Fix pipeline starting...", prefix="fix", repo=self.repo)

        try:
            # Sort findings by severity (critical first)
            if findings:
                sorted_findings = sort_findings_by_severity(findings)
                self.verdict["findings"] = sorted_findings
                (self.workspace / "verdict.json").write_text(
                    json.dumps(self.verdict, indent=2)
                )

            # Bootstrap Codex session
            write_live_log(self.pr_number, "Bootstrapping Codex...", prefix="fix", repo=self.repo)
            codex_prompt = _build_codex_bootstrap_prompt()
            exit_code, codex_thread_id = bootstrap_codex(
                codex_prompt, str(self.workspace), env=build_claude_env()
            )

            if codex_thread_id:
                self.codex_thread_id = codex_thread_id
                logger.info(f"Codex session bootstrapped: {codex_thread_id[:8]}")
            else:
                logger.warning("Codex bootstrap failed — senior will work without delegation")
                (self.workspace / "no-codex.txt").write_text("codex bootstrap failed")

            # Write env file for tmux-spawned runner (tmux doesn't inherit our env)
            fix_env = {
                "GATE_CODEX_THREAD_ID": self.codex_thread_id,
                "GATE_FIX_WORKSPACE": str(self.workspace),
            }
            (self.workspace / "fix-env.json").write_text(json.dumps(fix_env))

            all_fixed: list[dict] = []
            last_fix_json: dict | None = None

            # Tagged, fixability-classified findings — used for validation
            # and for dedup key resolution in _validate_fix_json. Read from
            # the actionable list so dedup aligns with what fix-senior
            # actually sees in its prompt.
            tagged_findings = tag_findings(actionable)

            # ── Polish path: one attempt per finding ─────────
            # Dispatched ONLY when the repo is pinned to the legacy
            # polish_legacy mode. Under the default hopper mode the
            # senior-driven monolithic path runs over all findings with
            # sub-scope checkpointing (see Part 3 of the hardening plan).
            from gate.config import get_fix_pipeline_mode, get_repo_bool
            pipeline_mode = get_fix_pipeline_mode(self.config)
            polish_enabled = get_repo_bool(
                self.config, "fix_polish_loop_enabled", True
            )
            if (
                pipeline_mode == "polish_legacy"
                and self.is_polish
                and polish_enabled
                and tagged_findings
            ):
                return self._run_polish_path(tagged_findings, finding_count)

            for iteration in range(1, MAX_ITERATIONS + 1):
                if self._cancelled.is_set():
                    return FixResult(success=False, summary="Cancelled")

                write_live_log(
                    self.pr_number,
                    f"Fix iteration {iteration}/{MAX_ITERATIONS}",
                    prefix="fix",
                    repo=self.repo,
                )

                # Run or resume the fix session
                self._emit_fix_stage("fix-session")
                if iteration == 1:
                    fix_result = self._run_fix_session()
                else:
                    rereview_data = self._read_json("fix-rereview.json") or {}
                    feedback = _build_rereview_feedback_prompt(rereview_data)
                    fix_result = self._resume_fix_session(feedback)

                last_fix_json = fix_result.get("fix_json")

                # Validate + normalize the senior's output (Group 4B)
                # before merging into all_fixed. The validator also
                # synthesizes missing fix_description / detail fields so
                # downstream commit messages never show literal "fixed".
                warnings, normalized = _validate_fix_json(last_fix_json, tagged_findings)
                for w in warnings:
                    logger.info(f"fix.json validation: {w}")
                new_fixed = normalized["fixed"]

                # Synthesize entries when the workspace has changes but
                # fixed[] is empty — signals that fix-senior wrote files
                # but skipped the report (Group 1E.iv). Tagged as
                # synth:<file> so the dedup helper keeps them distinct.
                if fix_result.get("has_changes") and not new_fixed:
                    changed = _get_changed_files(self.workspace)
                    for f in changed:
                        new_fixed.append({
                            "finding_id": f"synth:{f}",
                            "file": f,
                            "line": 0,
                            "finding_message": "unreported change",
                            "fix_description": (
                                f"Agent modified {f} but did not record a finding mapping"
                            ),
                            "_synthesized": True,
                        })

                all_fixed.extend(new_fixed)

                # Minimum-effort floor (Group 1E.iii): when the senior
                # skipped any trivial finding, re-prompt once in-place
                # so new_fixed picks up whatever the second pass lands.
                # Only runs on the first iteration to avoid infinite
                # loops and only when the senior is resumable.
                if iteration == 1 and self._reprompt_trivial_skips(
                    last_fix_json, tagged_findings
                ):
                    follow_up_json = self._read_json("fix.json") or self._read_json(
                        "fix-senior-findings.json"
                    )
                    _, follow_up_normalized = _validate_fix_json(
                        follow_up_json, tagged_findings
                    )
                    reprompt_fixed = follow_up_normalized["fixed"]
                    if reprompt_fixed:
                        all_fixed.extend(reprompt_fixed)
                        last_fix_json = follow_up_json
                        fix_result["has_changes"] = (
                            len(_get_changed_files(self.workspace)) > 0
                        )

                if not fix_result.get("has_changes"):
                    logger.info(f"Iteration {iteration}: no changes")
                    # Iter 1 with zero changes on approve_with_notes =
                    # graceful no-op (Group 1C). Checked before looping
                    # into iter 2 with empty re-review feedback.
                    if iteration == 1 and self._is_graceful_noop_case():
                        return self._graceful_noop_result(finding_count)
                    if iteration >= MAX_ITERATIONS:
                        state.record_fix_attempt(self.pr_number, repo=self.repo)
                        return FixResult(
                            success=False,
                            summary=(
                                f"Fix-senior produced no changes for {finding_count} "
                                f"findings after {iteration} iterations "
                                "(investigation required)"
                            ),
                        )
                    continue

                # Post-fix verification
                self._emit_fix_stage("fix-build")
                enforce_blocklist(self.workspace, config=self.config)
                cleanup_gate_tests(self.workspace)

                build_result = build_verify(self.workspace, self.build, config=self.config)

                # If build fails, resume session with build error context
                if not build_result["pass"]:
                    write_live_log(
                        self.pr_number,
                        "Build failed, resuming with errors"
                        f" (typecheck={build_result['typecheck_errors']})",
                        prefix="fix",
                        repo=self.repo,
                    )
                    error_prompt = _build_build_error_prompt(build_result)
                    self._resume_fix_session(error_prompt)

                    enforce_blocklist(self.workspace, config=self.config)
                    cleanup_gate_tests(self.workspace)
                    build_result = build_verify(self.workspace, self.build, config=self.config)

                if not build_result["pass"]:
                    logger.info(f"Build still failing after iteration {iteration}")
                    _revert_all(self.workspace)
                    if iteration >= MAX_ITERATIONS:
                        state.record_fix_attempt(self.pr_number, repo=self.repo)
                        notify.fix_failed(self.pr_number, "build failures", iteration, self.repo)
                        return FixResult(
                            success=False,
                            summary=f"Build failing after {iteration} iterations",
                        )
                    continue

                # Generate diff and run re-review
                write_diff(self.workspace)

                self._emit_fix_stage("fix-rereview")
                write_live_log(self.pr_number, "Running re-review...", prefix="fix", repo=self.repo)
                rereview_pass = self._run_rereview()

                if rereview_pass:
                    return self._commit_and_finish(
                        iteration, all_fixed, last_fix_json, finding_count
                    )

                logger.info(f"Re-review rejected iteration {iteration}")
                _revert_all(self.workspace)
                if iteration >= MAX_ITERATIONS:
                    state.record_fix_attempt(self.pr_number, repo=self.repo)
                    notify.fix_failed(self.pr_number, "re-review rejected", iteration, self.repo)
                    return FixResult(
                        success=False,
                        summary=f"Re-review rejected after {iteration} iterations",
                    )

            state.record_fix_attempt(self.pr_number, repo=self.repo)
            return FixResult(success=False, summary="All iterations exhausted")

        except FileNotFoundError as e:
            logger.warning(f"Fix pipeline aborted (workspace deleted): {e}")
            return FixResult(success=False, summary="Workspace deleted (cancelled)")
        except Exception as e:
            logger.exception(f"Fix pipeline failed for PR #{self.pr_number}")
            notify.fix_failed(self.pr_number, str(e), 0, self.repo)
            state.record_fix_attempt(self.pr_number, repo=self.repo)
            return FixResult(success=False, error=str(e), summary=f"Crash: {e}")
        finally:
            if self._connection:
                self._connection.stop()
                self._connection = None

    def _run_fix_session(self) -> dict:
        """Run the initial fix-senior session in tmux.

        Spawns fix-senior as an agent stage and waits for result.
        Returns {fix_json, has_changes}.
        """
        from gate.claude import spawn_review_stage
        from gate.tmux import kill_window

        result_file = self.workspace / "fix-senior-result.json"
        result_file.unlink(missing_ok=True)

        from gate.config import repo_slug
        review_id = (
            f"{repo_slug(self.repo)}-pr{self.pr_number}"
            if self.repo else f"pr{self.pr_number}"
        )
        pane_id = spawn_review_stage(
            review_id=review_id,
            stage="fix-senior",
            workspace=str(self.workspace),
            socket_path=str(self.socket_path) if self.socket_path else None,
            repo=self.repo,
        )
        if not pane_id:
            return {"fix_json": None, "has_changes": False}

        self.fix_pane_id = pane_id
        timeout = self.config.get("timeouts", {}).get("fix_session_s", 2400)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._cancelled.is_set():
                kill_window(pane_id)
                return {"fix_json": None, "has_changes": False}
            if result_file.exists():
                try:
                    data = json.loads(result_file.read_text())
                    logger.debug(f"Fix result: success={data.get('success', '?')}")
                    break
                except json.JSONDecodeError:
                    pass
            time.sleep(5)
        else:
            kill_window(pane_id)

        # Read back the session ID that ReviewRunner created so resume works
        session_file = self.workspace / "fix-senior-session-id.txt"
        if session_file.exists():
            self.session_id = session_file.read_text().strip()

        kill_window(pane_id)

        fix_json = self._read_json("fix.json") or self._read_json("fix-senior-findings.json")
        has_changes = len(_get_changed_files(self.workspace)) > 0

        return {"fix_json": fix_json, "has_changes": has_changes}

    def _resume_fix_session(self, resume_prompt: str) -> dict:
        """Resume the fix-senior session with new context.

        Writes a resume prompt and spawns a resumed session.

        We run claude with ``--print`` (non-interactive mode) and fully
        detached stdio so the subprocess never fights the parent process
        for a TTY. This matters whenever the orchestrator runs inside
        ``gate up`` (TUI mode): the Textual app owns the terminal, and a
        child ``claude`` that inherits the same stdio would deadlock both
        sides. Running headlessly (``gate up --headless``) also benefits:
        claude no longer tries to render an Ink UI into the LaunchAgent
        log stream. Output is captured to ``resume-stdout.log`` /
        ``resume-stderr.log`` in the workspace for debugging.

        Returns {fix_json, has_changes}.
        """
        if not self.session_id:
            logger.warning("No session ID available for resume")
            return {"fix_json": None, "has_changes": False}

        resume_path = self.workspace / "fix-resume-prompt.md"
        resume_path.write_text(resume_prompt)

        env = build_claude_env()
        env["GATE_CODEX_THREAD_ID"] = self.codex_thread_id
        env["GATE_FIX_WORKSPACE"] = str(self.workspace)

        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--print",
            "--resume",
            self.session_id,
            "--model",
            self.config.get("models", {}).get("fix_senior", "opus"),
            "--max-turns",
            str(RESUME_MAX_TURNS),
            resume_prompt,
        ]

        stdout_path = self.workspace / "resume-stdout.log"
        stderr_path = self.workspace / "resume-stderr.log"
        try:
            with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
                subprocess.run(
                    cmd,
                    env=env,
                    cwd=str(self.workspace),
                    timeout=self.config.get("timeouts", {}).get("fix_session_s", 2400),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                )
        except subprocess.TimeoutExpired as e:
            logger.warning(f"Resume session timed out after {e.timeout}s; killed")
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"Resume session failed: {e}")

        fix_json = self._read_json("fix.json") or self._read_json("fix-senior-findings.json")
        has_changes = len(_get_changed_files(self.workspace)) > 0

        return {"fix_json": fix_json, "has_changes": has_changes}

    def _run_rereview(self) -> bool:
        """Run the fix re-review (structured, inline).

        Ported from runReReview() in run-fix-phases.js.
        """
        try:
            template = prompt.load("fix-rereview")
        except FileNotFoundError:
            logger.warning("fix-rereview prompt not found, auto-passing")
            return True

        vars_dict = prompt.build_vars(self.workspace, "fix-rereview", {}, self.config)
        assembled = prompt.safe_substitute(template, vars_dict, "fixer-rereview")

        result = run_with_retry(
            lambda: StructuredRunner().run(
                "fix-rereview", assembled, self.workspace, self.config
            ),
            "fix-rereview",
            self.config,
        )

        # Write result
        (self.workspace / "fix-rereview.json").write_text(
            json.dumps(result.data, indent=2)
        )

        passed = result.data.get("pass") is not False
        logger.info(f"Re-review: {'pass' if passed else 'fail'}")
        return passed

    def _reprompt_trivial_skips(
        self,
        last_fix_json: dict | None,
        tagged_findings: list[dict],
    ) -> bool:
        """Minimum-effort floor re-prompt (Group 1E.iii / audit A7).

        When fix-senior returned a non-empty ``not_fixed`` that contains
        findings classified ``fixability == "trivial"``, resume the
        session with a focused re-prompt enumerating those findings.

        The re-prompt explicitly says *concrete blocker or fix* — it
        does NOT pressure the senior to fabricate fixes. A senior that
        has a real reason the trivial fix is unsafe can still record
        ``not_fixed`` with an expanded, specific ``detail``.

        Returns ``True`` if a re-prompt was dispatched, ``False`` if no
        trivial findings were skipped.

        Gated behind ``fix_pipeline.mode = "polish_legacy"``. Under
        hopper mode the senior plans the entire scope up-front and we
        don't want to re-prompt based on fixability classes (those are
        informational-only in hopper mode — see Part 3).
        """
        from gate.config import get_fix_pipeline_mode
        if get_fix_pipeline_mode(self.config) != "polish_legacy":
            return False
        if not self.session_id or not last_fix_json:
            return False
        not_fixed = last_fix_json.get("not_fixed") or []
        if not isinstance(not_fixed, list):
            return False

        trivial_ids = {
            f["finding_id"]
            for f in tagged_findings
            if f.get("fixability") == "trivial"
        }
        skipped_trivial = [
            entry for entry in not_fixed
            if isinstance(entry, dict)
            and entry.get("finding_id") in trivial_ids
        ]
        if not skipped_trivial:
            return False

        bullet_lines = "\n".join(
            f"- `{e.get('finding_id')}` {e.get('file')}:{e.get('line')} — "
            f"{e.get('finding_message', '')[:120]}"
            for e in skipped_trivial
        )
        reprompt = (
            "# Trivial findings were left unfixed\n\n"
            "The findings below were classified `trivial` (single-line or "
            "near-single-line mechanical fixes). You placed them in "
            "`not_fixed` without a concrete blocker.\n\n"
            "For each finding, either:\n\n"
            "1. **Fix it mechanically** (preferred) — dispatch `gate-code "
            "implement` with a one-sentence direction and re-emit the "
            "entry in `fixed[]` with the `finding_id` below.\n"
            "2. **Provide a concrete per-finding blocker** — file "
            "references, exact line numbers, and the specific reason the "
            "mechanical change is unsafe. Do NOT guess. Do NOT use "
            "placeholder detail text.\n\n"
            f"{bullet_lines}\n\n"
            "When you are done, rewrite `fix-senior-findings.json` with "
            "the updated entries (each trivial finding must appear "
            "exactly once in either `fixed[]` or `not_fixed[]`) and stop."
        )
        write_live_log(
            self.pr_number,
            f"Min-effort floor: re-prompting on {len(skipped_trivial)} "
            "skipped trivial findings",
            prefix="fix", repo=self.repo,
        )
        self._resume_fix_session(reprompt)
        return True

    def _run_polish_path(
        self,
        tagged_findings: list[dict],
        finding_count: int,
    ) -> FixResult:
        """Execute the per-finding polish loop and finalize the result.

        On zero changes we honor the ``graceful_noop_on_approve_with_notes``
        flag — same semantics as the monolithic iter-1 no-op case. On
        changes we run the structured re-review and then commit via
        ``_commit_and_finish`` so the audit-corrected pushed/no_diff/
        push_failed reporting applies uniformly.
        """
        from gate import fixer_polish

        aggregate = fixer_polish.run_polish_loop(self, tagged_findings)
        (self.workspace / "fix.json").write_text(json.dumps(aggregate, indent=2))

        all_fixed = list(aggregate.get("fixed", []))
        fixed_count = len(all_fixed)
        has_changes = len(_get_changed_files(self.workspace)) > 0

        if not has_changes:
            if self._is_graceful_noop_case():
                return self._graceful_noop_result(finding_count)
            state.record_fix_attempt(self.pr_number, repo=self.repo)
            return FixResult(
                success=False,
                summary=(
                    f"Polish loop produced no changes for {finding_count} "
                    "findings (investigation required)"
                ),
            )

        write_diff(self.workspace)
        self._emit_fix_stage("fix-rereview")
        rereview_pass = self._run_rereview()
        if not rereview_pass:
            _revert_all(self.workspace)
            state.record_fix_attempt(self.pr_number, repo=self.repo)
            notify.fix_failed(
                self.pr_number, "re-review rejected polish changes", 1, self.repo,
            )
            return FixResult(
                success=False,
                summary=(
                    f"Polish loop fixed {fixed_count}/{finding_count} findings "
                    "but re-review rejected the changes"
                ),
            )

        return self._commit_and_finish(1, all_fixed, aggregate, finding_count)

    def _is_graceful_noop_case(self) -> bool:
        """Whether zero-change iter 1 should be treated as a success no-op.

        Only applies to ``approve_with_notes`` verdicts, and only when the
        per-repo flag ``graceful_noop_on_approve_with_notes`` is true.
        Request-changes verdicts always fail on "no changes" — that is
        a legitimate regression signal (audit A9).
        """
        from gate.config import get_repo_bool
        if not self.is_polish:
            return False
        return get_repo_bool(self.config, "graceful_noop_on_approve_with_notes", True)

    def _graceful_noop_result(self, finding_count: int) -> FixResult:
        """Return a success no-op for approve_with_notes PRs with zero fixes.

        Records the attempt as a no-op (``no_op=True``) so it does not
        count against ``check_fix_limits`` and clears any prior
        ``fix_attempts.txt`` counter since no real attempt occurred
        (Group 5D, audit A9).
        """
        summary = (
            f"No mechanical fixes needed "
            f"(approve_with_notes — {finding_count} findings were notes only)"
        )
        write_live_log(self.pr_number, summary, prefix="fix", repo=self.repo)
        state.record_fix_attempt(self.pr_number, repo=self.repo, no_op=True)
        return FixResult(success=True, pushed=False, summary=summary)

    def _commit_and_finish(
        self,
        iteration: int,
        all_fixed: list[dict],
        last_fix_json: dict | None,
        finding_count: int,
    ) -> FixResult:
        """Commit, push, and return a status-accurate :class:`FixResult`.

        Splits the three commit outcomes that the old implementation
        collapsed into ``success=True``:

        - ``pushed``      → success, with a detailed PR comment
        - ``no_diff``     → failure, summary explains the empty push
        - ``push_failed`` → failure, summary includes the git error tail

        Ports ``commitAndPush()`` from run-fix-loop.js with the Group 1A
        semantics bolted on.
        """
        write_live_log(
            self.pr_number, "Re-review passed, committing...",
            prefix="fix", repo=self.repo,
        )

        # Dedup all_fixed by finding_id before reporting (Group 1B / A2)
        all_fixed = _dedup_fixed(all_fixed)
        real_fixed = [f for f in all_fixed if not str(f.get("finding_id", "")).startswith("synth:")]
        synth_fixed = [f for f in all_fixed if str(f.get("finding_id", "")).startswith("synth:")]
        # Public counts never exceed the input finding_count.
        fixed_count = min(len(real_fixed), finding_count)
        synth_count = len(synth_fixed)

        not_fixed_raw = (last_fix_json or {}).get("not_fixed", [])
        # Validate not_fixed too so synthesized details propagate into
        # the commit message / PR comment (Group 4B + 4C).
        _, normalized_nf = _validate_fix_json(
            {"fixed": [], "not_fixed": not_fixed_raw},
            tag_findings(self.verdict.get("findings", [])),
        )
        not_fixed = normalized_nf["not_fixed"]

        # Phase 4: post the author disambiguation comment (once per
        # digest) for every ``requires_author_disambiguation`` entry.
        # This runs regardless of commit outcome so the author sees
        # the question even when the push ultimately fails.
        self._post_disambig_comment_if_needed(not_fixed)

        commit_msg = f"fix(gate): auto-fix {fixed_count}/{finding_count} findings"
        if synth_count:
            commit_msg += f" (+{synth_count} file-level reconstructions)"
        commit_msg += " from Gate review"

        fixed_details = "\n".join(
            f"- {f.get('file', '?')} — {f.get('fix_description')}"
            for f in real_fixed
        )
        if fixed_details:
            commit_msg += f"\n\nFindings fixed:\n{fixed_details}"
        if synth_fixed:
            synth_lines = "\n".join(
                f"- {f.get('file', '?')} — {f.get('fix_description')}"
                for f in synth_fixed
            )
            commit_msg += (
                "\n\nFile-level changes reconstructed from diff "
                "(fix-senior did not report them):\n" + synth_lines
            )
        not_fixed_details = "\n".join(
            f"- {f.get('file', '?')} ({f.get('reason')}): {f.get('detail')}"
            for f in not_fixed
        )
        if not_fixed_details:
            commit_msg += f"\n\nNot fixed (require human action):\n{not_fixed_details}"

        synthesized_any = any(
            f.get("_description_synthesized") for f in real_fixed
        )
        if synthesized_any:
            commit_msg += (
                "\n\n_Some descriptions synthesized from diff; "
                "fix-senior omitted fix_description._"
            )

        cleanup_artifacts(self.workspace)

        # Squash any intermediate ``gate-polish checkpoint`` commits left
        # behind by fixer_polish._git_checkpoint. Without this the
        # checkpoints pushed straight through to the PR branch (see PR
        # #217 pollution in Part 1B). ``--soft`` keeps the final tree
        # state intact so the next commit captures the real fixes.
        if self._pre_fix_sha:
            try:
                subprocess.run(
                    ["git", "reset", "--soft", self._pre_fix_sha],
                    cwd=str(self.workspace),
                    check=True, capture_output=True, timeout=10,
                )
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                FileNotFoundError,
            ) as exc:
                logger.warning(
                    f"PR #{self.pr_number}: pre-commit reset "
                    f"to pre-fix sha failed: {exc}"
                )

        result = github.commit_and_push(self.workspace, commit_msg, branch=self.branch)

        if result.status == "no_diff":
            state.record_fix_attempt(self.pr_number, repo=self.repo)
            msg = (
                "Re-review passed but no diff to push "
                "(fix-senior made no file changes)"
            )
            write_live_log(self.pr_number, msg, prefix="fix", repo=self.repo)
            notify.fix_failed(self.pr_number, "no diff to push", iteration, self.repo)
            return FixResult(
                success=False,
                pushed=False,
                summary=msg,
                fixed_count=fixed_count,
                not_fixed_count=len(not_fixed),
            )

        if result.status == "push_failed":
            state.record_fix_attempt(self.pr_number, repo=self.repo)
            error_tail = (result.error or "unknown").splitlines()[-1]
            msg = f"Push rejected by remote: {error_tail}"
            write_live_log(self.pr_number, msg, prefix="fix", repo=self.repo)
            notify.fix_failed(self.pr_number, f"push failed: {error_tail}", iteration, self.repo)
            return FixResult(
                success=False,
                pushed=False,
                error=result.error,
                summary=msg,
                fixed_count=fixed_count,
                not_fixed_count=len(not_fixed),
            )

        new_sha = result.sha
        marker = self._state_dir / "fix-rereview-passed.txt"
        marker.write_text(new_sha)

        fixed_lines = "\n".join(
            f"- `{f.get('file', '?')}` — {f.get('fix_description')}"
            for f in real_fixed
        )
        not_fixed_lines = "\n".join(
            f"- `{f.get('file', '?')}` ({f.get('reason')}): {f.get('detail')}"
            for f in not_fixed
        )
        not_fixed_count = len(not_fixed)
        body = ""
        if not_fixed_count > 0:
            body += (
                f"> **Gate Auto-Fix: {fixed_count} fixed, "
                f"{not_fixed_count} require human action**\n\n"
            )
        body += "## Gate Auto-Fix Applied\n\n"
        body += f"Fixed {fixed_count}/{finding_count} findings "
        if synth_count:
            body += f"(plus {synth_count} file-level reconstructions) "
        body += f"in {iteration} iteration(s) ({new_sha[:8]}).\n\n"
        if fixed_lines:
            body += f"**Fixed:**\n{fixed_lines}\n\n"
        if not_fixed_lines:
            body += f"**Not fixed (require human action):**\n{not_fixed_lines}\n"
        if synthesized_any:
            body += (
                "\n_Some descriptions synthesized from the diff because "
                "fix-senior omitted `fix_description`._\n"
            )
        github.comment_pr(self.repo, self.pr_number, body)

        state.record_fix_attempt(self.pr_number, repo=self.repo)
        notify.fix_complete(self.pr_number, fixed_count, finding_count, iteration, self.repo)

        summary = (
            f"Fixed {fixed_count}/{finding_count} findings in {iteration} iteration(s)"
        )
        if synth_count:
            summary += f" (+{synth_count} reconstructions)"
        write_live_log(self.pr_number, summary, prefix="fix", repo=self.repo)

        # Hopper-mode observability: summarise the senior's sub_scope_log
        # (validated by ``_validate_fix_json``) so the orchestrator can
        # forward the counts to reviews.jsonl. polish_legacy runs leave
        # these at their zero defaults.
        _, normalized = _validate_fix_json(
            last_fix_json, tag_findings(self.verdict.get("findings", []))
        )
        sub_scope_log = normalized.get("sub_scope_log") or []
        sub_scope_total = len(sub_scope_log)
        sub_scope_committed = sum(
            1 for s in sub_scope_log if s.get("outcome") == "committed"
        )
        sub_scope_reverted = sum(
            1 for s in sub_scope_log if s.get("outcome") == "reverted"
        )
        sub_scope_empty = sum(
            1 for s in sub_scope_log if s.get("outcome") == "empty"
        )

        from gate.config import get_fix_pipeline_mode
        pipeline_mode = get_fix_pipeline_mode(self.config)
        wall_clock = (
            int(time.monotonic() - self._fix_start_monotonic)
            if self._fix_start_monotonic
            else 0
        )

        return FixResult(
            success=True,
            pushed=True,
            summary=summary,
            fixed_count=fixed_count,
            not_fixed_count=not_fixed_count,
            pipeline_mode=pipeline_mode,
            sub_scope_total=sub_scope_total,
            sub_scope_committed=sub_scope_committed,
            sub_scope_reverted=sub_scope_reverted,
            sub_scope_empty=sub_scope_empty,
            wall_clock_seconds=wall_clock,
            runaway_guard_hit=self._runaway_guard_hit,
        )

    def _read_json(self, filename: str) -> dict | None:
        """Read a JSON file from the workspace."""
        path = self.workspace / filename
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    # ── Phase 4: ambiguity disambiguation comment ──────────────

    @staticmethod
    def _disambig_digest(entry: dict) -> str:
        """Stable 16-char digest for a disambiguation ``not_fixed`` entry.

        Shape: ``sha256(finding_id::file::line::message[:200])[:16]``.
        Same finding on the same PR across fix-reruns produces the
        same digest — the dedup set in ``disambig_asked.txt``
        guarantees we post at most one question per digest.
        """
        import hashlib

        raw = (
            f"{entry.get('finding_id', '')}::"
            f"{entry.get('file', '')}::"
            f"{entry.get('line', '')}::"
            f"{str(entry.get('finding_message', ''))[:200]}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _post_disambig_comment_if_needed(self, not_fixed: list[dict]) -> None:
        """Post a single combined disambiguation comment (if any, and if
        not already posted for these digests).

        Dedup state lives in ``state/<slug>/pr<N>/disambig_asked.txt``
        (one 16-char digest per line). The cap
        ``max_disambig_questions_per_pr`` (default 2) keeps the comment
        short and prevents flooding.

        Fails open: any exception logs and returns — a broken dedup
        store must never block the fix pipeline's commit.
        """
        repo_cfg = self.config.get("repo", {})
        if not repo_cfg.get("halt_on_ambiguity", True):
            return
        disambig = [
            e for e in not_fixed
            if e.get("reason") == "requires_author_disambiguation"
        ]
        if not disambig:
            return
        try:
            asked_path = (
                state.get_pr_state_dir(self.pr_number, self.repo)
                / "disambig_asked.txt"
            )
            already: set[str] = set()
            if asked_path.exists():
                already = set(
                    line.strip()
                    for line in asked_path.read_text().splitlines()
                    if line.strip()
                )
            fresh = [
                e for e in disambig
                if self._disambig_digest(e) not in already
            ]
            cap = int(repo_cfg.get("max_disambig_questions_per_pr", 2))
            # Audit fix N4 — when ``cap`` is 0 the repo has explicitly
            # opted out of asking questions. We must not bump the stale
            # counter in that case (which would otherwise eventually
            # trip ``force_safest_interpretation`` despite the repo's
            # opt-out).
            if cap <= 0:
                return
            fresh = fresh[:cap]
            if not fresh:
                # Increment a stale counter so the orchestrator can
                # eventually force the safest interpretation.
                self._bump_disambig_stale_count(disambig)
                return
            body = self._build_disambig_comment(fresh)
            try:
                github.comment_pr(self.repo, self.pr_number, body)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"PR #{self.pr_number}: disambig comment_pr failed: {e}"
                )
                return
            updated = sorted(
                already | {self._disambig_digest(e) for e in fresh}
            )
            asked_path.write_text("\n".join(updated) + "\n")
            # Audit fix W8 — the stale counter is for unanswered
            # questions only. Posting a fresh batch starts a brand-new
            # waiting clock, so reset the counter to 0. Without this,
            # a single trip past ``max_disambig_stale_retries``
            # permanently locks ``force_safest_interpretation`` for the
            # remainder of the PR's life — including subsequent pushes
            # that introduce legitimately new ambiguous findings.
            self._reset_disambig_stale_count()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"PR #{self.pr_number}: disambig comment pipeline failed: {e}"
            )

    def _bump_disambig_stale_count(self, disambig: list[dict]) -> None:
        """Increment the stale-counter for pending disambig questions.

        When every pending digest has been re-seen without an answer
        for ``max_disambig_stale_retries`` reviews, the orchestrator
        will pass ``force_safest_interpretation=true`` to fix-senior
        so the pipeline stops blocking on the author.
        """
        try:
            count_path = (
                state.get_pr_state_dir(self.pr_number, self.repo)
                / "disambig_stale_count.txt"
            )
            prior = 0
            if count_path.exists():
                try:
                    prior = int(count_path.read_text().strip() or "0")
                except ValueError:
                    prior = 0
            count_path.write_text(str(prior + 1))
        except Exception as e:  # noqa: BLE001
            logger.info(
                f"PR #{self.pr_number}: disambig stale counter bump failed: {e}"
            )

    def _reset_disambig_stale_count(self) -> None:
        """Zero the stale counter when a fresh question is posted.

        See audit fix W8 — without this, a single trip past
        ``max_disambig_stale_retries`` would permanently lock
        ``force_safest_interpretation`` for the rest of the PR's life.
        """
        try:
            count_path = (
                state.get_pr_state_dir(self.pr_number, self.repo)
                / "disambig_stale_count.txt"
            )
            if count_path.exists():
                count_path.write_text("0")
        except Exception as e:  # noqa: BLE001
            logger.info(
                f"PR #{self.pr_number}: disambig stale counter reset failed: {e}"
            )

    @staticmethod
    def _build_disambig_comment(fresh: list[dict]) -> str:
        """Render the PR comment body for a batch of fresh disambig entries."""
        lines: list[str] = [
            "## Gate — Ambiguity Halt",
            "",
            (
                "Gate's fix pipeline detected that the following "
                f"{len(fresh)} finding(s) admit multiple plausible fixes "
                "with materially different observable behavior. Rather "
                "than pick an interpretation silently, Gate is asking "
                "for your input before attempting a mechanical change."
            ),
            "",
        ]
        for i, entry in enumerate(fresh, start=1):
            lines.append(f"### {i}. `{entry.get('file', '?')}` "
                         f"line {entry.get('line', '?')}")
            lines.append("")
            detail = str(entry.get("detail") or "").strip()
            if detail:
                lines.append(detail)
            else:
                msg = str(entry.get("finding_message") or "").strip()
                lines.append(f"**Finding:** {msg or '(no finding_message)'}")
            lines.append("")
        lines.append(
            "Reply on this PR with the intended behavior and Gate will "
            "resume on the next push. This question will not be re-asked "
            "for these findings until the text changes."
        )
        return "\n".join(lines)
