"""Prompt loading and variable substitution.

Ported from:
- safeSubstitute() in shared/utils.js
- buildVars() and buildDiffOrSummary() in run-stage.js
"""

import json
import logging
import re
import subprocess
from pathlib import Path

from gate import profiles
from gate.config import gate_dir, get_fix_pipeline_mode

logger = logging.getLogger(__name__)


def prompts_dir() -> Path:
    """Return the prompts directory (shipped with the package)."""
    return gate_dir() / "prompts"


MAX_DIFF_BYTES = 500_000
MAX_PR_BODY_BYTES = 50_000
MAX_FILE_LIST_BYTES = 50_000
TRIAGE_DIFF_BUDGET_BYTES = 150_000


def safe_substitute(template: str, vars: dict[str, str], caller: str = "") -> str:
    """Replace $var_name tokens. Leave unresolved tokens in place and log them.

    Ported from safeSubstitute() in shared/utils.js.
    Uses a regex that matches $variable_name tokens (lowercase + underscore).

    Defensive coercion: ``vars`` is typed as ``dict[str, str]``, but upstream
    code occasionally leaks non-string values (notably when a stage JSON
    file's ``summary`` field comes back as a dict — see PR #216 architecture
    stage). Any non-string value is coerced with ``json.dumps`` (for
    dict/list) or ``str`` (everything else) before substitution so that
    ``re.sub``'s internal ``str.join`` can never raise
    ``TypeError: sequence item N: expected str instance, dict found``.
    """

    def _coerce(key: str, value: object) -> str:
        if isinstance(value, str):
            return value
        logger.warning(
            f"[{caller}] variable ${key} had non-string type "
            f"{type(value).__name__}; coercing to string"
        )
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, indent=2)
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    def replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in vars:
            return _coerce(key, vars[key])
        logger.warning(f"[{caller}] unresolved variable: ${key}")
        return match.group(0)

    return re.sub(r"\$([a-z_][a-z0-9_]*)", replacer, template)


def load(name: str) -> str:
    """Load a prompt template by name.

    Looks for prompts/<name>.md in the gate directory.

    Args:
        name: Prompt name (with or without .md extension).

    Returns:
        The prompt template text.

    Raises:
        FileNotFoundError: If the prompt file doesn't exist.
    """
    base = prompts_dir()
    if name.endswith(".md"):
        path = base / name
    else:
        path = base / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text().strip()


def truncate(text: str, max_bytes: int, label: str) -> str:
    """Truncate text to max_bytes, appending a truncation notice.

    Ported from truncate() in shared/utils.js.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + f"\n\n[{label} truncated at {max_bytes // 1024}KB]"


def _read_file(path: Path) -> str:
    """Read a file, returning empty string on any error."""
    try:
        return path.read_text()
    except (OSError, FileNotFoundError):
        return ""


def _read_json_file(path: Path) -> dict | None:
    """Read and parse a JSON file, returning None on any error."""
    raw = _read_file(path)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _stage_summary(
    data: dict | None,
    fallback: str,
) -> str:
    """Return a string summary for a stage JSON payload.

    Claude occasionally emits ``summary`` as a dict (e.g. a per-severity
    count object like ``{"errors": 4, "warnings": 14, "info": 3}``) rather
    than the expected human-readable sentence. Returning non-str values
    from ``build_vars`` propagates into :func:`safe_substitute` and, prior
    to the defensive coercion there, crashed the next stage with
    ``TypeError: sequence item N: expected str instance, dict found``.

    This helper keeps the prompt variable shape strictly ``str`` and
    produces a legible one-line representation of count-dicts so the
    downstream model still sees the information.
    """
    if not data:
        return fallback
    summary = data.get("summary")
    if isinstance(summary, str):
        return summary
    if isinstance(summary, dict):
        # Render "key: value, key: value" so downstream stages still see
        # the per-severity counts rather than a raw dict repr.
        if summary and all(isinstance(v, (int, float, str)) for v in summary.values()):
            return ", ".join(f"{k}: {v}" for k, v in summary.items())
        try:
            return json.dumps(summary)
        except (TypeError, ValueError):
            return str(summary)
    if isinstance(summary, list):
        try:
            return json.dumps(summary)
        except (TypeError, ValueError):
            return str(summary)
    if summary is None:
        return fallback
    return str(summary)


def build_diff_or_summary(
    workspace: Path,
    budget_bytes: int = TRIAGE_DIFF_BUDGET_BYTES,
    config: dict | None = None,
) -> str:
    """Full diff if under budget, otherwise per-file summary.

    Ported from buildDiffOrSummary() in run-stage.js.
    """
    diff = _read_file(workspace / "diff.txt")
    if len(diff.encode("utf-8")) <= budget_bytes:
        return diff

    default_branch = (config or {}).get("repo", {}).get("default_branch", "main")
    diff_ref = f"origin/{default_branch}...HEAD"

    diff_stats = _read_file(workspace / "diff_stats.txt")
    changed_files = [
        f for f in _read_file(workspace / "changed_files.txt").split("\n") if f.strip()
    ]

    parts = [
        f"[Note: Full diff exceeds {budget_bytes // 1024}KB — showing per-file summary "
        f"for triage classification. Full diff is available to downstream review stages.]\n",
        f"## Diff Stats\n{diff_stats}\n",
        "## Per-File Preview (first 10 lines of each file's diff)\n",
    ]

    for file in changed_files:
        try:
            result = subprocess.run(
                ["git", "diff", diff_ref, "--", file],
                capture_output=True,
                text=True,
                cwd=str(workspace),
                timeout=5,
            )
            file_diff = result.stdout.strip()
            lines = file_diff.split("\n")[:15]
            file_diff = "\n".join(lines)
            parts.append(f"### {file}\n```\n{file_diff}\n```\n")
        except (subprocess.SubprocessError, OSError):
            parts.append(f"### {file}\n[Could not extract file diff]\n")

    return "\n".join(parts)


_POLISH_MODE_SECTION = """## Mode: polish (approve_with_notes)

