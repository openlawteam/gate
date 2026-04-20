"""Tests for gate.spec_pr — Phase 5 spec-PR promotion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gate import spec_pr
from gate.schemas import CommitResult


def _make_config(
    persist: bool = True,
    target_dir: str = "tests/gate-specs",
    max_files: int = 5,
    base_branch: str | None = None,
) -> dict:
    repo: dict = {
        "persist_spec_tests": persist,
        "spec_tests_dir": target_dir,
        "spec_pr_max_files": max_files,
        "default_branch": "main",
    }
    if base_branch is not None:
        repo["spec_pr_base_branch"] = base_branch
    return {"repo": repo}


class TestBranchName:
    def test_deterministic_from_pr_and_sha(self):
        assert spec_pr._branch_name(42, "abcdef0123456789") == "gate/specs/pr42-abcdef01"

    def test_handles_short_sha(self):
        assert spec_pr._branch_name(1, "abc") == "gate/specs/pr1-abc"

    def test_handles_empty_sha(self):
        # Never raises; falls back to a "nosha" literal
        assert spec_pr._branch_name(7, "") == "gate/specs/pr7-nosha"


class TestTargetDirBlocked:
    def test_empty_blocklist_not_blocked(self, tmp_path):
        blocklist = tmp_path / "fix-blocklist.txt"
        blocklist.write_text("")
        config = _make_config()
        config["repo"]["fix_blocklist"] = str(blocklist)
        assert spec_pr._target_dir_blocked("tests/gate-specs", config) is False

    def test_missing_blocklist_file_not_blocked(self, tmp_path):
        config = _make_config()
        config["repo"]["fix_blocklist"] = str(tmp_path / "does-not-exist.txt")
        assert spec_pr._target_dir_blocked("tests/gate-specs", config) is False

    def test_exact_match_blocks(self, tmp_path):
        blocklist = tmp_path / "blocklist.txt"
        blocklist.write_text("tests/gate-specs\n")
        config = _make_config()
        config["repo"]["fix_blocklist"] = str(blocklist)
        assert spec_pr._target_dir_blocked("tests/gate-specs", config) is True

    def test_recursive_match_blocks(self, tmp_path):
        blocklist = tmp_path / "blocklist.txt"
        blocklist.write_text("infra/**\n")
        config = _make_config()
        config["repo"]["fix_blocklist"] = str(blocklist)
        assert spec_pr._target_dir_blocked("infra", config) is True

    def test_unrelated_pattern_does_not_block(self, tmp_path):
        blocklist = tmp_path / "blocklist.txt"
        blocklist.write_text("migrations/**\n")
        config = _make_config()
        config["repo"]["fix_blocklist"] = str(blocklist)
        assert spec_pr._target_dir_blocked("tests/gate-specs", config) is False


class TestCreateSpecPr:
    """create_spec_pr: the end-to-end promotion path.

    These tests exercise the logic by mocking `github.branch_exists`,
    `workspace.create_auxiliary_worktree`, `github.commit_and_push`,
    `github.create_pr`, and `workspace.remove_worktree`. Filesystem
    side-effects (copying spec files into the worktree) are exercised
    against real tmp_path trees to catch path bugs.
    """

    def _setup_worktree(self, tmp_path: Path) -> Path:
        wt = tmp_path / "worktree"
        wt.mkdir()
        return wt

    def test_returns_none_without_spec_files(self):
        result = spec_pr.create_spec_pr(
            repo="o/r", pr_number=1, spec_files=[],
            base_sha="deadbeef", clone_path="/tmp/clone",
            config=_make_config(),
        )
        assert result is None

    def test_idempotent_when_branch_exists(self, tmp_path):
        spec = tmp_path / "__gate_test_foo.ts"
        spec.write_text("test('x', ...)")
        with patch.object(spec_pr.github, "branch_exists", return_value=True) as be, \
             patch.object(spec_pr.workspace, "create_auxiliary_worktree") as cwt:
            result = spec_pr.create_spec_pr(
                repo="o/r", pr_number=42, spec_files=[spec],
                base_sha="abc12345", clone_path="/tmp/clone",
                config=_make_config(),
            )
        assert result is None
        be.assert_called_once_with("o/r", "gate/specs/pr42-abc12345")
        cwt.assert_not_called()

    def test_target_dir_blocklist_aborts_before_worktree(self, tmp_path):
        blocklist = tmp_path / "blocklist.txt"
        blocklist.write_text("tests/gate-specs/**\n")
        config = _make_config()
        config["repo"]["fix_blocklist"] = str(blocklist)
        spec = tmp_path / "__gate_test_foo.ts"
        spec.write_text("")
        with patch.object(spec_pr.github, "branch_exists") as be, \
             patch.object(spec_pr.workspace, "create_auxiliary_worktree") as cwt:
            result = spec_pr.create_spec_pr(
                repo="o/r", pr_number=1, spec_files=[spec],
                base_sha="deadbeef", clone_path="/tmp/clone",
                config=config,
            )
        assert result is None
        be.assert_not_called()
        cwt.assert_not_called()

    def test_happy_path_copies_and_opens_pr(self, tmp_path):
        spec = tmp_path / "__gate_test_foo.ts"
        spec.write_text("test('it works', () => expect(1).toBe(1))")
        wt = self._setup_worktree(tmp_path)

        with patch.object(spec_pr.github, "branch_exists", return_value=False), \
             patch.object(spec_pr.workspace, "create_auxiliary_worktree", return_value=wt), \
             patch.object(spec_pr.workspace, "remove_worktree") as rm, \
             patch.object(spec_pr.github, "commit_and_push",
                          return_value=CommitResult(status="pushed", sha="new_sha")) as cap, \
             patch.object(spec_pr.github, "create_pr", return_value=101) as cp:
            result = spec_pr.create_spec_pr(
                repo="o/r", pr_number=42, spec_files=[spec],
                base_sha="abc12345", clone_path="/tmp/clone",
                config=_make_config(),
            )

        assert result == 101
        # File copied to the target dir within the worktree
        copied = list((wt / "tests" / "gate-specs").glob("spec_pr42_0_*"))
        assert len(copied) == 1
        assert copied[0].read_text() == spec.read_text()

        cap.assert_called_once()
        args, kwargs = cp.call_args
        assert kwargs["repo"] == "o/r"
        assert kwargs["head"] == "gate/specs/pr42-abc12345"
        assert kwargs["base"] == "main"
        assert "PR #42" in kwargs["title"] or "#42" in kwargs["title"]
        # Worktree always torn down
        rm.assert_called_once_with(wt)

    def test_failure_isolation_create_pr_raises(self, tmp_path):
        """If create_pr raises, we still tear down the worktree and
        return None — the original PR must never be tainted."""
        spec = tmp_path / "__gate_test_foo.ts"
        spec.write_text("")
        wt = self._setup_worktree(tmp_path)

        with patch.object(spec_pr.github, "branch_exists", return_value=False), \
             patch.object(spec_pr.workspace, "create_auxiliary_worktree", return_value=wt), \
             patch.object(spec_pr.workspace, "remove_worktree") as rm, \
             patch.object(spec_pr.github, "commit_and_push",
                          return_value=CommitResult(status="pushed", sha="s")), \
             patch.object(spec_pr.github, "create_pr",
                          side_effect=RuntimeError("network down")):
            with pytest.raises(RuntimeError):
                spec_pr.create_spec_pr(
                    repo="o/r", pr_number=42, spec_files=[spec],
                    base_sha="abc12345", clone_path="/tmp/clone",
                    config=_make_config(),
                )
        rm.assert_called_once_with(wt)

    def test_push_failure_does_not_open_pr(self, tmp_path):
        spec = tmp_path / "__gate_test_foo.ts"
        spec.write_text("")
        wt = self._setup_worktree(tmp_path)
        with patch.object(spec_pr.github, "branch_exists", return_value=False), \
             patch.object(spec_pr.workspace, "create_auxiliary_worktree", return_value=wt), \
             patch.object(spec_pr.workspace, "remove_worktree"), \
             patch.object(spec_pr.github, "commit_and_push",
                          return_value=CommitResult(status="push_failed", error="e")), \
             patch.object(spec_pr.github, "create_pr") as cp:
            result = spec_pr.create_spec_pr(
                repo="o/r", pr_number=1, spec_files=[spec],
                base_sha="deadbeef", clone_path="/tmp/clone",
                config=_make_config(),
            )
        assert result is None
        cp.assert_not_called()

    def test_respects_max_files_cap(self, tmp_path):
        files = []
        for i in range(10):
            p = tmp_path / f"__gate_test_{i}.ts"
            p.write_text("")
            files.append(p)
        wt = self._setup_worktree(tmp_path)
        with patch.object(spec_pr.github, "branch_exists", return_value=False), \
             patch.object(spec_pr.workspace, "create_auxiliary_worktree", return_value=wt), \
             patch.object(spec_pr.workspace, "remove_worktree"), \
             patch.object(spec_pr.github, "commit_and_push",
                          return_value=CommitResult(status="pushed", sha="s")), \
             patch.object(spec_pr.github, "create_pr", return_value=5):
            spec_pr.create_spec_pr(
                repo="o/r", pr_number=1, spec_files=files,
                base_sha="deadbeef", clone_path="/tmp/clone",
                config=_make_config(max_files=3),
            )
        copied = list((wt / "tests" / "gate-specs").iterdir())
        assert len(copied) == 3

    def test_custom_base_branch(self, tmp_path):
        spec = tmp_path / "__gate_test_foo.ts"
        spec.write_text("")
        wt = self._setup_worktree(tmp_path)
        with patch.object(spec_pr.github, "branch_exists", return_value=False), \
             patch.object(spec_pr.workspace, "create_auxiliary_worktree", return_value=wt), \
             patch.object(spec_pr.workspace, "remove_worktree"), \
             patch.object(spec_pr.github, "commit_and_push",
                          return_value=CommitResult(status="pushed", sha="s")), \
             patch.object(spec_pr.github, "create_pr", return_value=7) as cp:
            spec_pr.create_spec_pr(
                repo="o/r", pr_number=1, spec_files=[spec],
                base_sha="deadbeef", clone_path="/tmp/clone",
                config=_make_config(base_branch="develop"),
            )
        assert cp.call_args.kwargs["base"] == "develop"


class TestGithubCreatePr:
    """github.create_pr returns parsed PR number, fail-opens on errors."""

    def test_returns_pr_number_from_url(self):
        from gate import github

        with patch.object(github, "_gh", return_value="https://github.com/o/r/pull/123\n"):
            assert github.create_pr("o/r", "t", "b", "head", "main") == 123

    def test_returns_none_on_gh_failure(self):
        import subprocess as sp

        from gate import github

        with patch.object(
            github, "_gh",
            side_effect=sp.CalledProcessError(1, "gh", stderr="boom"),
        ):
            assert github.create_pr("o/r", "t", "b", "head", "main") is None

    def test_returns_none_on_unparseable_output(self):
        from gate import github

        with patch.object(github, "_gh", return_value="garbage\n"):
            assert github.create_pr("o/r", "t", "b", "head", "main") is None


class TestGithubBranchExists:
    def test_true_when_api_succeeds(self):
        from gate import github

        with patch.object(github, "_gh", return_value=""):
            assert github.branch_exists("o/r", "gate/specs/pr1-abc") is True

    def test_false_when_api_fails(self):
        import subprocess as sp

        from gate import github

        with patch.object(
            github, "_gh",
            side_effect=sp.CalledProcessError(1, "gh", stderr=""),
        ):
            assert github.branch_exists("o/r", "gate/specs/pr1-abc") is False


class TestOrchestratorPromoteSpecTests:
    """Orchestrator integration: _promote_spec_tests + cleanup ordering."""

    def _make_orchestrator(self, tmp_path: Path, config: dict):
        from gate.orchestrator import ReviewOrchestrator

        orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
        orch.pr_number = 42
        orch.repo = "o/r"
        orch.head_sha = "deadbeef1234"
        orch.workspace = tmp_path
        orch.config = config
        orch._clone_path = lambda: "/tmp/clone"  # type: ignore[attr-defined]
        return orch

    def test_disabled_returns_without_side_effects(self, tmp_path):
        config = _make_config(persist=False)
        orch = self._make_orchestrator(tmp_path, config)
        (tmp_path / "logic-findings.json").write_text("{}")
        with patch("gate.spec_pr.create_spec_pr") as csp:
            orch._promote_spec_tests(MagicMock(data={"decision": "approve"}))
        csp.assert_not_called()

    def test_missing_findings_returns_cleanly(self, tmp_path):
        config = _make_config(persist=True)
        orch = self._make_orchestrator(tmp_path, config)
        with patch("gate.spec_pr.create_spec_pr") as csp:
            orch._promote_spec_tests(MagicMock(data={"decision": "approve"}))
        csp.assert_not_called()

    def test_no_qualifying_tests_skips(self, tmp_path):
        config = _make_config(persist=True)
        orch = self._make_orchestrator(tmp_path, config)
        (tmp_path / "logic-findings.json").write_text(
            '{"tests_written":[{"intent_type":"inconclusive"}]}'
        )
        with patch("gate.spec_pr.create_spec_pr") as csp:
            orch._promote_spec_tests(MagicMock(data={"decision": "approve"}))
        csp.assert_not_called()

    def test_qualifying_tests_copied_and_spec_pr_invoked(
        self, tmp_path, monkeypatch
    ):
        config = _make_config(persist=True)
        orch = self._make_orchestrator(tmp_path, config)
        spec_src = tmp_path / "__gate_test_foo.ts"
        spec_src.write_text("t('a', ...)")
        (tmp_path / "logic-findings.json").write_text(
            '{"tests_written":[{"file":"__gate_test_foo.ts",'
            '"intent_type":"confirmed_correct",'
            '"mutation_check":{"result":"fail"}}]}'
        )
        sidecar_dir = tmp_path / "state"
        sidecar_dir.mkdir()

        import gate.state as state_mod
        monkeypatch.setattr(state_mod, "get_pr_state_dir", lambda *a, **kw: sidecar_dir)

        with patch("gate.spec_pr.create_spec_pr", return_value=99) as csp:
            orch._promote_spec_tests(MagicMock(data={"decision": "approve"}))
        csp.assert_called_once()
        # sidecar received the file
        assert (sidecar_dir / "spec_tests" / "__gate_test_foo.ts").exists()

    def test_spec_pr_exception_is_swallowed(self, tmp_path, monkeypatch):
        config = _make_config(persist=True)
        orch = self._make_orchestrator(tmp_path, config)
        spec_src = tmp_path / "__gate_test_foo.ts"
        spec_src.write_text("")
        (tmp_path / "logic-findings.json").write_text(
            '{"tests_written":[{"file":"__gate_test_foo.ts",'
            '"intent_type":"confirmed_correct",'
            '"mutation_check":{"result":"fail"}}]}'
        )
        sidecar_dir = tmp_path / "state"
        sidecar_dir.mkdir()
        import gate.state as state_mod
        monkeypatch.setattr(state_mod, "get_pr_state_dir", lambda *a, **kw: sidecar_dir)
        with patch("gate.spec_pr.create_spec_pr",
                   side_effect=RuntimeError("gh 500")):
            # MUST NOT raise
            orch._promote_spec_tests(MagicMock(data={"decision": "approve"}))


class TestCleanupUnderscoreGateTests:
    def _make_orch(self, tmp_path):
        from gate.orchestrator import ReviewOrchestrator

        orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
        orch.workspace = tmp_path
        return orch

    def test_removes_underscore_patterns(self, tmp_path):
        (tmp_path / "__gate_test_a.ts").write_text("")
        (tmp_path / "__gate_fix_test_b.ts").write_text("")
        (tmp_path / "normal.ts").write_text("")
        orch = self._make_orch(tmp_path)
        orch._cleanup_underscore_gate_tests()
        assert not (tmp_path / "__gate_test_a.ts").exists()
        assert not (tmp_path / "__gate_fix_test_b.ts").exists()
        assert (tmp_path / "normal.ts").exists()

    def test_idempotent(self, tmp_path):
        orch = self._make_orch(tmp_path)
        orch._cleanup_underscore_gate_tests()
        orch._cleanup_underscore_gate_tests()

    def test_no_workspace_is_noop(self):
        from gate.orchestrator import ReviewOrchestrator
        orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
        orch.workspace = None
        orch._cleanup_underscore_gate_tests()  # no crash
