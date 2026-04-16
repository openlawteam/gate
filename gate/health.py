"""Infrastructure health monitoring.

Ported from health-check.sh, adjusted for the new architecture:
- Monitor 1 runner (not 3) per Revision 3
- Orphaned check run cleanup (Revision 4) — critical safety net
- Orphaned tmux window cleanup
- No status-server or gate-logger checks (replaced by Python)
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from gate import github, notify
from gate.config import get_all_repos, load_config, logs_dir, socket_path, state_dir
from gate.io import atomic_write
from gate.logger import read_recent_decisions
from gate.quota import quota_cache_path

logger = logging.getLogger(__name__)

ALERT_COOLDOWN_S = 3600
HARD_TIMEOUT_S = 1200


def run_health_check() -> dict:
    """Run all health checks. Returns structured results."""
    results = {
        "sleep_disabled": check_sleep_disabled(),
        "runner": check_runner(),
        "github_api": check_github_api(),
        "tailscale": check_tailscale(),
        "disk": check_disk_usage(),
        "tmux_session": check_tmux_session(),
        "gate_server": check_gate_server(),
        "stuck_reviews": check_stuck_reviews(),
        "orphaned_checks": check_orphaned_check_runs(),
        "orphaned_windows": check_orphaned_tmux_windows(),
        "circuit_breaker": check_circuit_breaker(),
        "quota": check_quota_freshness(),
        "recent_errors": check_recent_errors(),
    }

    errors = [k for k, v in results.items() if isinstance(v, dict) and not v.get("ok", True)]
    if errors:
        _send_alerts(results, errors)

    return results


def check_sleep_disabled() -> dict:
    """Verify sleep is disabled on the machine."""
    import re

    try:
        result = subprocess.run(
            ["pmset", "-g"], capture_output=True, text=True, timeout=5
        )
        match = re.search(r"SleepDisabled\s+(\d)", result.stdout)
        disabled = match is not None and match.group(1) == "1"
        if not disabled:
            subprocess.run(
                ["sudo", "pmset", "-a", "disablesleep", "1"],
                capture_output=True, timeout=10,
            )
        detail = "sleep disabled" if disabled else "sleep NOT disabled — fix attempted"
        return {"ok": disabled, "detail": detail}
    except (subprocess.SubprocessError, OSError):
        return {"ok": False, "detail": "sleep check failed"}


def check_runner() -> dict:
    """Check single GHA runner health (Revision 3: 1 runner)."""
    config = load_config()
    runner_path = config.get("runner", {}).get("path", "")
    if not runner_path:
        return {"ok": True, "detail": "no runner configured"}
    runner_dir = Path(runner_path)
    runner_name = runner_dir.name
    if not runner_dir.exists():
        return {"ok": False, "detail": f"{runner_name} directory not found"}

    try:
        result = subprocess.run(
            ["./svc.sh", "status"],
            capture_output=True, text=True,
            cwd=str(runner_dir), timeout=10,
        )
        is_running = "started" in result.stdout.lower() or "active" in result.stdout.lower()
        if not is_running:
            subprocess.run(
                ["./svc.sh", "start"],
                capture_output=True, cwd=str(runner_dir), timeout=30,
            )
            notify.runner_down(runner_name)
            return {"ok": False, "detail": "runner was stopped, restarted"}
        return {"ok": True, "detail": f"{runner_name} running"}
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": False, "detail": f"runner check failed: {e}"}


def check_github_api() -> dict:
    """Check GitHub API connectivity."""
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "5", "https://api.github.com/zen"],
            capture_output=True, text=True, timeout=10,
        )
        ok = result.returncode == 0 and len(result.stdout.strip()) > 0
        return {"ok": ok, "detail": "reachable" if ok else "unreachable"}
    except (subprocess.SubprocessError, OSError):
        return {"ok": False, "detail": "unreachable"}


def check_tailscale() -> dict:
    """Check Tailscale connectivity for remote access."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Tailscale"],
            capture_output=True, timeout=5,
        )
        ok = result.returncode == 0
        return {"ok": ok, "detail": "running" if ok else "not running"}
    except (subprocess.SubprocessError, OSError):
        return {"ok": True, "detail": "check skipped"}


