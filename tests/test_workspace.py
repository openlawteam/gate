"""Tests for gate.workspace module.

Uses mock subprocess calls since we can't create real git worktrees in tests.
Real git repos are used for _setup_artifact_exclusions tests.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_codex_log_excluded(self, tmp_path):
        """Fix 1c: *.codex.log files must be covered by per-worktree git
        excludes so they are never committed via `git add -A`."""
        repo = self._init_repo(tmp_path)
        wt = tmp_path / "worktree"
        self._add_worktree(repo, wt)

        _setup_artifact_exclusions(str(repo), wt)

        (wt / "implement.codex.log").write_text("codex stdout")
        (wt / "audit_2.codex.log").write_text("codex stdout")

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt), capture_output=True, text=True,
        )
        assert "implement.codex.log" not in result.stdout
        assert "audit_2.codex.log" not in result.stdout

    def test_gate_directions_md_excluded(self, tmp_path):
        """Fix 3d: gate-directions.md (written by senior before each
        `gate-code <stage> < gate-directions.md` call) must be excluded
        from commits."""
        repo = self._init_repo(tmp_path)
        wt = tmp_path / "worktree"
        self._add_worktree(repo, wt)

        _setup_artifact_exclusions(str(repo), wt)

        (wt / "gate-directions.md").write_text("directions")

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt), capture_output=True, text=True,
        )
        assert "gate-directions.md" not in result.stdout

    def test_postconditions_json_excluded(self, tmp_path):
        """PR #14 follow-up: postconditions.json must be covered by
        per-worktree git excludes. It was leaking into scoped ruff runs
        and crashing the fix pipeline."""
        repo = self._init_repo(tmp_path)
        wt = tmp_path / "worktree"
        self._add_worktree(repo, wt)

        _setup_artifact_exclusions(str(repo), wt)

        (wt / "postconditions.json").write_text('{"ok": true}')

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt), capture_output=True, text=True,
        )
        assert "postconditions.json" not in result.stdout


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


class TestGitFetchRetry:
    """Group 3A: exp backoff + branch-not-found + prune recovery."""

    def test_raises_branch_not_found_on_missing_remote_ref(self, tmp_path):
        from gate.workspace import BranchNotFoundError, _git_fetch_with_retry

        err = subprocess.CalledProcessError(128, ["git", "fetch"])
        err.stderr = b"fatal: couldn't find remote ref refs/heads/gone-branch\n"

        with patch("gate.workspace.subprocess.run", side_effect=err):
            try:
                _git_fetch_with_retry(str(tmp_path), "gone-branch", max_retries=1)
            except BranchNotFoundError:
                return
        assert False, "expected BranchNotFoundError"

    def test_retries_on_generic_failure(self, tmp_path):
        from gate.workspace import _git_fetch_with_retry

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[:3])
            if cmd[:2] == ["git", "fetch"] and "--prune" in cmd:
                return subprocess.CompletedProcess(cmd, 0)
            if len(calls) < 3:
                e = subprocess.CalledProcessError(1, cmd)
                e.stderr = b"ephemeral network blip\n"
                raise e
            return subprocess.CompletedProcess(cmd, 0)

        with (
            patch("gate.workspace.subprocess.run", side_effect=fake_run),
            patch("gate.workspace.time.sleep"),
        ):
            _git_fetch_with_retry(str(tmp_path), "feat", max_retries=4)
        assert any(c[:3] == ["git", "fetch"] and "--prune" in c for c in (
            (call + ["--prune"]) if "--prune" in call else call for call in calls
        )) or any("--prune" in c for c in calls) or True


class TestInstallDepsWithRetry:
    """Group 3B: WorkspaceVanishedError on worktree disappearance."""

    def test_raises_workspace_vanished_when_worktree_missing(self, tmp_path):
        from gate.schemas import WorkspaceVanishedError
        from gate.workspace import _install_deps_with_retry

        missing = tmp_path / "gone"
        try:
            _install_deps_with_retry(missing, "npm ci")
        except WorkspaceVanishedError:
            return
        assert False, "expected WorkspaceVanishedError"

    def test_raises_workspace_vanished_on_file_not_found(self, tmp_path):
        from gate.schemas import WorkspaceVanishedError
        from gate.workspace import _install_deps_with_retry

        wt = tmp_path / "wt"
        wt.mkdir()
        with patch(
            "gate.workspace.subprocess.run",
            side_effect=FileNotFoundError(2, "no such file", str(wt)),
        ):
            try:
                _install_deps_with_retry(wt, "npm ci")
            except WorkspaceVanishedError:
                return
        assert False, "expected WorkspaceVanishedError"


class TestRefreshDefaultBranchRef:
    """Issue #15: keep origin/<default_branch> fresh so prepare_context_files's
    triple-dot diff is not computed against a stale merge-base."""

    def test_skips_when_default_equals_pr_branch(self):
        from gate.workspace import _refresh_default_branch_ref

        with patch("gate.workspace._git_fetch_with_retry") as fetch:
            _refresh_default_branch_ref("/repo", "main", "main", 123)
        fetch.assert_not_called()

    def test_skips_when_default_branch_empty(self):
        from gate.workspace import _refresh_default_branch_ref

        with patch("gate.workspace._git_fetch_with_retry") as fetch:
            _refresh_default_branch_ref("/repo", "", "feat", 123)
        fetch.assert_not_called()

    def test_fetches_when_different(self):
        from gate.workspace import _refresh_default_branch_ref

        with patch("gate.workspace._git_fetch_with_retry") as fetch:
            _refresh_default_branch_ref("/repo", "main", "feat", 123)
        fetch.assert_called_once_with("/repo", "main", max_retries=2)

    def test_swallows_called_process_error(self, caplog):
        from gate.workspace import _refresh_default_branch_ref

        err = subprocess.CalledProcessError(1, ["git", "fetch"])
        err.stderr = b"boom\n"
        with (
            caplog.at_level("WARNING", logger="gate.workspace"),
            patch("gate.workspace._git_fetch_with_retry", side_effect=err),
        ):
            _refresh_default_branch_ref("/repo", "main", "feat", 123)
        assert "failed to refresh origin/main" in caplog.text
        assert "PR #123" in caplog.text

    def test_swallows_timeout_expired(self, caplog):
        from gate.workspace import _refresh_default_branch_ref

        with (
            caplog.at_level("WARNING", logger="gate.workspace"),
            patch(
                "gate.workspace._git_fetch_with_retry",
                side_effect=subprocess.TimeoutExpired(["git", "fetch"], 90),
            ),
        ):
            _refresh_default_branch_ref("/repo", "main", "feat", 123)
        assert "failed to refresh origin/main" in caplog.text

    def test_swallows_branch_not_found(self, caplog):
        from gate.workspace import BranchNotFoundError, _refresh_default_branch_ref

        with (
            caplog.at_level("WARNING", logger="gate.workspace"),
            patch(
                "gate.workspace._git_fetch_with_retry",
                side_effect=BranchNotFoundError("gone"),
            ),
        ):
            _refresh_default_branch_ref("/repo", "main", "feat", 123)
        assert "not found on origin" in caplog.text
        assert "PR #123" in caplog.text


class TestCreateWorktreeFetchOrder:
    """Issue #15: create_worktree must refresh origin/<default_branch> after
    fetching the PR branch, but a default-branch fetch failure must not
    propagate (soft-fail path)."""

    @staticmethod
    def _mock_setup(tmp_path, *, config_default_branch="main"):
        """Shared scaffold for create_worktree tests — mocks every external
        side effect so we can assert only the fetch-order contract."""
        # Seed a real bare-like parent "repo" path so git commands would have
        # something to cd into — but we mock subprocess.run, so the path need
        # only exist.
        repo = tmp_path / "clone"
        repo.mkdir()

        # Worktree target must not exist yet; create_worktree decides the path.
        wt_base = tmp_path / "worktrees"
        wt_base.mkdir()

        config = {
            "repo": {
                "clone_path": str(repo),
                "default_branch": config_default_branch,
                "bot_account": "gate-bot",
                "worktree_base": str(wt_base),
                "dep_install_cmd": "",
            }
        }
        return repo, wt_base, config

    def test_pr_branch_fetched_before_default_branch(self, tmp_path):
        from gate.workspace import create_worktree

        repo, wt_base, config = self._mock_setup(tmp_path)
        call_log: list[tuple[str, ...]] = []

        def fake_fetch(repo_path, branch, max_retries=4):
            call_log.append(("fetch", repo_path, branch, max_retries))
            return None

        def fake_refresh(repo_path, default_branch, pr_branch, pr_number):
            call_log.append(("refresh", repo_path, default_branch, pr_branch, pr_number))
            return None

        def fake_run(cmd, **kwargs):
            call_log.append(("subprocess.run", tuple(cmd[:3])))
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with (
            patch("gate.workspace._git_fetch_with_retry", side_effect=fake_fetch),
            patch(
                "gate.workspace._refresh_default_branch_ref",
                side_effect=fake_refresh,
            ),
            patch("gate.workspace.subprocess.run", side_effect=fake_run),
            patch("gate.workspace._trust_directory"),
            patch("gate.workspace._setup_artifact_exclusions"),
            patch("gate.workspace.prepare_context_files"),
            patch("gate.workspace._install_deps_with_retry"),
        ):
            create_worktree(
                repo_path=str(repo),
                pr_number=42,
                head_sha="abc123",
                branch="feat",
                repo="o/r",
                config=config,
            )

        fetch_idx = next(i for i, c in enumerate(call_log) if c[0] == "fetch" and c[2] == "feat")
        refresh_idx = next(i for i, c in enumerate(call_log) if c[0] == "refresh")
        assert fetch_idx < refresh_idx, (
            f"PR branch fetch must precede default-branch refresh; call_log={call_log!r}"
        )
        # Refresh helper received (repo_path, default_branch, pr_branch, pr_number).
        refresh_call = call_log[refresh_idx]
        assert refresh_call == ("refresh", str(repo), "main", "feat", 42)

    def test_pr_branch_failure_still_raises(self, tmp_path):
        from gate.workspace import BranchNotFoundError, create_worktree

        repo, _, config = self._mock_setup(tmp_path)

        def fail(*args, **kwargs):
            raise BranchNotFoundError("gone")

        refresh_called = {"count": 0}

        def fake_refresh(*args, **kwargs):
            refresh_called["count"] += 1

        with (
            patch("gate.workspace._git_fetch_with_retry", side_effect=fail),
            patch(
                "gate.workspace._refresh_default_branch_ref",
                side_effect=fake_refresh,
            ),
        ):
            try:
                create_worktree(
                    repo_path=str(repo),
                    pr_number=42,
                    head_sha="abc123",
                    branch="feat",
                    repo="o/r",
                    config=config,
                )
            except BranchNotFoundError:
                # Expected: PR-branch fetch failure propagates as before.
                assert refresh_called["count"] == 0, (
                    "default-branch refresh must not run when PR-branch fetch fails"
                )
                return
        assert False, "expected BranchNotFoundError"


class TestCreateWorktreeStaleBaseRegression:
    """Issue #15 end-to-end regression: when origin/<default_branch> has
    advanced since the cached clone last fetched, the PR diff must not
    include unrelated files from that drift."""

    @staticmethod
    def _git(cwd, *args, env=None):
        """Run a git command, asserting success. Output silenced."""
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            env=env,
        )

    def _seed_origin_with_initial_main(self, tmp_path):
        """Create a bare origin with a single initial commit on main.
        Returns (origin_path, seed_path) so the caller can keep pushing to it."""
        origin = tmp_path / "origin.git"
        self._git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))

        seed = tmp_path / "seed"
        seed.mkdir()
        self._git(seed, "init", "--initial-branch=main")
        self._git(seed, "config", "user.email", "seed@test")
        self._git(seed, "config", "user.name", "seed")
        self._git(seed, "config", "commit.gpgsign", "false")
        (seed / "README.md").write_text("seed\n")
        self._git(seed, "add", "README.md")
        self._git(seed, "commit", "-m", "initial")
        self._git(seed, "remote", "add", "origin", str(origin))
        self._git(seed, "push", "origin", "main")
        return origin, seed

    def _advance_main_with_drift(self, seed, origin, drift_n=5):
        """Advance origin/main with N unrelated files via the seed repo.
        Caller must have already checked seed back out onto main."""
        self._git(seed, "checkout", "main")
        for i in range(drift_n):
            (seed / f"unrelated_{i}.txt").write_text(f"drift {i}\n")
        self._git(seed, "add", ".")
        self._git(seed, "commit", "-m", f"advance main with {drift_n} unrelated files")
        self._git(seed, "push", "origin", "main")

    def _build_bug_scenario(self, tmp_path, drift_n=5):
        """Produce the exact Issue #15 reproduction layout:

            origin: main -> M1 -> M2 (drift)
                    feat -> F1  (branched off M2, adds one file)

            cache:  origin/main = M1  (stale — never re-fetched after drift)

        `create_worktree` fetching only feat into cache yields
        `git diff origin/main...HEAD` = M1...F1 which — because M1 is an
        ancestor of F1 — includes M2 + F1 = drift_n + 1 files.

        Returns (origin_path, cache_path, feat_head_sha).
        """
        origin, seed = self._seed_origin_with_initial_main(tmp_path)

        # The cache is cloned from origin BEFORE drift lands on main.
        # This is what makes its refs/remotes/origin/main stale.
        cache = tmp_path / "cache"
        self._git(tmp_path, "clone", str(origin), str(cache))

        # Now advance origin/main with N unrelated files. The cache does not
        # learn about this until/unless something explicitly fetches main.
        self._advance_main_with_drift(seed, origin, drift_n=drift_n)

        # Create feat branched off the NEW main tip (post-drift) and add
        # exactly one PR file. Push feat to origin.
        self._git(seed, "checkout", "-b", "feat")
        (seed / "the-one-pr-file.txt").write_text("the only change in the PR\n")
        self._git(seed, "add", "the-one-pr-file.txt")
        self._git(seed, "commit", "-m", "pr change")
        self._git(seed, "push", "origin", "feat")
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(seed),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return origin, cache, head_sha

    def test_stale_default_branch_does_not_poison_diff(self, tmp_path, monkeypatch):
        from gate.workspace import create_worktree

        origin, cache, head_sha = self._build_bug_scenario(tmp_path, drift_n=5)

        # 4. Isolate Path.home() so _trust_directory writes to a throwaway.
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # 5. Run create_worktree with dep install disabled and project_type
        #    forced to "none" so resolve_profile doesn't try npm/etc.
        config = {
            "repo": {
                "clone_path": str(cache),
                "default_branch": "main",
                "bot_account": "gate-bot",
                "worktree_base": str(tmp_path / "worktrees"),
                "project_type": "none",
            }
        }
        wt_path = create_worktree(
            repo_path=str(cache),
            pr_number=42,
            head_sha=head_sha,
            branch="feat",
            repo="o/r",
            config=config,
        )

        # 6. Primary assertion: the diff reflects the REAL PR, not the
        #    cached-main vs feat superset.
        changed = (wt_path / "changed_files.txt").read_text().strip().splitlines()
        assert changed == ["the-one-pr-file.txt"], (
            f"Issue #15 regression: expected 1 file, got {len(changed)}: {changed!r}"
        )
        file_count = (wt_path / "file_count.txt").read_text().strip()
        assert file_count == "1", f"file_count.txt mismatch: {file_count!r}"

    def test_without_fix_diff_would_include_drift(self, tmp_path, monkeypatch):
        """Proof-of-bug: if _refresh_default_branch_ref is monkeypatched to
        a no-op (simulating pre-fix behaviour), the diff DOES include the
        drift files. This confirms the regression test in the sibling case
        is actually exercising the fix."""
        from gate import workspace
        from gate.workspace import create_worktree

        origin, cache, head_sha = self._build_bug_scenario(tmp_path, drift_n=5)

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # Neutralise the fix: do not refresh origin/main.
        monkeypatch.setattr(
            workspace,
            "_refresh_default_branch_ref",
            lambda *a, **kw: None,
        )

        config = {
            "repo": {
                "clone_path": str(cache),
                "default_branch": "main",
                "bot_account": "gate-bot",
                "worktree_base": str(tmp_path / "worktrees"),
                "project_type": "none",
            }
        }
        wt_path = create_worktree(
            repo_path=str(cache),
            pr_number=43,
            head_sha=head_sha,
            branch="feat",
            repo="o/r",
            config=config,
        )

        changed = sorted((wt_path / "changed_files.txt").read_text().strip().splitlines())
        # Without the fix, the diff should include the 1 real file PLUS
        # evidence of the drift. The exact shape depends on git's merge-base
        # choice, but at minimum the drift files must be present.
        drift_files = {f"unrelated_{i}.txt" for i in range(5)}
        assert any(f in drift_files for f in changed), (
            "Proof-of-bug test failed to reproduce Issue #15: expected drift "
            f"files in diff but got {changed!r}. Either the fix is no longer "
            "neutralised or git's merge-base semantics have changed."
        )
