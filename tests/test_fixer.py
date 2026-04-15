"""Tests for gate.fixer module — fix helpers and pipeline logic."""

import subprocess

from unittest.mock import MagicMock, patch

from gate.fixer import (
    _build_build_error_prompt,
    _build_rereview_feedback_prompt,
    _match_glob,
    build_verify,
    cleanup_artifacts,
    cleanup_gate_tests,
    enforce_blocklist,
    sort_findings_by_severity,
    write_diff,
)


class TestBuildVerifySkip:
    def test_build_verify_skips_without_package_json(self, tmp_path):
        result = build_verify(tmp_path)
        assert result["pass"] is True
        assert result["tsc_errors"] == 0
        assert result["lint_errors"] == 0
        assert result["test_failures"] == 0
        assert result["tsc_log"] == ""
        assert result["lint_log"] == ""


class TestMatchGlob:
    def test_exact_match(self):
        assert _match_glob("package.json", "package.json") is True
        assert _match_glob("package.json", "other.json") is False

    def test_star_wildcard(self):
        assert _match_glob("foo.lock", "*.lock") is True
        assert _match_glob("bar.lock", "*.lock") is True
        assert _match_glob("foo.txt", "*.lock") is False

    def test_directory_glob(self):
        assert _match_glob(".github/workflows/ci.yml", ".github/**") is True
        assert _match_glob(".github/gate/test.js", ".github/**") is True
        assert _match_glob("src/foo.ts", ".github/**") is False

    def test_exact_dir(self):
        assert _match_glob(".github", ".github/**") is True


class TestEnforceBlocklist:
    @patch("gate.fixer._revert_file")
    @patch("gate.fixer._get_changed_files")
    def test_reverts_blocklisted_files(self, mock_changed, mock_revert, tmp_path):
        blocklist = tmp_path / "config" / "fix-blocklist.txt"
        blocklist.parent.mkdir(parents=True)
        blocklist.write_text("package-lock.json\n*.lock\n# comment\n\n")

        mock_changed.return_value = ["src/foo.ts", "package-lock.json", "yarn.lock"]

        with patch("gate.fixer.gate_dir", return_value=tmp_path):
            violations = enforce_blocklist(tmp_path)

        assert "package-lock.json" in violations
        assert "yarn.lock" in violations
        assert "src/foo.ts" not in violations
        assert mock_revert.call_count == 2

    @patch("gate.fixer._get_changed_files")
    def test_no_blocklist_file(self, mock_changed, tmp_path):
        with patch("gate.fixer.gate_dir", return_value=tmp_path):
            violations = enforce_blocklist(tmp_path)
        assert violations == []


