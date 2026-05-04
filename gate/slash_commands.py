"""Slash command parser, dispatcher, and GitHub-issue-comment poller.

The team interacts with Gate through PR comments — ``/gate rerun``,
``/gate skip <reason>``, ``/gate status``, ``/gate explain <id>``,
``/gate bypass <link>``. This module:

* Parses comment bodies and extracts well-formed commands
  (:func:`parse_command`).
* Dispatches commands to the right side effect — server socket
  enqueue, label flip, state-tree lookup — and returns reply text
  (:func:`dispatch_command`).
* Polls each configured repo's issue comments at a fixed interval and
  routes new ones through the parser/dispatcher
  (:class:`CommentPoller`).

Why polling, not webhooks? Most Gate installs sit behind NAT or run on
a laptop with no inbound port. Polling works on every install today
without operator setup. A webhook handler can be layered on later (in
the companion repo) once a tunnel exists; both can run side by side
because :func:`dispatch_command` is the same regardless of how the
command arrived.

Auth model:

* ``/gate status`` and ``/gate explain`` are read-only and open to any
  commenter.
* The mutating verbs (``rerun``, ``skip``, ``bypass``) require the
  commenter's GitHub login to be in the repo config's
  ``allowed_commanders`` list. Default is empty → mutations are
  rejected with a reply explaining how to authorise them.

Audit log:

  Every dispatch — success or rejection — writes one record to
  ``logs/actions.jsonl`` via :mod:`gate.actions`. ``/gate bypass`` in
  particular *requires* an incident link argument that must match the
  configured pattern, so the audit trail is non-bypassable.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from gate import client as gate_client
from gate.actions import record_action
from gate.config import data_dir, get_all_repos, repo_slug
from gate.state import get_pr_state_dir

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 30.0

# Bypass links must point to a real incident-management system. The
# default pattern covers Linear, Jira (Atlassian Cloud), GitHub
# Issues, and PagerDuty; operators can override per-repo via
# ``repo.bypass_link_patterns`` in gate.toml.
_DEFAULT_BYPASS_LINK_PATTERNS: tuple[str, ...] = (
    r"^https://linear\.app/.+/issue/.+",
    r"^https://[\w.-]+\.atlassian\.net/browse/[A-Z]+-\d+",
    r"^https://github\.com/[\w.-]+/[\w.-]+/issues/\d+",
    r"^https://[\w.-]+\.pagerduty\.com/incidents/.+",
)

_OPEN_VERBS: frozenset[str] = frozenset({"status", "explain"})
_MUTATING_VERBS: frozenset[str] = frozenset({"rerun", "skip", "bypass"})
_KNOWN_VERBS: frozenset[str] = _OPEN_VERBS | _MUTATING_VERBS


@dataclass
class Command:
    """A parsed ``/gate <verb> [args...]`` invocation."""

    verb: str
    args: list[str]
    pr_number: int = 0
    repo: str = ""
    commenter: str = ""
    comment_id: int = 0


def parse_command(body: str) -> Command | None:
    """Extract a ``/gate <verb> [args]`` from a PR comment body.

    The command must appear on a *line by itself* — bare mentions in
    prose ("see /gate status") don't trigger. This avoids accidental
    re-triggering when an old comment is quoted.

    Returns the first matching command in the body, or ``None`` when
    none found.
    """
    if not isinstance(body, str) or "/gate" not in body:
        return None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("/gate"):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if not tokens or tokens[0] != "/gate":
            continue
        if len(tokens) < 2:
            return None
        verb = tokens[1].lower()
        if verb not in _KNOWN_VERBS:
            return None
        return Command(verb=verb, args=tokens[2:])
    return None


def _is_authorised(
    cmd: Command,
    repo_config: dict | None,
) -> tuple[bool, str]:
    """Return (authorised, reject_reason) for a parsed command."""
    if cmd.verb in _OPEN_VERBS:
        return True, ""
    allowed = []
    if isinstance(repo_config, dict):
        raw = repo_config.get("allowed_commanders") or []
        if isinstance(raw, list):
            allowed = [str(x).strip().lower() for x in raw if isinstance(x, str)]
    if not allowed:
        return False, (
            "no_allowed_commanders_configured: add the operator's "
            "GitHub login to `repo.allowed_commanders` in gate.toml"
        )
    if cmd.commenter.strip().lower() not in allowed:
        return False, f"commenter_not_authorised: {cmd.commenter}"
    return True, ""


def _bypass_link_patterns(repo_config: dict | None) -> tuple[str, ...]:
    if isinstance(repo_config, dict):
        override = repo_config.get("bypass_link_patterns")
        if isinstance(override, list) and override:
            return tuple(str(p) for p in override if isinstance(p, str))
    return _DEFAULT_BYPASS_LINK_PATTERNS


def _validate_bypass_link(link: str, repo_config: dict | None) -> bool:
    for pattern in _bypass_link_patterns(repo_config):
        try:
            if re.match(pattern, link):
                return True
        except re.error:
            continue
    return False


def _latest_verdict_for_pr(repo: str, pr_number: int) -> dict | None:
    """Find the most-recent verdict.json for a (repo, pr) pair."""
    pr_dir = get_pr_state_dir(pr_number, repo, create=False)
    if not pr_dir.exists():
        return None
    reviews_dir = pr_dir / "reviews"
    if reviews_dir.exists():
        archives = sorted(
            (d for d in reviews_dir.iterdir() if d.is_dir()),
            reverse=True,
        )
        for archive in archives:
            verdict_path = archive / "verdict.json"
            if verdict_path.exists():
                try:
                    return json.loads(verdict_path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
    fallback = pr_dir / "verdict.json"
    if fallback.exists():
        try:
            return json.loads(fallback.read_text())
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _format_status(reviews: list[dict], queue: list[dict], pr_number: int) -> str:
    active = next((r for r in reviews if r.get("pr_number") == pr_number), None)
    if active:
        stage = active.get("stage", "?")
        status = active.get("status", "?")
        return f"PR #{pr_number}: **{status}** at stage `{stage}`."
    queued = next(
        (i for i, q in enumerate(queue) if q.get("pr_number") == pr_number), None
    )
    if queued is not None:
        return f"PR #{pr_number}: queued at position #{queued + 1} of {len(queue)}."
    return f"PR #{pr_number}: not currently active or queued. Push to re-trigger."


def _format_explain(verdict: dict | None, finding_id: str) -> str:
    if not verdict:
        return (
            f"No verdict found for finding `{finding_id}`. The PR may not "
            "have been reviewed yet, or the state archive may have been "
            "pruned."
        )
    findings = verdict.get("findings") or []
    for f in findings:
        if str(f.get("finding_id", "")) == finding_id:
            sev = f.get("severity", "?")
            file_loc = f.get("file", "?")
            line = f.get("line")
            loc = f"{file_loc}:{line}" if line is not None else file_loc
            rule = f.get("rule_source", "")
            sugg = f.get("suggestion", "")
            parts = [
                f"**Finding `{finding_id}`** (`{sev}` at `{loc}`)",
                "",
                f.get("message", "(no message)"),
            ]
            if rule:
                parts += ["", f"_Rule source:_ `{rule}`"]
            if sugg:
                parts += ["", "_Suggested fix:_", "", sugg]
            return "\n".join(parts)
    return f"Finding `{finding_id}` not found in the latest verdict."


def dispatch_command(
    cmd: Command,
    *,
    socket_path: Path,
    repo_config: dict | None = None,
) -> tuple[str, str]:
    """Execute a parsed command. Returns ``(outcome, reply_markdown)``.

    ``outcome`` is one of ``"ok"`` / ``"rejected"`` / ``"error"`` and
    is written to the actions log alongside the reply.
    """
    if cmd.verb not in _KNOWN_VERBS:
        return "rejected", f"Unknown command `{cmd.verb}`."

    authorised, reject_reason = _is_authorised(cmd, repo_config)
    if not authorised:
        record_action(
            verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
            pr_number=cmd.pr_number, args={"raw_args": cmd.args},
            outcome="rejected", detail=reject_reason,
        )
        if reject_reason.startswith("no_allowed_commanders_configured"):
            return "rejected", (
                ":no_entry_sign: Mutating slash commands are disabled until "
                "the operator adds authorised GitHub logins to "
                "`repo.allowed_commanders` in gate.toml."
            )
        return "rejected", (
            f":no_entry_sign: `@{cmd.commenter}` is not authorised to run "
            f"`/gate {cmd.verb}`. Ask the operator to add you to "
            f"`repo.allowed_commanders` in gate.toml."
        )

    if cmd.verb == "status":
        reviews = gate_client.list_reviews(socket_path)
        queue = gate_client.list_queue(socket_path)
        reply = _format_status(reviews, queue, cmd.pr_number)
        record_action(
            verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
            pr_number=cmd.pr_number, args={}, outcome="ok",
        )
        return "ok", reply

    if cmd.verb == "explain":
        if not cmd.args:
            return "rejected", "`/gate explain` requires a finding id."
        finding_id = cmd.args[0]
        verdict = _latest_verdict_for_pr(cmd.repo, cmd.pr_number)
        reply = _format_explain(verdict, finding_id)
        record_action(
            verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
            pr_number=cmd.pr_number, args={"finding_id": finding_id},
            outcome="ok",
        )
        return "ok", reply

    if cmd.verb == "rerun":
        # Re-enqueue is a server socket message; reply tells the user
        # the request is in.
        from gate.github import get_pr_info

        try:
            pr_info = get_pr_info(cmd.repo, cmd.pr_number)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            record_action(
                verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
                pr_number=cmd.pr_number, args={}, outcome="error",
                detail=f"get_pr_info failed: {e}",
            )
            return "error", (
                f":x: Could not fetch PR #{cmd.pr_number} head info; "
                "rerun aborted."
            )

        head_sha = str(pr_info.get("headRefOid") or "")
        branch = str(pr_info.get("headRefName") or "")
        if not head_sha or not branch:
            record_action(
                verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
                pr_number=cmd.pr_number, args={}, outcome="rejected",
                detail="missing_head_sha_or_branch",
            )
            return "rejected", (
                f":no_entry_sign: PR #{cmd.pr_number} has no head commit "
                "(closed or detached?). Cannot rerun."
            )
        gate_client.send_message(
            socket_path,
            {
                "type": "review_request",
                "pr_number": cmd.pr_number,
                "repo": cmd.repo,
                "head_sha": head_sha,
                "event": "manual_rerun",
                "branch": branch,
                "labels": [],
            },
            wait_for_response=False,
        )
        record_action(
            verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
            pr_number=cmd.pr_number, args={}, outcome="ok",
        )
        return "ok", (
            ":arrows_counterclockwise: Re-running review for PR "
            f"#{cmd.pr_number}."
        )

    if cmd.verb == "skip":
        reason = " ".join(cmd.args).strip()
        if not reason:
            return "rejected", (
                "`/gate skip` requires a reason: `/gate skip <reason>`."
            )
        try:
            _add_label(cmd.repo, cmd.pr_number, "gate-skip")
        except subprocess.CalledProcessError as e:
            record_action(
                verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
                pr_number=cmd.pr_number, args={"reason": reason},
                outcome="error", detail=str(e),
            )
            return "error", f":x: Failed to add `gate-skip` label: {e}"
        record_action(
            verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
            pr_number=cmd.pr_number, args={"reason": reason}, outcome="ok",
        )
        return "ok", (
            f":fast_forward: Added `gate-skip` label. Reason: _{reason}_"
        )

    if cmd.verb == "bypass":
        if not cmd.args:
            return "rejected", (
                "`/gate bypass` requires a link to an incident: "
                "`/gate bypass https://linear.app/...`"
            )
        link = cmd.args[0]
        if not _validate_bypass_link(link, repo_config):
            return "rejected", (
                f":no_entry_sign: Bypass link `{link}` does not match an "
                "approved incident-tracker pattern. Approved sources: "
                "Linear, Jira, GitHub Issues, PagerDuty (or whatever your "
                "operator configured in `repo.bypass_link_patterns`)."
            )
        try:
            _add_label(cmd.repo, cmd.pr_number, "gate-emergency-bypass")
        except subprocess.CalledProcessError as e:
            record_action(
                verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
                pr_number=cmd.pr_number, args={"link": link},
                outcome="error", detail=str(e),
            )
            return "error", f":x: Failed to add `gate-emergency-bypass` label: {e}"
        record_action(
            verb=cmd.verb, actor=cmd.commenter, repo=cmd.repo,
            pr_number=cmd.pr_number, args={"link": link}, outcome="ok",
        )
        return "ok", (
            f":rotating_light: Bypass recorded for `@{cmd.commenter}`: {link}\n"
            "This is audited; the orchestrator will skip blocking findings "
            "for this PR."
        )

    return "rejected", f"Unsupported command `{cmd.verb}`."


def _add_label(repo: str, pr_number: int, label: str) -> None:
    """Add a label to a PR via gh. Imported lazily to keep tests fast."""
    from gate.github import _gh

    _gh([
        "pr", "edit", str(pr_number),
        "--repo", repo,
        "--add-label", label,
    ])


def _post_reply(repo: str, pr_number: int, body: str) -> None:
    from gate.github import _gh

    _gh([
        "pr", "comment", str(pr_number),
        "--repo", repo,
        "--body", body,
    ])


# ── Poller ────────────────────────────────────────────────────


def _last_seen_path(repo: str) -> Path:
    return data_dir() / "slash" / f"{repo_slug(repo)}.last_seen"


def _read_last_seen(repo: str) -> str:
    path = _last_seen_path(repo)
    if not path.exists():
        return ""
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _write_last_seen(repo: str, iso: str) -> None:
    path = _last_seen_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(iso)
    except OSError:
        logger.exception(f"Failed to persist last_seen for {repo}")


class CommentPoller:
    """Polls each configured repo's PR comments for ``/gate <verb>`` invocations.

    One thread per ``CommentPoller`` instance (not per repo); repos are
    polled sequentially within the loop. For up to ~10 repos this is
    fine because the GitHub API call returns in <500ms typically and
    we poll on the order of every 30 seconds.

    Persisted ``last_seen`` per repo means restarts don't replay
    commands the operator already saw — important for ``/gate rerun``,
    which would otherwise re-enqueue the same PR every time the server
    restarted.
    """

    def __init__(
        self,
        *,
        socket_path: Path,
        repos: list[dict] | None = None,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
        config: dict | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._repos = repos if repos is not None else get_all_repos(config)
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._processed_comment_ids: set[int] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="slash-poller", daemon=True
        )
        self._thread.start()
        logger.info(
            f"Slash command poller started for {len(self._repos)} repo(s); "
            f"interval={self._interval_s}s"
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            for repo_cfg in self._repos:
                if self._stop.is_set():
                    break
                try:
                    self._poll_repo(repo_cfg)
                except Exception:
                    logger.exception(
                        f"Slash poll failed for {repo_cfg.get('name', '?')}"
                    )
            self._stop.wait(timeout=self._interval_s)

    def _poll_repo(self, repo_cfg: dict) -> None:
        from gate.github import _gh

        repo = repo_cfg.get("name", "")
        if not repo:
            return
        last_seen = _read_last_seen(repo)
        params = ["--method", "GET", "-F", "per_page=50", "-F", "sort=updated"]
        if last_seen:
            params += ["-F", f"since={last_seen}"]
        try:
            out = _gh([
                "api",
                f"repos/{repo}/issues/comments",
                *params,
            ])
        except subprocess.CalledProcessError as e:
            logger.warning(f"Slash poll: {repo} API call failed: {e}")
            return
        try:
            comments = json.loads(out or "[]")
        except json.JSONDecodeError:
            logger.warning(f"Slash poll: {repo} returned non-JSON")
            return
        if not isinstance(comments, list):
            return

        # Sort by updated_at ascending so we process oldest first; this
        # gives deterministic ordering when ``/gate skip`` and a
        # follow-up ``/gate rerun`` arrive in the same poll cycle.
        comments.sort(key=lambda c: c.get("updated_at", ""))

        max_seen = last_seen
        for c in comments:
            cid = c.get("id")
            if not isinstance(cid, int) or cid in self._processed_comment_ids:
                continue
            updated_at = c.get("updated_at", "")
            if updated_at and (not max_seen or updated_at > max_seen):
                max_seen = updated_at
            issue_url = c.get("issue_url", "")
            pr_number = _extract_pr_number(issue_url)
            if pr_number is None:
                continue
            self._processed_comment_ids.add(cid)
            self._dispatch_comment(repo_cfg, cid, c, pr_number)

        if max_seen and max_seen != last_seen:
            _write_last_seen(repo, max_seen)

    def _dispatch_comment(
        self,
        repo_cfg: dict,
        comment_id: int,
        comment: dict,
        pr_number: int,
    ) -> None:
        repo = repo_cfg.get("name", "")
        body = comment.get("body") or ""
        cmd = parse_command(body)
        if not cmd:
            return
        cmd.repo = repo
        cmd.pr_number = pr_number
        cmd.comment_id = comment_id
        commenter = ""
        user = comment.get("user")
        if isinstance(user, dict):
            commenter = str(user.get("login", "") or "")
        cmd.commenter = commenter

        outcome, reply = dispatch_command(
            cmd,
            socket_path=self._socket_path,
            repo_config=repo_cfg,
        )
        try:
            _post_reply(repo, pr_number, reply)
        except subprocess.CalledProcessError:
            logger.exception(f"Slash reply post failed for {repo}#{pr_number}")
        logger.info(
            f"slash {repo}#{pr_number} /{cmd.verb} from @{commenter}: {outcome}"
        )


_ISSUE_URL_PR_RE = re.compile(r"/repos/[^/]+/[^/]+/issues/(\d+)$")


def _extract_pr_number(issue_url: str) -> int | None:
    """Extract the PR number from an issue API URL.

    GitHub's issues comments API returns comments on both issues and
    PRs (PRs are issues in the GitHub data model). The ``issue_url``
    field looks like ``https://api.github.com/repos/o/r/issues/123``;
    we don't try to distinguish PR-vs-issue here because GitHub
    doesn't expose that on the comments endpoint cheaply. Downstream
    label / status APIs work for both, so a stray issue comment that
    happens to start with ``/gate`` would just no-op against missing
    PR state.
    """
    if not issue_url:
        return None
    m = _ISSUE_URL_PR_RE.search(issue_url)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None
