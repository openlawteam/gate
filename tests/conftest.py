"""Shared test fixtures for Gate tests.

Isolation model (adopted from Hopper's conftest):

The autouse ``isolate_paths`` fixture redirects BOTH the install/package
directory (``GATE_DIR``) and the runtime data directory (``GATE_DATA_DIR``)
to per-test temp dirs. Every test gets its own fake filesystem, so:

- No test ever writes to the real ``~/Library/Application Support/gate``
  (macOS) or ``~/.local/share/gate`` (Linux) runtime directory.
- No test ever writes to the real repo's ``config/`` or ``logs/`` tree.
- Tests are function-scoped: each test gets a fresh temp dir with no
  cross-contamination.

The fixture copies the real ``config/gate.toml`` into the fake install dir
(if present) so tests that load config still see reasonable defaults. A
minimal ``prompts/`` is also mirrored so prompt-loading tests keep working.

If a test needs the *real* package directory (e.g. to validate that a
shipped prompt parses), request the ``real_gate_dir`` fixture.
"""

import shutil

# Dev-dependency guard (PR A.3). ``pip install -e .`` (no extras) leaves
# ``pytest-asyncio`` uninstalled, which then manifests as 25 opaque
# collection failures in test_tui.py. Detect the missing dep at
# collection time and fail loud with the correct install command so
# the next operator doesn't chase the same red herring I did.
try:
    import pytest_asyncio  # noqa: F401
except ImportError as _e:  # pragma: no cover - depends on install mode
    raise ImportError(
        "Missing test dependency 'pytest-asyncio'. Install dev dependencies "
        "with:  pip install -e '.[dev]'  "
        "(a bare `pip install -e .` omits the `[dev]` extra and is NOT enough "
        "to run Gate's test suite.)"
    ) from _e

import pytest

import gate.config

REAL_GATE_DIR = gate.config.GATE_DIR
REAL_DATA_DIR = gate.config.GATE_DATA_DIR


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    """Redirect GATE_DIR + GATE_DATA_DIR to per-test temp directories.

    Function-scoped so each test gets a clean filesystem. Uses monkeypatch
    so the teardown is automatic and can't leak between tests.
    """
    fake_install = tmp_path / "install"
    fake_data = tmp_path / "data"
    (fake_install / "config").mkdir(parents=True)
    (fake_install / "prompts").mkdir(parents=True)
    (fake_data / "state").mkdir(parents=True)
    (fake_data / "logs").mkdir(parents=True)

    toml_src = REAL_GATE_DIR / "config" / "gate.toml"
    if toml_src.exists():
        (fake_install / "config" / "gate.toml").write_text(toml_src.read_text())

    real_prompts = REAL_GATE_DIR / "prompts"
    if real_prompts.exists():
        for entry in real_prompts.iterdir():
            if entry.is_file() and entry.suffix == ".md":
                shutil.copy2(entry, fake_install / "prompts" / entry.name)

    monkeypatch.setattr(gate.config, "GATE_DIR", fake_install)
    monkeypatch.setattr(gate.config, "GATE_DATA_DIR", fake_data)
    yield tmp_path


@pytest.fixture
def real_gate_dir(monkeypatch):
    """Temporarily restore the real GATE_DIR for tests that validate shipped assets."""
    monkeypatch.setattr(gate.config, "GATE_DIR", REAL_GATE_DIR)
    return REAL_GATE_DIR


@pytest.fixture(autouse=True)
def clean_gate_env(monkeypatch):
    """Remove gate-specific env vars so tests default to an unconfigured shell."""
    for var in (
        "GATE_PAT",
        "GATE_NTFY_TOPIC",
        "GATE_DISCORD_WEBHOOK",
        "GATE_CODEX_THREAD_ID",
        "GATE_FIX_WORKSPACE",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


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