The reviewer already approved this PR. These findings are polish items: \
missing comments, naming inconsistencies, small typing gaps, barrel \
re-exports, new nanostores without justification comments. They are \
*expected* to be fixable with one-to-few line additions.

Finding breakdown: $fixability_summary_inline

Rules for polish mode (override earlier guidance where they conflict):

- You MUST attempt every finding classified `trivial` or `scoped`.
- You MUST NOT return `fixed: []` while non-empty findings exist unless you \
have recorded every finding in `not_fixed` with a specific `reason` and a \
non-placeholder `detail` (at least 20 chars explaining the concrete blocker).
- `deferred` is NOT a valid reason in polish mode. Choose `blocked_file`, \
`would_break_build`, `too_broad` (with a file count), or `requires_architecture_change`.
- Prefer small, surgical edits (JSDoc on new exports, justification \
comments on new stores) over larger rewrites — EXCEPT for \
`eslint-disable` / `ts-ignore` / `ts-expect-error` findings: see the \
disable-directives rule below.
- A clean build still matters — but the bar is "leave 5 trivial fixes \
landed", not "attempt zero fixes to guarantee zero regressions".

### Disable-directives (`eslint-disable`, `ts-ignore`, `ts-expect-error`)

The default fix for a finding about an unjustified disable line is to \
**remove the disable directive and fix the underlying rule violation**. \
Adding a justification comment is a fallback, not a shortcut: use it only \
when the disable is genuinely required (third-party API shape, a \
narrow known-safe exception, a platform limitation) and the comment must \
cite the specific technical reason on the same line or the line above.

`// eslint-disable-next-line` without a follow-on `// because …` \
citation is NOT a valid fix for a finding about an unjustified disable.

Every entry in `fixed[]` and `not_fixed[]` MUST include a `finding_id` field \
copied verbatim from the `Findings to Fix` payload above. Do not invent ids.
"""

_STRICT_MODE_SECTION = """## Mode: strict (request_changes)

These findings are blocking issues. Your job is to fix every finding with \
real code changes. Skipping is only acceptable when the fix would break the \
build, touch a blocked file, or require more than 8 files of churn.

### Disable-directives (`eslint-disable`, `ts-ignore`, `ts-expect-error`)

The default fix for a finding about an unjustified disable line is to \
**remove the disable directive and fix the underlying rule violation**. \
Adding a justification comment is a fallback, not a shortcut: use it only \
when the disable is genuinely required (third-party API shape, a \
narrow known-safe exception, a platform limitation) and the comment must \
cite the specific technical reason. A bare \
`// eslint-disable-next-line` with no technical reason is never an \
acceptable fix for an unjustified-disable finding in strict mode.

