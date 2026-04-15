"""GitHub API operations.

Ported from post-review.js + PR info fetching + GitHub Checks API.
All gh commands use GH_TOKEN env var set to os.environ["GATE_PAT"].
"""

import json
import logging
import os
import random
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ESCALATION_REVIEWERS = ""

EVIDENCE_LABELS = {
    "test_confirmed": "test confirmed",
    "code_trace": "code trace",
    "pattern_match": "pattern match",
    "speculative": "speculative",
}

GH_TIMEOUT_S = 30
GH_MAX_RETRIES = 5
GH_BASE_DELAY_S = 3

_TRANSIENT_PATTERNS = (
    "timeout",
    "timed out",
    "i/o timeout",
    "dial tcp",
    "TLS handshake",
    "ECONNRESET",
    "ECONNREFUSED",
    "ETIMEDOUT",
    "ENETUNREACH",
    "connection refused",
    "connection reset",
    "no such host",
    "502",
    "503",
    "429",
)


def _gh_env() -> dict[str, str]:
    """Build env dict for gh commands with GATE_PAT as GH_TOKEN."""
    env = os.environ.copy()
    pat = os.environ.get("GATE_PAT", "")
    if pat:
        env["GH_TOKEN"] = pat
    return env


def _wait_for_connectivity(max_wait: float = 30.0) -> bool:
    """Block until GitHub API is reachable via TCP, or timeout.

    Resolves github.com via DNS to get the current IP, falling back to a
    known IP if DNS fails. This avoids hardcoding an IP that GitHub may change.
    """
    import socket as sock_mod

    try:
        addrs = sock_mod.getaddrinfo("github.com", 443, type=sock_mod.SOCK_STREAM)
        target = addrs[0][4][0]
    except (sock_mod.gaierror, IndexError):
        target = "140.82.114.6"

    deadline = time.monotonic() + max_wait
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            s = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((target, 443))
            s.close()
            if attempt > 1:
                logger.info(f"GitHub reachable after {attempt} probe(s)")
            return True
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            time.sleep(min(2.0, deadline - time.monotonic()))
    logger.warning(f"GitHub unreachable after {max_wait}s of probing")
    return False


def _gh(args: list[str], timeout: float | None = None) -> str:
    """Run gh CLI with robust retry on transient network errors.

    Retries on any network-related failure (DNS, TCP, TLS, HTTP 5xx, 429).
    Uses exponential backoff with jitter. On the final retry, waits for TCP
    connectivity to GitHub before attempting — this handles intermittent
    network drops caused by Tailscale/macOS routing issues.
    """
    env = _gh_env()
    effective_timeout = timeout or GH_TIMEOUT_S
    last_error: Exception | None = None

    for attempt in range(1, GH_MAX_RETRIES + 2):
        if attempt == GH_MAX_RETRIES + 1:
            # Bonus attempt after connectivity probe — only if we got here
            # via the connectivity wait path below
            if last_error is None:
                break
            logger.info("Bonus attempt after connectivity restored")

        try:
            result = subprocess.run(
                ["gh"] + args,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env=env,
            )
            if result.returncode == 0:
                if attempt > 1:
                    logger.info(f"gh succeeded on attempt {attempt}")
                return result.stdout

            stderr_raw = result.stderr[:500].strip()
            stderr = stderr_raw.lower()
            is_transient = any(p in stderr for p in _TRANSIENT_PATTERNS)

            if not is_transient:
                logger.warning(
                    f"gh non-transient failure (rc={result.returncode}): {stderr_raw[:200]}"
                )

            if is_transient and attempt < GH_MAX_RETRIES:
                delay = GH_BASE_DELAY_S * (2 ** (attempt - 1)) * (0.5 + random.random())
                logger.warning(
                    f"gh retry {attempt}/{GH_MAX_RETRIES} "
                    f"(delay {delay:.0f}s): {result.stderr[:120].strip()}"
                )
                time.sleep(delay)
                continue

            if is_transient and attempt == GH_MAX_RETRIES:
                logger.warning("Retries exhausted — waiting for GitHub connectivity")
                last_error = subprocess.CalledProcessError(
                    result.returncode, "gh", stderr=result.stderr
                )
                if _wait_for_connectivity(max_wait=60.0):
                    continue
                raise last_error

            raise subprocess.CalledProcessError(
                result.returncode, "gh", stderr=result.stderr
            )

        except subprocess.TimeoutExpired:
            if attempt < GH_MAX_RETRIES:
                delay = GH_BASE_DELAY_S * (2 ** (attempt - 1)) * (0.5 + random.random())
                logger.warning(f"gh timeout retry {attempt}/{GH_MAX_RETRIES}")
                time.sleep(delay)
                continue
            if attempt == GH_MAX_RETRIES:
                logger.warning("Retries exhausted after timeout — waiting for connectivity")
                last_error = subprocess.TimeoutExpired(["gh"] + args, effective_timeout)
                if _wait_for_connectivity(max_wait=60.0):
                    continue
            raise

    if last_error:
        raise last_error
    return ""


