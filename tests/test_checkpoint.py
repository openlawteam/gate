"""Tests for the hopper-mode ``gate checkpoint`` subcommand + primitives.

We operate inside a real local ``git init`` so ``git add``/``commit`` +
``git reset`` behave exactly as they do in a worktree. Scoped
``build_verify`` is stubbed at module level to keep the test hermetic
(no ``tsc`` / ``eslint`` on disk required).
"""

import os
import subprocess
from pathlib import Path

import pytest

from gate import checkpoint

# ── helpers ──────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd), check=True, capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@e",
        },
    )


def _head(cwd: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Fresh git repo with a baseline commit + ``.gate/pre-fix-sha`` marker."""
    _git("init", "-q", cwd=tmp_path)
    _git("checkout", "-q", "-b", "main", cwd=tmp_path)
    (tmp_path / "README.md").write_text("hi\n")
    # Match production worktree's artifact exclusion so ``.gate/`` baseline
    # markers never show up in ``git add -A``.
    (tmp_path / ".gitignore").write_text(".gate/\n")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-q", "-m", "init", cwd=tmp_path)
    baseline = _head(tmp_path)
    gate_dir = tmp_path / ".gate"
    gate_dir.mkdir()
    (gate_dir / "pre-fix-sha").write_text(baseline + "\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def stub_build_ok(monkeypatch):
    def _ok(_w, _touched, config=None):
        return {
            "pass": True, "typecheck_exit": 0, "typecheck_tail": "",
            "lint_exit": 0, "lint_tail": "", "files": [],
        }
    monkeypatch.setattr(checkpoint, "scoped_build_verify", _ok)


@pytest.fixture
def stub_build_fail(monkeypatch):
    def _fail(_w, _touched, config=None):
        return {
            "pass": False, "typecheck_exit": 2, "typecheck_tail": "TS2304",
            "lint_exit": 0, "lint_tail": "", "files": [],
        }
    monkeypatch.setattr(checkpoint, "scoped_build_verify", _fail)


# ── list_checkpoints ─────────────────────────────────────────


class TestListCheckpoints:
    def test_empty_when_no_checkpoints(self, repo):
        assert checkpoint.list_checkpoints(repo) == []

    def test_lists_checkpoints_newest_first(self, repo):
        (repo / "a.txt").write_text("1")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "gate-checkpoint: scope1", cwd=repo)
        (repo / "b.txt").write_text("2")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "gate-checkpoint: scope2", cwd=repo)

        cps = checkpoint.list_checkpoints(repo)
        assert [c.name for c in cps] == ["scope2", "scope1"]

    def test_stops_at_non_checkpoint_ancestor(self, repo):
        (repo / "a.txt").write_text("1")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "human commit", cwd=repo)
        (repo / "b.txt").write_text("2")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "gate-checkpoint: scope1", cwd=repo)

        cps = checkpoint.list_checkpoints(repo)
        assert [c.name for c in cps] == ["scope1"]


# ── save ─────────────────────────────────────────────────────


class TestSave:
    def test_no_changes_exits_3(self, repo, stub_build_ok):
        rc = checkpoint.cli_main(["save", "--name", "x"])
        assert rc == 3

    def test_success_prints_sha(self, repo, stub_build_ok, capsys):
        (repo / "a.txt").write_text("1")
        rc = checkpoint.cli_main(["save", "--name", "scope1"])
        captured = capsys.readouterr()
        assert rc == 0
        assert len(captured.out.strip()) == 40  # git sha

    def test_build_failure_exits_5_and_keeps_commit(
        self, repo, stub_build_fail, capsys
    ):
        baseline = _head(repo)
        (repo / "a.txt").write_text("1")
        rc = checkpoint.cli_main(["save", "--name", "scope1"])
        captured = capsys.readouterr()
        assert rc == 5
        # Commit still in place so senior can revert or iterate.
        assert _head(repo) != baseline
        assert "TS2304" in captured.err


# ── revert ───────────────────────────────────────────────────


class TestRevert:
    def test_needs_flag(self, repo):
        rc = checkpoint.cli_main(["revert"])
        assert rc == 2

    def test_to_last_clean_drops_only_latest(self, repo, stub_build_ok):
        (repo / "a.txt").write_text("1")
        assert checkpoint.cli_main(["save", "--name", "s1"]) == 0
        after_s1 = _head(repo)
        (repo / "b.txt").write_text("2")
        assert checkpoint.cli_main(["save", "--name", "s2"]) == 0

        assert checkpoint.cli_main(["revert", "--to-last-clean"]) == 0
        assert _head(repo) == after_s1
        assert not (repo / "b.txt").exists()

    def test_to_baseline(self, repo, stub_build_ok):
        baseline = _head(repo)
        (repo / "a.txt").write_text("1")
        assert checkpoint.cli_main(["save", "--name", "s1"]) == 0

        assert checkpoint.cli_main(["revert", "--to-baseline"]) == 0
        assert _head(repo) == baseline


# ── finalize ─────────────────────────────────────────────────


class TestFinalize:
    def test_no_changes_exits_3(self, repo):
        rc = checkpoint.cli_main(["finalize", "--message", "body"])
        assert rc == 3

    def test_squashes_checkpoints_into_one_commit(self, repo, stub_build_ok):
        baseline = _head(repo)
        (repo / "a.txt").write_text("1")
        assert checkpoint.cli_main(["save", "--name", "s1"]) == 0
        (repo / "b.txt").write_text("2")
        assert checkpoint.cli_main(["save", "--name", "s2"]) == 0

        assert (
            checkpoint.cli_main(["finalize", "--message", "fix(gate): done"])
            == 0
        )

        # One commit now sits between baseline and HEAD.
        log = subprocess.run(
            ["git", "log", "--pretty=%H %s", f"{baseline}..HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        assert len(log) == 1
        assert log[0].endswith("fix(gate): done")


# ── _scoped_lint extension filter ────────────────────────────


class TestScopedLintExtensionFilter:
    """Regression guard for PR #14: scoped lint must not feed non-source
    artifacts (``*.codex.log``, ``postconditions.json`` …) to ruff/eslint.
    """

    def _fake_profile(
        self, monkeypatch: pytest.MonkeyPatch, lint_cmd: str
    ) -> list[list[str]]:
        """Stub ``profiles.resolve_profile`` and capture what the runner saw."""
        from gate import profiles

        monkeypatch.setattr(
            profiles, "resolve_profile",
            lambda _cfg, _ws: {"lint_cmd": lint_cmd, "project_type": "python"},
        )
        calls: list[list[str]] = []

        def _fake_run(cmd, cwd=None):
            calls.append(cmd if isinstance(cmd, list) else [cmd])
            return "", 0

        monkeypatch.setattr("gate.fixer._run_silent", _fake_run)
        return calls

    def test_ruff_drops_non_python_files(self, tmp_path, monkeypatch):
        calls = self._fake_profile(monkeypatch, "ruff check")
        files = [
            "gate/codex.py",
            "postconditions.json",
            "audit.codex.log",
            "tests/test_codex.py",
        ]
        exit_code, _ = checkpoint._scoped_lint(tmp_path, {}, files)
        assert exit_code == 0
        assert len(calls) == 1
        assert calls[0] == ["ruff", "check", "gate/codex.py", "tests/test_codex.py"]

    def test_ruff_skips_run_when_no_python_files(self, tmp_path, monkeypatch):
        calls = self._fake_profile(monkeypatch, "ruff check")
        files = ["postconditions.json", "audit.codex.log"]
        exit_code, tail = checkpoint._scoped_lint(tmp_path, {}, files)
        assert exit_code == 0
        assert tail == ""
        assert calls == []

    def test_eslint_drops_non_js_files(self, tmp_path, monkeypatch):
        calls = self._fake_profile(monkeypatch, "eslint .")
        files = ["src/a.ts", "build.json", "src/b.tsx", "junk.log"]
        exit_code, _ = checkpoint._scoped_lint(tmp_path, {}, files)
        assert exit_code == 0
        assert calls[0] == ["eslint", ".", "src/a.ts", "src/b.tsx"]

    def test_unknown_linter_falls_back_to_full_command(self, tmp_path, monkeypatch):
        calls = self._fake_profile(monkeypatch, "mylint --fix")
        exit_code, _ = checkpoint._scoped_lint(
            tmp_path, {}, ["postconditions.json", "a.py"]
        )
        assert exit_code == 0
        # Unknown linter path: full command string passed through, no files appended.
        assert calls[0] == ["mylint --fix"]

    def test_lint_family_recognizes_prefixed_tools(self):
        # Accept things like `./node_modules/.bin/eslint` or `poetry run ruff`.
        assert checkpoint._lint_family("./node_modules/.bin/eslint") == "eslint"
        assert checkpoint._lint_family("ruff") == "ruff"
        assert checkpoint._lint_family("custom-biome-wrapper") == "biome"
        assert checkpoint._lint_family("mylint") is None