Every entry in `fixed[]` and `not_fixed[]` MUST include a `finding_id` field \
copied verbatim from the `Findings to Fix` payload above. Do not invent ids.
"""


def _build_polish_mode_section(fix_mode: str, fixability_summary: str) -> str:
    """Render the mode-specific guidance block injected into fix-senior.md.

    Kept in prompt.py (not fixer.py) because build_vars is the single place
    that assembles the final template and we want the block to follow the
    same substitution rules as other variables.
    """
    if fix_mode == "polish":
        section = _POLISH_MODE_SECTION
    else:
        section = _STRICT_MODE_SECTION
    return section.replace("$fixability_summary_inline", fixability_summary)


def _build_hopper_mode_section(pipeline_mode: str) -> str:
    """Return the hopper-mode addendum block, empty in polish_legacy mode.

    Keeps the legacy prompt path unchanged so we can roll back by flipping
    ``fix_pipeline.mode`` without re-templating the prompt.

    Loaded from ``prompts/fix-senior-hopper.md`` so the addendum lives
    alongside every other prompt template (triage, build, fix-senior,
    …) and stays editable without a Python rebuild — per the
    "prompts belong in prompts/*.md" convention.
    """
    if pipeline_mode == "hopper":
        return load("fix-senior-hopper")
    return ""


def build_vars(
    workspace: Path,
    stage: str,
    env_vars: dict,
    config: dict | None = None,
) -> dict[str, str]:
    """Assemble all template variables for a stage.

    Ported from buildVars() in run-stage.js. Reads context files from the
    workspace and PR metadata from env_vars.

    Args:
        workspace: Path to the review worktree.
        stage: Stage name (triage, architecture, etc.).
        env_vars: Dict with PR metadata (pr_title, pr_body, pr_author, etc.).
        config: Optional gate.toml config dict. Limits read from [limits] section.

    Returns:
        Dict of variable name -> value for template substitution.
    """
    limits = (config or {}).get("limits", {})
    max_diff = limits.get("max_diff_bytes", MAX_DIFF_BYTES)
    max_pr_body = limits.get("max_pr_body_bytes", MAX_PR_BODY_BYTES)
    max_file_list = limits.get("max_file_list_bytes", MAX_FILE_LIST_BYTES)

    diff = _read_file(workspace / "diff.txt")
    changed_files = _read_file(workspace / "changed_files.txt")
    file_count = _read_file(workspace / "file_count.txt").strip()
    lines_changed = _read_file(workspace / "lines_changed.txt").strip()

    build_json = _read_file(workspace / "build.json")
    triage_json = _read_file(workspace / "triage.json")
    architecture_json = _read_file(workspace / "architecture.json")
    security_json = _read_file(workspace / "security.json")
    logic_json = _read_file(workspace / "logic.json")

    triage = _read_json_file(workspace / "triage.json")
    architecture = _read_json_file(workspace / "architecture.json")
    security = _read_json_file(workspace / "security.json")

    # Expose `change_intent` as its own template var so logic/verdict
    # prompts can reference the author's claimed intent without having
    # to parse the full triage JSON. Defaults to "{}" when triage is
    # missing or predates the change_intent field — safe for cached
    # state from older Gate versions.
    change_intent = (triage or {}).get("change_intent") or {}
    change_intent_json = json.dumps(change_intent, indent=2)

    # Postconditions stage output. Empty array when the stage was
    # skipped (low-risk PR, fast-track, or fallback on failure) —
    # prompts should treat an empty list as "no postconditions to
    # check" rather than an error.
    postconditions_parsed = _read_json_file(workspace / "postconditions.json")
    if isinstance(postconditions_parsed, dict):
        postconditions_list = postconditions_parsed.get("postconditions") or []
    else:
        postconditions_list = []
    postconditions_json = json.dumps(postconditions_list, indent=2)

    repo_cfg = (config or {}).get("repo", {})
    cursor_rules_path = repo_cfg.get("cursor_rules", "")
    if cursor_rules_path:
        cursor_rules = _read_file(Path(cursor_rules_path))
    else:
        cursor_rules = _read_file(gate_dir() / "config" / "cursor-rules.md")

    prior_review_json = _read_file(workspace / "prior-review.json")
    diff_stats = _read_file(workspace / "diff_stats.txt")

    if stage in ("triage", "postconditions"):
        diff_or_summary = build_diff_or_summary(workspace, config=config)
    else:
        diff_or_summary = diff

    verdict = _read_json_file(workspace / "verdict.json")
    fixable_findings = []
    if verdict and isinstance(verdict.get("findings"), list):
        fixable_findings = [
            f
            for f in verdict["findings"]
            if f.get("introduced_by_pr") is not False
            and f.get("severity") in ("critical", "error", "warning")
        ]

    # Tag each finding with a stable `finding_id` and a `fixability` class
    # (audit A2 + 1E.ii) before handing the list to the fix-senior prompt.
    # Import lazily to avoid a fixer → prompt → fixer cycle at module load.
    try:
        from gate.fixer import (
            fixability_summary as _fixability_summary,
        )
        from gate.fixer import (
            tag_findings as _tag_findings,
        )
        fixable_findings = _tag_findings(fixable_findings)
        fix_mode = "polish" if (verdict or {}).get("decision") == "approve_with_notes" else "strict"
        summary_line = _fixability_summary(fixable_findings)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"fix-senior tagging failed: {e}")
        fix_mode = "strict"
        summary_line = "0 trivial, 0 scoped, 0 broad, 0 unknown"

    blocklist_path = repo_cfg.get("fix_blocklist", "")
    if blocklist_path:
        blocklist = _read_file(Path(blocklist_path))
    else:
        blocklist = _read_file(gate_dir() / "config" / "fix-blocklist.txt")

    prep_context = _read_file(workspace / "fix-prep.json")
    fix_plan = _read_file(workspace / "fix-plan.json")

    # Resolve project profile for template variables
    profile = profiles.resolve_profile(repo_cfg, workspace)

    result = {
        "pr_title": env_vars.get("pr_title", ""),
        "pr_body": truncate(env_vars.get("pr_body", ""), max_pr_body, "PR body"),
        "pr_author": env_vars.get("pr_author", ""),
        "pr_number": env_vars.get("pr_number", ""),
        "diff": truncate(diff, max_diff, "Diff"),
        "diff_or_summary": diff_or_summary,
        "diff_stats": diff_stats,
        "file_list": truncate(changed_files, max_file_list, "File list"),
        "changed_files": truncate(changed_files, max_file_list, "File list"),
        "file_count": file_count,
        "lines_changed": lines_changed,
        "build_results": build_json,
        "build_json": build_json,
        "compiled_cursor_rules": cursor_rules,
        "triage_json": triage_json,
        "triage_summary": _stage_summary(triage, "Triage: not yet run"),
        "change_intent_json": change_intent_json,
        "postconditions_json": postconditions_json,
        "postconditions_max_functions": str(
            limits.get("postconditions_max_functions", 10)
        ),
        "risk_level": str((triage or {}).get("risk_level") or "medium"),
        "architecture_json": architecture_json,
        "architecture_summary": _stage_summary(
            architecture, "Architecture review: skipped by triage"
        ),
        "security_json": security_json,
        "security_summary": _stage_summary(
            security, "Security review: skipped by triage"
        ),
        "logic_json": logic_json,
        "prior_review_json": prior_review_json or '{ "has_prior": false }',
        "fix_diff": _read_file(workspace / "fix-diff.txt"),
        "tsc_errors": (
            _read_file(workspace / "fix-build-tsc-errors.txt") or "(no build errors)"
        ),
        "lint_errors": (
            _read_file(workspace / "fix-build-lint-errors.txt") or "(no lint errors)"
        ),
        "findings_json": json.dumps(fixable_findings, indent=2),
        "fix_mode": fix_mode,
        "fixability_summary": summary_line,
        "polish_mode_section": _build_polish_mode_section(fix_mode, summary_line),
        "hopper_mode_section": _build_hopper_mode_section(
            # Resolve here (not at fixer level) so every template that
            # references $hopper_mode_section gets the same value.
            get_fix_pipeline_mode(config or {})
        ),
        "blocklist": blocklist or "(no blocklist configured)",
        "prep_context": prep_context or "(prep phase skipped)",
        "fix_plan": fix_plan or "(plan phase skipped — fix all findings using your judgment)",
        "previous_attempt_context": (
            _read_file(workspace / "fix-previous-attempt.txt")
            or "(first attempt)"
        ),
        "bot_account": (config or {}).get("repo", {}).get("bot_account", "gate-bot"),
        # Phase 4: ambiguity halt knobs surfaced to fix-senior.md so the
        # senior can branch on the repo policy. ``halt_on_ambiguity``
        # defaults to true (safer for autonomy). ``force_safest_interpretation``
        # is flipped by the orchestrator after too many fix-reruns with
        # no author response — tells the senior to pick the safest
        # interpretation rather than halt indefinitely.
        "halt_on_ambiguity": json.dumps(
            bool(repo_cfg.get("halt_on_ambiguity", True))
        ),
        "force_safest_interpretation": json.dumps(
            bool(env_vars.get("force_safest_interpretation", False))
        ),
        # Project profile variables
        "project_language": profile.get("language", "Unknown"),
        "project_type": profile.get("project_type", ""),
        "typecheck_cmd": profile.get("typecheck_cmd", ""),
        "lint_cmd": profile.get("lint_cmd", ""),
        "test_cmd": profile.get("test_cmd", ""),
        "test_file_pattern": profile.get("test_file_pattern", ""),
        "dep_file": profile.get("dep_file", ""),
        "config_files": profile.get("config_files", ""),
        "env_access_pattern": profile.get("env_access_pattern", ""),
        "import_style": profile.get("import_style", ""),
        # Phase 6: pass-through verifier command for the `proof_confirmed`
        # evidence tier. Empty string means "this repo has no verifier";
        # prompts must gate the proof-verification section on a non-empty
        # value rather than inventing a command.
        "verify_cmd": profile.get("verify_cmd", ""),
    }
    return result