# ── PR Info ──────────────────────────────────────────────────


def get_pr_info(repo: str, pr_number: int) -> dict:
    """Fetch PR info via gh CLI."""
    fields = "number,title,body,author,labels,headRefName,headRefOid,state,isDraft"
    out = _gh(["pr", "view", str(pr_number), "--repo", repo, "--json", fields])
    data = json.loads(out)
    # Normalize: gh returns author as {login: ...}, flatten for easier access
    if isinstance(data.get("author"), dict):
        data["user"] = data["author"]
    else:
        data["user"] = {"login": "unknown"}
    return data


def get_pr_labels(repo: str, pr_number: int) -> list[str]:
    """Fetch current labels on a PR."""
    out = _gh(["pr", "view", str(pr_number), "--repo", repo, "--json", "labels"])
    data = json.loads(out)
    return [label["name"] for label in data.get("labels", [])]


def remove_label(repo: str, pr_number: int, label: str) -> None:
    """Remove a label from a PR (e.g., gate-rerun)."""
    try:
        _gh(["pr", "edit", str(pr_number), "--repo", repo, "--remove-label", label])
    except subprocess.CalledProcessError:
        logger.warning(f"Failed to remove label {label} from PR #{pr_number}")


# ── Review Posting ───────────────────────────────────────────


def _format_findings(findings: list[dict]) -> str:
    """Format findings into markdown sections.

    Ported from formatFindings() in post-review.js.
    """
    if not findings:
        return ""

    errors = [f for f in findings if f.get("severity") in ("critical", "error")]
    warnings = [f for f in findings if f.get("severity") == "warning"]
    infos = [f for f in findings if f.get("severity") == "info"]

    md = ""

    if errors:
        md += "\n### Errors\n\n"
        for f in errors:
            ev_key = f.get("evidence_level", "")
            ev = f" ({EVIDENCE_LABELS.get(ev_key, ev_key)})" if ev_key else ""
            loc = f"{f.get('file', '?')}"
            if f.get("line"):
                loc += f":{f['line']}"
            md += f"- **{loc}**{ev} — {f.get('message', '')}"
            if f.get("suggestion"):
                md += f"\n  > Fix: {f['suggestion']}"
            md += "\n"

    if warnings:
        md += "\n### Warnings\n\n"
        for f in warnings:
            ev_key = f.get("evidence_level", "")
            ev = f" ({EVIDENCE_LABELS.get(ev_key, ev_key)})" if ev_key else ""
            loc = f"{f.get('file', '?')}"
            if f.get("line"):
                loc += f":{f['line']}"
            md += f"- **{loc}**{ev} — {f.get('message', '')}"
            if f.get("suggestion"):
                md += f"\n  > Fix: {f['suggestion']}"
            md += "\n"

    if infos:
        md += "\n### Notes\n\n"
        for f in infos:
            loc = f"{f.get('file', '?')}"
            if f.get("line"):
                loc += f":{f['line']}"
            md += f"- **{loc}** — {f.get('message', '')}\n"

    return md


def _format_resolved(resolved: list[dict]) -> str:
    """Format resolved findings. Ported from formatResolved()."""
    if not resolved:
        return ""
    md = "\n### Resolved since last review\n\n"
    for r in resolved:
        resolution = "fixed" if r.get("resolution") == "fixed_by_author" else "no longer applicable"
        md += f"- ~~**{r.get('file', '?')}** — {r.get('message', '')}~~ ({resolution})\n"
    return md