class TestCleanupGateTests:
    def test_removes_gate_test_dir(self, tmp_path):
        gate_dir = tmp_path / "tests" / "gate"
        gate_dir.mkdir(parents=True)
        (gate_dir / "test.ts").write_text("test")

        with patch("gate.fixer.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            cleanup_gate_tests(tmp_path)

        assert not gate_dir.exists()

    def test_removes_gate_test_files(self, tmp_path):
        (tmp_path / "__gate_test_foo.ts").write_text("test")
        (tmp_path / "__gate_fix_test_bar.ts").write_text("test")

        with patch("gate.fixer.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            cleanup_gate_tests(tmp_path)

        assert not (tmp_path / "__gate_test_foo.ts").exists()
        assert not (tmp_path / "__gate_fix_test_bar.ts").exists()


class TestSortFindingsBySeverity:
    def test_sorts_critical_first(self):
        findings = [
            {"severity": "info", "message": "note"},
            {"severity": "critical", "message": "critical"},
            {"severity": "warning", "message": "warn"},
            {"severity": "error", "message": "err"},
        ]
        result = sort_findings_by_severity(findings)
        assert result[0]["severity"] == "critical"
        assert result[1]["severity"] == "error"
        assert result[2]["severity"] == "warning"
        assert result[3]["severity"] == "info"

    def test_empty_list(self):
        assert sort_findings_by_severity([]) == []


class TestWriteDiff:
    @patch("gate.fixer._run_silent")
    def test_writes_diff_file(self, mock_run, tmp_path):
        mock_run.return_value = "diff --git a/foo.ts\n+line\n"
        write_diff(tmp_path)
        assert (tmp_path / "fix-diff.txt").exists()
        assert "diff" in (tmp_path / "fix-diff.txt").read_text()

    @patch("gate.fixer._run_silent")
    def test_no_changes(self, mock_run, tmp_path):
        mock_run.return_value = ""
        write_diff(tmp_path)
        assert (tmp_path / "fix-diff.txt").read_text() == "(no changes)"


class TestBuildErrorPrompt:
    def test_includes_tsc_errors(self):
        result = _build_build_error_prompt({
            "tsc_errors": 3,
            "lint_errors": 0,
            "tsc_log": "error TS2322: Type mismatch",
        })
        assert "TypeScript Errors (3)" in result
        assert "TS2322" in result

    def test_includes_lint_errors(self):
        result = _build_build_error_prompt({
            "tsc_errors": 0,
            "lint_errors": 2,
            "lint_log": "no-unused-vars",
        })
        assert "Lint Errors (2)" in result

    def test_no_errors(self):
        result = _build_build_error_prompt({"tsc_errors": 0, "lint_errors": 0})
        assert "Build Errors After Fix" in result


class TestRereviewFeedbackPrompt:
    def test_includes_json(self):
        rereview = {"pass": False, "issues": [{"message": "regression"}]}
        result = _build_rereview_feedback_prompt(rereview)
        assert "Re-Review Feedback" in result
        assert "regression" in result
        assert "```json" in result


class TestCleanupArtifacts:
    def test_removes_known_files(self, tmp_path):
        (tmp_path / "diff.txt").write_text("d")
        (tmp_path / "verdict.json").write_text("{}")
        (tmp_path / "fix-build.json").write_text("{}")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert not (tmp_path / "diff.txt").exists()
        assert not (tmp_path / "verdict.json").exists()
        assert not (tmp_path / "fix-build.json").exists()
        assert "diff.txt" in removed
        assert "verdict.json" in removed

    def test_removes_glob_matched_files(self, tmp_path):
        (tmp_path / "architecture-findings.json").write_text("{}")
        (tmp_path / "logic-result.json").write_text("{}")
        (tmp_path / "fix-senior-session-id.txt").write_text("abc")
        (tmp_path / "implement.in.md").write_text("prompt")
        (tmp_path / "implement.out.md").write_text("output")
        (tmp_path / "implement_1.in.md").write_text("prompt2")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert not (tmp_path / "architecture-findings.json").exists()
        assert not (tmp_path / "logic-result.json").exists()
        assert not (tmp_path / "fix-senior-session-id.txt").exists()
        assert not (tmp_path / "implement.in.md").exists()
        assert not (tmp_path / "implement_1.in.md").exists()
        assert len(removed) == 6

    def test_removes_fix_build_directory(self, tmp_path):
        build_dir = tmp_path / "fix-build"
        build_dir.mkdir()
        (build_dir / "tsc.log").write_text("log")
        (build_dir / "lint.log").write_text("log")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert not build_dir.exists()
        assert "fix-build/" in removed

    def test_does_not_touch_source_files(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "README.md").write_text("hi")
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.ts").write_text("code")
        (tmp_path / "diff.txt").write_text("artifact")

        with patch("gate.fixer.subprocess.run"):
            cleanup_artifacts(tmp_path)

        assert (tmp_path / "package.json").exists()
        assert (tmp_path / "README.md").exists()
        assert (src / "app.ts").exists()
        assert not (tmp_path / "diff.txt").exists()

    def test_idempotent_when_no_artifacts(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert removed == []
        assert (tmp_path / "package.json").exists()

    def test_globs_do_not_recurse_into_subdirs(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "notes-findings.json").write_text("should stay")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert (src / "notes-findings.json").exists()
        assert removed == []


class TestRevertFile:
    def _init_repo(self, tmp_path):
        """Set up a minimal git repo for _revert_file tests."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=str(tmp_path), capture_output=True,
        )
        tracked = tmp_path / "tracked.txt"
        tracked.write_text("original")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        return tracked

    def test_tracked_file_restored_not_deleted(self, tmp_path):
        from gate.fixer import _revert_file

        tracked = self._init_repo(tmp_path)
        tracked.write_text("modified")

        _revert_file(tmp_path, "tracked.txt")

        assert tracked.exists()
        assert tracked.read_text() == "original"

    def test_untracked_file_removed(self, tmp_path):
        from gate.fixer import _revert_file

        self._init_repo(tmp_path)
        untracked = tmp_path / "new-artifact.json"
        untracked.write_text("{}")

        _revert_file(tmp_path, "new-artifact.json")

        assert not untracked.exists()
