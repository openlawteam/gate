"""Claude Code wrapper for Gate — spawns review stages in tmux windows.

Creates tmux windows running `gate process <review_id> <stage>` commands.
"""

import os
import shlex

from gate.tmux import new_window, select_window


def spawn_review_stage(
    review_id: str,
    stage: str,
    workspace: str,
    socket_path: str | None = None,
    foreground: bool = False,
    repo: str = "",
) -> str | None:
    """Spawn a review stage runner in a new tmux window.

    Creates a tmux window running `gate process <review_id> <stage>`.
    The ReviewRunner inside that window handles Claude lifecycle.

    Args:
        review_id: Review identifier (e.g., "org-repo-pr42").
        stage: Stage name (architecture, security, logic, fix-senior).
        workspace: Worktree path for the review.
        socket_path: Server socket path for IPC (Phase 4+).
        foreground: If True, switch to the new window.
        repo: GitHub owner/repo string for per-repo config resolution.

    Returns:
        The tmux pane ID on success, None on failure.
    """
    path = os.environ.get("PATH", "/usr/bin:/bin")
    fail = "echo 'gate process failed (exit $?)'; sleep 10"

    inner_parts = [
        f"export PATH={shlex.quote(path)};",
        "gate process",
        shlex.quote(review_id),
        shlex.quote(stage),
        f"--workspace {shlex.quote(workspace)}",
    ]
    if repo:
        inner_parts.append(f"--repo {shlex.quote(repo)}")
    if socket_path:
        inner_parts.append(f"--socket {shlex.quote(socket_path)}")

    inner = " ".join(inner_parts) + f" || {{ {fail}; }}"
    command = f"/bin/sh -c {shlex.quote(inner)}"
    return new_window(command, cwd=workspace, background=not foreground)


def switch_to_pane(pane_id: str) -> bool:
    """Switch to the tmux window containing the given pane.

    Args:
        pane_id: The tmux pane ID to switch to (e.g., "%1").

    Returns:
        True if successfully switched, False otherwise.
    """
    return select_window(pane_id)