def _format_build_section(build: dict | None) -> str:
    """Format build results. Uses tool names from build result when available."""
    if not build:
        return ""

    if build.get("skipped"):
        reason = build.get("skip_reason", "no build commands configured")
        return f"\n### Build Results\n- Build verification skipped ({reason})\n"

    md = "\n### Build Results\n"

    tc = build.get("typecheck", build.get("typescript", {}))
    if tc:
        tool_name = tc.get("tool") or "Type check"
        if tc.get("pass"):
            md += f"- {tool_name}: ✅ ({tc.get('error_count', 0)} errors)\n"
        else:
            md += f"- {tool_name}: ❌ ({tc.get('error_count', '?')} errors)\n"

    lint = build.get("lint", {})
    if lint:
        lint_name = lint.get("tool") or "Lint"
        if lint.get("pass"):
            md += f"- {lint_name}: ✅ ({lint.get('warning_count', 0)} warnings)\n"
        else:
            md += (
                f"- {lint_name}: ❌ ({lint.get('error_count', '?')} errors, "
                f"{lint.get('warning_count', 0)} warnings)\n"
            )

    tests = build.get("tests", {})
    if tests:
        test_name = tests.get("tool") or "Tests"
        if tests.get("pass"):
            md += f"- {test_name}: ✅ ({tests.get('passed', 0)}/{tests.get('total', 0)} passed)\n"
        else:
            md += (
                f"- {test_name}: ❌ ({tests.get('failed', 0)} failed, "
                f"{tests.get('passed', 0)}/{tests.get('total', 0)} passed)\n"
            )

    return md


def _build_comment(verdict: dict, build: dict | None) -> str:
    """Build the review comment markdown. Ported from buildComment()."""
    decision = verdict.get("decision", "approve")
    confidence = verdict.get("confidence", "unknown")
    summary = verdict.get("summary", "")
    findings = verdict.get("findings", [])
    resolved = verdict.get("resolved_findings", [])
    stats = verdict.get("stats", {})
    review_time = verdict.get("review_time_seconds", "?")

    errors = len([f for f in findings if f.get("severity") in ("critical", "error")])
    warnings = len([f for f in findings if f.get("severity") == "warning"])
    infos = len([f for f in findings if f.get("severity") == "info"])
    stages_run = stats.get("stages_run", "?")

    is_approved = decision in ("approve", "approve_with_notes")

    if is_approved:
        notes = " with notes" if decision == "approve_with_notes" else ""
        md = f"## Gate Review ✅\n\n**Approved{notes}** — {summary or 'No issues found.'}"
    else:
        default_summary = f"{errors} errors, {warnings} warnings found."
        md = f"## Gate Review ❌\n\n**Changes requested** — {summary or default_summary}"

    md += _format_findings(findings)
    md += _format_resolved(resolved)
    md += _format_build_section(build)

    count_parts = []
    if errors > 0:
        count_parts.append(f"{errors} error{'s' if errors > 1 else ''}")
    if warnings > 0:
        count_parts.append(f"{warnings} warning{'s' if warnings > 1 else ''}")
    if infos > 0:
        count_parts.append(f"{infos} note{'s' if infos > 1 else ''}")
    count_str = ", ".join(count_parts) if count_parts else "no issues"

    md += (
        f"\n---\n*{count_str} across {stages_run} stages"
        f" ({review_time}s, confidence: {confidence})*"
    )

    return md


def post_review(
    repo: str,
    pr_number: int,
    verdict: dict,
    build: dict | None,
    sha: str,
    config: dict | None = None,
) -> None:
    """Post a review to the PR. Always enforcement mode.

    Ported from post-review.js main(). No advisory toggle.
    """
    comment = _build_comment(verdict, build)
    decision = verdict.get("decision", "approve")
    findings = verdict.get("findings", [])

    if decision in ("approve", "approve_with_notes"):
        try:
            _gh(["pr", "review", str(pr_number), "--repo", repo, "--approve", "--body", comment])
            logger.info(f"PR #{pr_number} approved ({decision})")
        except subprocess.CalledProcessError as e:
            if "approve your own" in (e.stderr or ""):
                logger.warning(f"PR #{pr_number}: cannot approve own PR, posting as comment")
                comment_pr(repo, pr_number, comment)
            else:
                raise
    else:
        _gh([
            "pr", "review", str(pr_number), "--repo", repo,
            "--request-changes", "--body", comment,
        ])
        logger.info(f"PR #{pr_number} changes requested")

        has_critical = any(
            f.get("severity") == "critical" and f.get("introduced_by_pr") is not False
            for f in findings
        )
        should_escalate = verdict.get("confidence") == "low" or has_critical

        if should_escalate:
            reviewers = (config or {}).get("repo", {}).get("escalation_reviewers", "")
            if not reviewers:
                logger.info(f"PR #{pr_number}: escalation skipped (no reviewers configured)")
            else:
                try:
                    _gh(["pr", "edit", str(pr_number), "--repo", repo,
                         "--add-label", "needs-human-review"])
                    _gh(["pr", "edit", str(pr_number), "--repo", repo,
                         "--add-reviewer", reviewers])
                    reason = "critical_finding" if has_critical else "low_confidence"
                    logger.info(f"PR #{pr_number} escalated: {reason}")
                except subprocess.CalledProcessError:
                    logger.warning(f"PR #{pr_number} escalation failed")


