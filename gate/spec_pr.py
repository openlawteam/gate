"""Phase 5 — Spec-PR promotion.

When the logic stage writes tests with ``intent_type == "confirmed_correct"``
AND a passing mutation check (``mutation_check.result == "fail"`` — i.e.
the mutant *should* fail the test), those tests encode verified author
intent. Keeping them as a living spec suite prevents future regressions,
so Gate opens a follow-up PR that copies them into a dedicated folder
(default ``tests/gate-specs/``).

Design invariants (enforced by tests):

* **Opt-in** — disabled unless ``repo.persist_spec_tests = true``.
* **Fail-open** — any failure below commit must log and return. The
  original PR's verdict is never tainted by spec-PR machinery.
* **Idempotent** — re-running for the same ``(pr_number, base_sha)``
  short-circuits if the branch already exists on the remote.
* **Blocklist-aware** — target_dir is validated against
  ``fix_blocklist`` patterns so spec files can never land in a
  repo-protected path (e.g. ``infra/**``).
* **No raw ``git worktree add``** — routes through
  :func:`gate.workspace.create_auxiliary_worktree` so ``GATE_PAT``
  injection and bot-identity setup remain centralized (audit M3 fix).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from gate import github, workspace

logger = logging.getLogger(__name__)


def _branch_name(pr_number: int, base_sha: str) -> str:
    """Deterministic branch name — tests assert exact format."""
    short = (base_sha or "nosha")[:8]
    return f"gate/specs/pr{pr_number}-{short}"


def _target_dir_blocked(target_dir: str, config: dict) -> bool:
    """Return True iff ``target_dir`` matches any ``fix_blocklist`` pattern.

    Reuses the same semantics as :func:`gate.fixer.enforce_blocklist` —
    the blocklist file is a newline-delimited set of glob patterns.
    Empty/missing blocklists mean "nothing blocked".
    """
    from gate.config import gate_dir
    from gate.fixer import _match_glob

    repo_cfg = config.get("repo", {})
    blocklist_path_str = repo_cfg.get("fix_blocklist", "")
    blocklist_path = (
        Path(blocklist_path_str)
        if blocklist_path_str
        else gate_dir() / "config" / "fix-blocklist.txt"
    )
    try:
        content = blocklist_path.read_text()
    except OSError:
        return False
    patterns = [
        line.strip()
        for line in content.split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    # Match the target_dir itself AND any file underneath it.
    probe = target_dir.rstrip("/") + "/probe.test.ts"
    for pat in patterns:
        if _match_glob(target_dir, pat) or _match_glob(probe, pat):
            return True
    return False


def create_spec_pr(
    repo: str,
    pr_number: int,
    spec_files: list[Path],
    base_sha: str,
    clone_path: str,
    config: dict,
) -> int | None:
    """Open a follow-up PR containing verified spec tests.

    Parameters
    ----------
    repo
        ``owner/name`` string.
    pr_number
        The source PR number the specs came from.
    spec_files
        Absolute paths to the sidecar-captured test files.
    base_sha
        Commit to branch from — typically the target-branch tip at the
        time the original PR was reviewed.
    clone_path
        Absolute path to the main repo clone (worktree parent).
    config
        Resolved Gate config dict. Honors ``repo.spec_tests_dir``,
        ``repo.spec_pr_max_files``, ``repo.spec_pr_base_branch``,
        ``repo.default_branch``, ``repo.bot_account``.

    Returns
    -------
    int | None
        PR number on success; ``None`` on any failure (fail-open).
    """
    if not spec_files:
        return None

    repo_cfg = config.get("repo", {})
    target_dir = repo_cfg.get("spec_tests_dir", "tests/gate-specs")
    base_branch = (
        repo_cfg.get("spec_pr_base_branch")
        or repo_cfg.get("default_branch", "main")
    )
    max_files = int(repo_cfg.get("spec_pr_max_files", 5))
    spec_files = list(spec_files)[: max(0, max_files)]

    if _target_dir_blocked(target_dir, config):
        logger.warning(
            f"PR #{pr_number}: spec promotion skipped — "
            f"target_dir {target_dir!r} is blocklisted"
        )
        return None

    branch = _branch_name(pr_number, base_sha)

    # Idempotency: if the branch already exists on the remote, assume a
    # prior run succeeded. Never re-open a second PR for the same SHA.
    if github.branch_exists(repo, branch):
        logger.info(
            f"PR #{pr_number}: spec branch {branch} already exists — skipping"
        )
        return None

    worktree_path: Path | None = None
    try:
        worktree_path = workspace.create_auxiliary_worktree(
            clone_path, branch=branch, base_sha=base_sha,
            label=f"spec-pr{pr_number}",
            config=config,
        )
    except subprocess.CalledProcessError as e:
        stderr = ""
        if getattr(e, "stderr", None):
            raw = e.stderr if isinstance(e.stderr, bytes) else str(e.stderr).encode()
            stderr = raw.decode("utf-8", errors="replace")[:200]
        logger.warning(
            f"PR #{pr_number}: could not create spec worktree: {stderr}"
        )
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PR #{pr_number}: spec worktree error: {e}")
        return None

    try:
        dst_dir = worktree_path / target_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for idx, src in enumerate(spec_files):
            if not src.exists():
                logger.info(f"PR #{pr_number}: spec file missing: {src}")
                continue
            dst = dst_dir / f"spec_pr{pr_number}_{idx}_{src.name}"
            try:
                shutil.copy2(src, dst)
                copied.append(str(dst.relative_to(worktree_path)))
            except OSError as e:
                logger.warning(f"PR #{pr_number}: spec copy failed {src}: {e}")
        if not copied:
            logger.info(f"PR #{pr_number}: no spec files copied; aborting PR")
            return None

        message = (
            f"chore(gate-specs): persist verified intent tests from PR #{pr_number}"
        )
        commit = github.commit_and_push(worktree_path, message, branch=branch)
        if commit.status != "pushed":
            logger.warning(
                f"PR #{pr_number}: spec push returned status={commit.status}"
            )
            return None

        title = f"[Gate] spec tests from PR #{pr_number}"
        body_lines = [
            f"Auto-generated by Gate from PR #{pr_number}.",
            "",
            "These tests were written by the logic review agent, marked",
            "`intent_type: confirmed_correct`, and passed a one-point",
            "mutation check (a minimal mutant broke at least one",
            "assertion). Merging them locks in that behaviour.",
            "",
            "**Included files:**",
            *[f"- `{p}`" for p in copied],
            "",
            f"Related: #{pr_number}",
        ]
        return github.create_pr(
            repo=repo,
            title=title,
            body="\n".join(body_lines),
            head=branch,
            base=base_branch,
        )
    finally:
        if worktree_path is not None:
            try:
                workspace.remove_worktree(worktree_path)
            except Exception as e:  # noqa: BLE001
                logger.info(
                    f"PR #{pr_number}: spec worktree teardown failed: {e}"
                )