def check_disk_usage() -> dict:
    """Check disk space. Alert above 85%."""
    try:
        result = subprocess.run(
            ["df", "-P", "/"], capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            pct_str = lines[1].split()[4].replace("%", "")
            pct = int(pct_str)
            ok = pct <= 85
            if not ok:
                _cleanup_old_worktrees()
            return {"ok": ok, "detail": f"{pct}% used"}
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        pass
    return {"ok": False, "detail": "disk check failed — could not parse df output"}


def check_tmux_session() -> dict:
    """Check that a gate tmux session exists."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", "gate"],
            capture_output=True, timeout=5,
        )
        ok = result.returncode == 0
        return {"ok": ok, "detail": "session exists" if ok else "no session"}
    except (subprocess.SubprocessError, OSError):
        return {"ok": True, "detail": "tmux not available"}


def check_gate_server() -> dict:
    """Check if the Gate server is running."""
    from gate.client import ping

    ok = ping(socket_path(), timeout=2.0)
    return {"ok": ok, "detail": "running" if ok else "not running"}


def check_stuck_reviews() -> dict:
    """Check for reviews stuck in progress too long.

    Handles both legacy (state/pr{N}/) and multi-repo (state/{slug}/pr{N}/) layouts.
    """
    state_root = state_dir()
    if not state_root.exists():
        return {"ok": True, "detail": "no state"}

    config = load_config()
    timeout = config.get("timeouts", {}).get("hard_timeout_s", HARD_TIMEOUT_S)

    stuck = []

    def _check_pr_dir(pr_dir: Path, label: str) -> None:
        marker = pr_dir / "active_review.json"
        if not marker.exists():
            return
        try:
            data = json.loads(marker.read_text())
            started_at = data.get("started_at", 0)
            age = time.time() - started_at
            if age > timeout:
                stuck.append(label)
        except (json.JSONDecodeError, OSError):
            pass

    for entry in state_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("pr"):
            _check_pr_dir(entry, entry.name)
        else:
            for sub in entry.iterdir():
                if sub.is_dir() and sub.name.startswith("pr"):
                    _check_pr_dir(sub, f"{entry.name}/{sub.name}")

    ok = len(stuck) == 0
    detail = f"{len(stuck)} stuck: {', '.join(stuck)}" if stuck else "none"
    return {"ok": ok, "detail": detail}


def check_orphaned_check_runs() -> dict:
    """Find and clean up check runs stuck in_progress with no active review.

    Critical safety net for Revision 4. If Python crashes after creating a
    check run but before completing it, the check blocks the PR forever.
    Handles both legacy (state/pr{N}/) and multi-repo (state/{slug}/pr{N}/) layouts.
    """
    state_root = state_dir()
    if not state_root.exists():
        return {"ok": True, "detail": "no state"}

    config = load_config()
    timeout = config.get("timeouts", {}).get("hard_timeout_s", HARD_TIMEOUT_S)

    cleaned = []

    def _process_marker(pr_dir: Path, default_repo: str, label: str) -> None:
        marker = pr_dir / "active_review.json"
        if not marker.exists():
            return

        try:
            data = json.loads(marker.read_text())
        except (json.JSONDecodeError, OSError):
            return

        pid = data.get("pid")
        check_run_id = data.get("check_run_id")
        started_at = data.get("started_at", 0)
        age = time.time() - started_at
        repo = data.get("repo", default_repo)

        pid_alive = _is_pid_alive(pid) if pid else False

        if not pid_alive and age > 60:
            pr_num = pr_dir.name.replace("pr", "")

            if not repo:
                logger.warning(f"Skipping orphan cleanup for {label}: no repo configured")
                marker.unlink(missing_ok=True)
                cleaned.append(label)
                return

            head_sha = data.get("head_sha", "")
            if check_run_id and head_sha:
                github.complete_check_run(
                    repo, check_run_id,
                    conclusion="cancelled",
                    output_title="Gate Review: orphaned",
                    output_summary="Review process died. PR auto-approved (fail-open).",
                    sha=head_sha,
                )
            try:
                pr_number = int(pr_num)
                github.approve_pr(
                    repo, pr_number,
                    "**Gate (error)** — review process died. Auto-approving.",
                )
            except ValueError:
                pass

            marker.unlink(missing_ok=True)
            cleaned.append(label)
            pr = int(pr_num) if pr_num.isdigit() else 0
            notify.review_failed(pr, "orphaned check run", repo=repo)
            logger.warning(f"Cleaned orphaned check run for {label}")

        elif age > timeout and pid_alive:
            logger.warning(f"Review for {label} exceeds timeout but PID {pid} still alive")

    default_repo = config.get("repo", {}).get("name", "")
    for entry in state_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("pr"):
            _process_marker(entry, default_repo, entry.name)
        else:
            for sub in entry.iterdir():
                if sub.is_dir() and sub.name.startswith("pr"):
                    _process_marker(sub, default_repo, f"{entry.name}/{sub.name}")

    ok = len(cleaned) == 0
    detail = f"cleaned {len(cleaned)}: {', '.join(cleaned)}" if cleaned else "none"
    return {"ok": ok, "detail": detail}


def check_orphaned_tmux_windows() -> dict:
    """Find tmux windows for gate reviews with no active review.

    Handles both legacy (pr{N}-{stage}) and multi-repo ({slug}-pr{N}-{stage}) window names.
    Uses review IDs from active_review.json markers for robust matching.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", "#{window_name} #{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {"ok": True, "detail": "no tmux"}
    except (subprocess.SubprocessError, OSError):
        return {"ok": True, "detail": "tmux not available"}

    state_root = state_dir()
    active_review_ids: set[str] = set()
    if state_root.exists():
        for entry in state_root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.startswith("pr"):
                marker = entry / "active_review.json"
                if marker.exists():
                    try:
                        data = json.loads(marker.read_text())
                        rid = data.get("review_id", entry.name)
                    except (json.JSONDecodeError, OSError):
                        rid = entry.name
                    active_review_ids.add(rid)
            else:
                for sub in entry.iterdir():
                    if sub.is_dir() and sub.name.startswith("pr"):
                        marker = sub / "active_review.json"
                        if marker.exists():
                            try:
                                data = json.loads(marker.read_text())
                                rid = data.get("review_id", f"{entry.name}-{sub.name}")
                            except (json.JSONDecodeError, OSError):
                                rid = f"{entry.name}-{sub.name}"
                            active_review_ids.add(rid)

    killed = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        window_name, pane_id = parts[0], parts[1]

        matched = False
        for rid in active_review_ids:
            if window_name.startswith(rid + "-"):
                matched = True
                break

        if not matched and (window_name.startswith("pr") or "-pr" in window_name):
            from gate.tmux import kill_window

            kill_window(pane_id)
            killed.append(window_name)
            logger.info(f"Killed orphaned tmux window: {window_name}")

    ok = len(killed) == 0
    detail = f"killed {len(killed)}: {', '.join(killed)}" if killed else "none"
    return {"ok": ok, "detail": detail}


def check_circuit_breaker() -> dict:
    """Check if circuit breaker is tripped (last 3 reviews = error)."""
    recent = read_recent_decisions(3)
    tripped = len(recent) >= 3 and all(d == "error" for d in recent)
    return {
        "ok": not tripped,
        "detail": "tripped — last 3 reviews were errors" if tripped else "ok",
    }


def check_quota_freshness() -> dict:
    """Check if quota cache is reasonably fresh."""
    cache_path = quota_cache_path()
    if not cache_path.exists():
        return {"ok": True, "detail": "no cache"}
    try:
        age = time.time() - cache_path.stat().st_mtime
        ok = age < 1800
        return {"ok": ok, "detail": f"age={int(age)}s" + ("" if ok else " (stale)")}
    except OSError:
        return {"ok": True, "detail": "check skipped"}


def check_recent_errors() -> dict:
    """Check for cascading review failures."""
    recent = read_recent_decisions(5)
    error_count = sum(1 for d in recent if d == "error")
    ok = error_count < 3
    return {"ok": ok, "detail": f"{error_count}/5 errors"}


# ── Helpers ──────────────────────────────────────────────────


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _cleanup_old_worktrees() -> None:
    """Remove worktrees older than 2 hours."""
    config = load_config()
    bases: set[str] = set()
    for repo_cfg in get_all_repos(config):
        bases.add(repo_cfg.get("worktree_base", "/tmp/gate-worktrees"))
    if not bases:
        bases.add("/tmp/gate-worktrees")
    for worktree_base in bases:
        try:
            subprocess.run(
                ["find", worktree_base, "-maxdepth", "1", "-mmin", "+120",
                 "-exec", "rm", "-rf", "{}", ";"],
                capture_output=True, timeout=30,
            )
        except (subprocess.SubprocessError, OSError):
            pass


def _send_alerts(results: dict, errors: list[str]) -> None:
    """Send notifications for health check failures with cooldown."""
    alert_state_path = logs_dir() / ".health-alert-state"
    alert_state: dict[str, float] = {}

    try:
        if alert_state_path.exists():
            for line in alert_state_path.read_text().strip().split("\n"):
                if "=" in line:
                    key, val = line.split("=", 1)
                    alert_state[key] = float(val)
    except (OSError, ValueError):
        pass

    now = time.time()
    sent = []
    for key in errors:
        last_alert = alert_state.get(key, 0)
        if now - last_alert < ALERT_COOLDOWN_S:
            continue

        detail = results[key].get("detail", key) if isinstance(results[key], dict) else key
        notify.notify(
            f"Gate Health Alert: {key}",
            detail,
            tags="warning",
            priority="high",
        )
        alert_state[key] = now
        sent.append(key)

    if sent:
        try:
            lines = [f"{k}={v}" for k, v in alert_state.items()]
            atomic_write(alert_state_path, "\n".join(lines) + "\n")
        except OSError:
            pass