# ── Commit Status API ────────────────────────────────────────
# Uses the Commit Statuses API instead of the Checks API because
# fine-grained PATs support statuses but NOT check runs (GitHub App only).
# The status context name ("gate-review") can be used in branch protection
# rules just like a check run name.

_CONCLUSION_TO_STATE = {
    "success": "success",
    "neutral": "success",
    "cancelled": "failure",
    "action_required": "failure",
    "failure": "failure",
}


def create_check_run(
    repo: str, sha: str, name: str = "gate-review", status: str = "queued"
) -> str | None:
    """Create a commit status (pending). Returns the context name as ID."""
    state = "pending"
    try:
        _gh([
            "api", f"repos/{repo}/statuses/{sha}",
            "-X", "POST",
            "-f", f"state={state}",
            "-f", f"context={name}",
            "-f", "description=Gate review queued",
        ])
        logger.info(f"Created commit status '{name}' on {sha[:8]}")
        return name
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to create commit status: {e}")
        return None


def update_check_run(
    repo: str,
    check_run_id: str | int | None,
    status: str = "in_progress",
    output_title: str = "",
    output_summary: str = "",
) -> None:
    """No-op: intermediate status updates are intentionally skipped.

    The Statuses API requires the commit SHA for every update, but this function
    only receives the context name (check_run_id). Rather than adding noise with
    intermediate pending -> pending transitions, we let the status go directly from
    the initial ``pending`` (set by ``create_check_run``) to the final
    ``success``/``failure`` (set by ``complete_check_run``).

    This keeps the GitHub commit status timeline clean. Stage progress is still
    visible via the TUI and server events.
    """
    if not check_run_id:
        return
    logger.debug(f"Status update (no-op, {check_run_id}): {output_title}")


def complete_check_run(
    repo: str,
    check_run_id: str | int | None,
    conclusion: str,
    output_title: str = "",
    output_summary: str = "",
    sha: str = "",
) -> None:
    """Set final commit status (success/failure)."""
    if not check_run_id:
        return
    state = _CONCLUSION_TO_STATE.get(conclusion, "failure")
    description = output_title[:140] if output_title else conclusion
    context = check_run_id if isinstance(check_run_id, str) else "gate-review"
    if not sha:
        logger.warning("complete_check_run called without sha, skipping")
        return
    try:
        _gh([
            "api", f"repos/{repo}/statuses/{sha}",
            "-X", "POST",
            "-f", f"state={state}",
            "-f", f"context={context}",
            "-f", f"description={description}",
        ])
        logger.info(f"Commit status '{context}' -> {state}: {description}")
    except subprocess.CalledProcessError:
        logger.warning(f"Failed to set commit status {context}")


# ── Simple PR Operations ────────────────────────────────────


def approve_pr(repo: str, pr_number: int, body: str) -> None:
    """Approve a PR, falling back to a comment if we own the PR."""
    try:
        _gh(["pr", "review", str(pr_number), "--repo", repo, "--approve", "--body", body])
    except subprocess.CalledProcessError as e:
        if "approve your own" in (e.stderr or ""):
            logger.warning(f"PR #{pr_number}: cannot approve own PR, posting as comment")
            comment_pr(repo, pr_number, body)
        else:
            logger.warning(f"Failed to approve PR #{pr_number}")


def comment_pr(repo: str, pr_number: int, body: str) -> None:
    """Post a comment on a PR."""
    try:
        _gh(["pr", "comment", str(pr_number), "--repo", repo, "--body", body])
    except subprocess.CalledProcessError:
        logger.warning(f"Failed to comment on PR #{pr_number}")


def commit_and_push(worktree: Path, message: str, branch: str = "") -> str | None:
    """Stage all changes, commit, and push from a worktree.

    Uses GATE_PAT for push authentication via git HTTPS extraheader.

    Returns the new HEAD SHA on success, None on failure or no changes.
    """
    from gate.workspace import _git_env

    env = _git_env()
    cwd = str(worktree)
    try:
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=cwd, capture_output=True,
        )
        if result.returncode == 0:
            logger.info("No changes to commit")
            return None
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=cwd, check=True, capture_output=True, env=env,
        )
        push_cmd = ["git", "push", "origin", f"HEAD:{branch}"] if branch else ["git", "push"]
        subprocess.run(
            push_cmd,
            cwd=cwd, check=True, capture_output=True, env=env,
        )
        sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True,
        )
        new_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else ""
        logger.info(f"Committed and pushed: {message} ({new_sha[:8]})")
        return new_sha or "unknown"
    except subprocess.CalledProcessError as e:
        logger.error(f"Commit/push failed: {e}")
        return None
