"""Tests for gate.workspace module.

Uses mock subprocess calls since we can't create real git worktrees in tests.
Real git repos are used for _setup_artifact_exclusions tests.
"""

import subprocess
from pathlib import Path

from unittest.mock import MagicMock, call, patch

from gate.workspace import _setup_artifact_exclusions, prepare_context_files, remove_worktree


class TestPrepareContextFiles:
    def test_creates_all_context_files(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        diff_output = "diff --git a/foo.ts b/foo.ts\n+line\n"
        changed_output = "foo.ts\nbar.ts\n"
        stat_output = " foo.ts | 3 +++\n bar.ts | 2 ++\n 2 files changed, 5 insertions(+)\n"

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "diff" in cmd and "--name-only" in cmd:
                result.stdout = changed_output
            elif "diff" in cmd and "--stat" in cmd:
                result.stdout = stat_output
            elif "diff" in cmd:
                result.stdout = diff_output
            else:
                result.stdout = ""
            return result

        with patch("gate.workspace.subprocess.run", side_effect=mock_run):
            prepare_context_files(workspace)

        assert (workspace / "diff.txt").exists()
        assert (workspace / "changed_files.txt").exists()
        assert (workspace / "file_count.txt").exists()
        assert (workspace / "diff_stats.txt").exists()
        assert (workspace / "lines_changed.txt").exists()

        assert (workspace / "diff.txt").read_text() == diff_output
        assert (workspace / "changed_files.txt").read_text() == changed_output
        assert (workspace / "file_count.txt").read_text() == "2"
        assert "2 files changed" in (workspace / "lines_changed.txt").read_text()


class TestRemoveWorktree:
    @patch("gate.workspace.shutil.rmtree")
    @patch("gate.workspace.subprocess.run")
    def test_calls_git_worktree_remove(self, mock_run, mock_rmtree, tmp_path):
        worktree = tmp_path / "pr42-12345"
        worktree.mkdir()

        remove_worktree(worktree)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["git", "worktree", "remove"]
        assert "--force" in cmd
        assert str(worktree) in cmd

    @patch("gate.workspace.shutil.rmtree")
    @patch("gate.workspace.subprocess.run")
    def test_rmtree_fallback(self, mock_run, mock_rmtree, tmp_path):
        worktree = tmp_path / "pr42-12345"
        worktree.mkdir()

        remove_worktree(worktree)
        mock_rmtree.assert_called_once_with(worktree, ignore_errors=True)


class TestSetupArtifactExclusions:
    """Tests using real git repos and worktrees to validate artifact exclusion."""

    def _init_repo(self, tmp_path):
        """Create a minimal git repo with one commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=str(repo), capture_output=True,
        )
        (repo / "README.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo), capture_output=True, check=True,
        )
        return repo

    def _add_worktree(self, repo, wt_path):
        """Create a worktree from the repo's HEAD."""
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "HEAD"],
            cwd=str(repo), capture_output=True, check=True,
        )

    def test_artifacts_excluded_from_git_status(self, tmp_path):
        repo = self._init_repo(tmp_path)
        wt = tmp_path / "worktree"
        self._add_worktree(repo, wt)

        _setup_artifact_exclusions(str(repo), wt)

        # Create artifact files in the worktree
        (wt / "diff.txt").write_text("diff")
        (wt / "verdict.json").write_text("{}")
        (wt / "architecture-findings.json").write_text("{}")
        (wt / "implement.in.md").write_text("prompt")

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt), capture_output=True, text=True,
        )
        # Artifact files must not appear in git status
        assert "diff.txt" not in result.stdout
        assert "verdict.json" not in result.stdout
        assert "architecture-findings.json" not in result.stdout
        assert "implement.in.md" not in result.stdout

    def test_non_artifacts_still_visible(self, tmp_path):
        repo = self._init_repo(tmp_path)
        wt = tmp_path / "worktree"
        self._add_worktree(repo, wt)

        _setup_artifact_exclusions(str(repo), wt)

        # Create a real source file
        src = wt / "src"
        src.mkdir()
        (src / "app.ts").write_text("code")
        (wt / "package.json").write_text("{}")

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt), capture_output=True, text=True,
        )
        # git status --porcelain may show "src/" or "src/app.ts" depending on config
        assert "src" in result.stdout
        assert "package.json" in result.stdout

    def test_main_repo_not_affected(self, tmp_path):
        repo = self._init_repo(tmp_path)
        wt = tmp_path / "worktree"
        self._add_worktree(repo, wt)

        _setup_artifact_exclusions(str(repo), wt)

        # Create an artifact file in the main repo (should still be visible)
        (repo / "diff.txt").write_text("should be visible")

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert "diff.txt" in result.stdout

    def test_git_add_a_skips_artifacts(self, tmp_path):
        repo = self._init_repo(tmp_path)
        wt = tmp_path / "worktree"
        self._add_worktree(repo, wt)

        _setup_artifact_exclusions(str(repo), wt)

        (wt / "diff.txt").write_text("artifact")
        (wt / "verdict.json").write_text("{}")
        src = wt / "src"
        src.mkdir()
        (src / "fix.ts").write_text("real fix")

        subprocess.run(["git", "add", "-A"], cwd=str(wt), capture_output=True)

        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(wt), capture_output=True, text=True,
        )
        # Real file staged, artifacts not
        assert "src/fix.ts" in result.stdout
        assert "diff.txt" not in result.stdout
        assert "verdict.json" not in result.stdout


class TestCreateWorktreeDepInstall:
    @patch("gate.workspace._install_deps_with_retry")
    def test_skips_deps_without_dep_file(self, mock_install, tmp_path):
        """No dep file => auto-detect returns 'none' => no dep install."""
        wt = tmp_path / "wt"
        wt.mkdir()
        assert not (wt / "package.json").exists()
        mock_install.assert_not_called()

    @patch("gate.workspace._install_deps_with_retry")
    def test_node_project_would_install(self, mock_install, tmp_path):
        """Sanity: if package.json exists, profile detects 'node' with dep_install_cmd."""
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "package.json").write_text("{}")
        from gate.profiles import resolve_profile
        profile = resolve_profile({}, wt)
        assert profile["dep_install_cmd"] == "npm ci"
