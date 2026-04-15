"""Shared test fixtures for Gate tests."""


import pytest

import gate.config


REAL_GATE_DIR = gate.config.GATE_DIR


@pytest.fixture(autouse=True, scope="session")
def isolate_gate_dir(tmp_path_factory):
    """Redirect gate_dir to a temp directory so tests never write to production logs."""
    fake_dir = tmp_path_factory.mktemp("gate")
    (fake_dir / "logs").mkdir()
    (fake_dir / "state").mkdir()
    config_src = REAL_GATE_DIR / "config"
    config_dst = fake_dir / "config"
    config_dst.mkdir()
    toml_src = config_src / "gate.toml"
    if toml_src.exists():
        (config_dst / "gate.toml").write_text(toml_src.read_text())
    gate.config.GATE_DIR = fake_dir
    yield fake_dir
    gate.config.GATE_DIR = REAL_GATE_DIR


@pytest.fixture
def real_gate_dir(isolate_gate_dir):
    """Temporarily restore the real GATE_DIR for tests that validate the package layout."""
    gate.config.GATE_DIR = REAL_GATE_DIR
    yield REAL_GATE_DIR
    gate.config.GATE_DIR = isolate_gate_dir


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace with common context files."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    (workspace / "diff.txt").write_text("diff --git a/foo.ts b/foo.ts\n+const x = 1;\n")
    (workspace / "changed_files.txt").write_text("foo.ts\nbar.ts\n")
    (workspace / "file_count.txt").write_text("2")
    (workspace / "lines_changed.txt").write_text("2 files changed, 10 insertions(+)")
    (workspace / "diff_stats.txt").write_text(
        " foo.ts | 5 +++++\n bar.ts | 5 +++++\n 2 files changed, 10 insertions(+)\n"
    )

    return workspace


@pytest.fixture
def sample_config():
    """Return a minimal gate config dict."""
    return {
        "repo": {
            "name": "test-org/test-repo",
            "clone_path": "~/test-repo",
            "worktree_base": "/tmp/gate-worktrees",
            "bot_account": "test-bot",
        },
        "models": {
            "triage": "sonnet",
            "architecture": "sonnet",
            "security": "opus",
            "logic": "opus",
            "verdict": "sonnet",
            "fix_senior": "opus",
            "fix_rereview": "sonnet",
        },
        "timeouts": {
            "agent_stage_s": 900,
            "structured_stage_s": 120,
            "fix_session_s": 2400,
            "stuck_threshold_s": 120,
            "hard_timeout_s": 1200,
        },
        "retry": {
            "max_retries": 4,
            "base_delay_s": 60,
            "transient_base_delay_s": 10,
        },
        "limits": {
            "max_review_cycles": 5,
            "max_fix_attempts_soft": 3,
            "max_fix_attempts_total": 6,
            "fix_cooldown_s": 600,
            "max_diff_bytes": 512000,
            "max_pr_body_bytes": 51200,
            "max_file_list_bytes": 51200,
            "triage_diff_budget_bytes": 153600,
        },
    }


@pytest.fixture
def multi_repo_config():
    """Return a config dict with [[repos]] array format."""
    return {
        "repos": [
            {
                "name": "org-a/repo-a",
                "clone_path": "~/repo-a",
                "worktree_base": "/tmp/gate-worktrees",
                "bot_account": "bot-a",
                "default_branch": "main",
            },
            {
                "name": "org-b/repo-b",
                "clone_path": "~/repo-b",
                "worktree_base": "/tmp/gate-worktrees",
                "bot_account": "bot-b",
                "default_branch": "develop",
            },
        ],
        "models": {"triage": "sonnet", "verdict": "sonnet"},
        "timeouts": {"agent_stage_s": 900},
        "limits": {"max_fix_attempts_soft": 3, "max_fix_attempts_total": 6, "fix_cooldown_s": 600},
    }


@pytest.fixture
def triage_result():
    """Return a sample triage result."""
    return {
        "change_type": "feature",
        "risk_level": "medium",
        "summary": "Adds new API endpoint",
        "files_by_category": {"api": ["foo.ts"]},
        "fast_track_eligible": False,
        "fast_track_reason": None,
        "flags": [],
    }


@pytest.fixture
def verdict_result():
    """Return a sample verdict result."""
    return {
        "decision": "approve",
        "confidence": "high",
        "summary": "Changes look good",
        "findings": [],
        "resolved_findings": [],
        "stats": {"stages_run": 4, "total_findings": 0},
        "review_time_seconds": 120,
    }
