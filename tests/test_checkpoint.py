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

    def test_finalize_reports_correct_checkpoint_count(
        self, repo, stub_build_ok, monkeypatch,
    ):
        """Regression guard for PR #222 triage confusion.

        The old implementation called ``list_checkpoints`` AFTER the
        soft-reset, which left zero checkpoints reachable from HEAD —
        the progress line always read ``"squashed 0 checkpoints"``.
        We snapshot the count before the reset so operators see the
        real number of sub-scopes that went into the final commit.
        """
        (repo / "a.txt").write_text("1")
        assert checkpoint.cli_main(["save", "--name", "s1"]) == 0
        (repo / "b.txt").write_text("2")
        assert checkpoint.cli_main(["save", "--name", "s2"]) == 0
        (repo / "c.txt").write_text("3")
        assert checkpoint.cli_main(["save", "--name", "s3"]) == 0

        progress: list[str] = []
        monkeypatch.setattr(
            checkpoint,
            "_emit_progress",
            lambda _ws, msg: progress.append(msg),
        )

        assert (
            checkpoint.cli_main(["finalize", "--message", "fix(gate): done"])
            == 0
        )

        finalize_lines = [m for m in progress if "finalized" in m]
        assert finalize_lines, f"no finalize progress message seen in {progress}"
        assert "squashed 3 checkpoints" in finalize_lines[-1]

    def test_finalize_reports_single_checkpoint(
        self, repo, stub_build_ok, monkeypatch,
    ):
        """Count stays accurate for the edge case of exactly one sub-scope."""
        (repo / "a.txt").write_text("1")
        assert checkpoint.cli_main(["save", "--name", "only"]) == 0

        progress: list[str] = []
        monkeypatch.setattr(
            checkpoint,
            "_emit_progress",
            lambda _ws, msg: progress.append(msg),
        )

        assert (
            checkpoint.cli_main(["finalize", "--message", "fix(gate): once"])
            == 0
        )

        finalize_lines = [m for m in progress if "finalized" in m]
        assert finalize_lines
        assert "squashed 1 checkpoints" in finalize_lines[-1]


# ── config threading through scoped_build_verify ─────────────
#
# The architecture review on PR #18 flagged that ``scoped_build_verify``
# was calling ``load_config()`` inside the helper. The fix is to make
# config required and resolve it once at the CLI entry point. These
# tests pin that contract so nobody puts the implicit load back in.


class TestScopedBuildVerifyConfigContract:
    def test_scoped_build_verify_requires_config(self):
        """``config`` is a required positional: calling without it must
        raise ``TypeError`` from Python rather than silently triggering
        an implicit ``load_config()``."""
        import inspect
        sig = inspect.signature(checkpoint.scoped_build_verify)
        cfg_param = sig.parameters["config"]
        # Required (no default) — a reversion to ``config=None`` would
        # re-open the door to implicit config loading inside the helper.
        assert cfg_param.default is inspect.Parameter.empty, (
            "scoped_build_verify(config=) must be required — the CLI "
            "entry point resolves config once and threads it through."
        )

    def test_cmd_save_threads_config_into_scoped_build_verify(
        self, repo, monkeypatch,
    ):
        """_cmd_save should resolve config once and pass it positionally
        to scoped_build_verify — not leave the helper to load it."""
        from gate import config as gate_config
        sentinel = {"repo": {"default_branch": "main"}, "fix_pipeline": {}}
        monkeypatch.setattr(gate_config, "load_config", lambda: sentinel)

        received: list[dict] = []

        def _spy(_w, _touched, cfg):
            received.append(cfg)
            return {
                "pass": True, "typecheck_exit": 0, "typecheck_tail": "",
                "lint_exit": 0, "lint_tail": "", "files": [],
            }

        monkeypatch.setattr(checkpoint, "scoped_build_verify", _spy)

        (repo / "a.txt").write_text("x")
        assert checkpoint.cli_main(["save", "--name", "trivial"]) == 0

        assert received, "scoped_build_verify was never called"
        assert received[0] is sentinel, (
            "_cmd_save must pass the resolved config through to "
            "scoped_build_verify (got a different object)."
        )

    def test_load_config_removed_from_checkpoint_module(self):
        """``_load_config`` was deleted once config threading moved to
        the CLI entry point. Re-adding it is a regression."""
        assert not hasattr(checkpoint, "_load_config"), (
            "checkpoint._load_config should not exist — config resolution "
            "belongs in the CLI entry point (_cmd_save)."
        )


# ── hopper-mode addendum is file-backed ──────────────────────
#
# _HOPPER_MODE_SECTION moved from a hardcoded Python constant to
# prompts/fix-senior-hopper.md. The tests below pin the new shape so
# a revert to the in-module string is obvious.


class TestHopperModeSectionPromptFile:
    def test_fix_senior_hopper_prompt_exists(self):
        from gate import prompt as prompt_mod
        text = prompt_mod.load("fix-senior-hopper")
        assert "HOPPER MODE" in text
        assert "gate checkpoint save" in text
        assert "fix-decomposition.json" in text

    def test_hopper_mode_section_loaded_from_file(self):
        """``_build_hopper_mode_section("hopper")`` must return the
        on-disk prompt, not a Python constant."""
        from gate import prompt as prompt_mod
        expected = prompt_mod.load("fix-senior-hopper")
        assert (
            prompt_mod._build_hopper_mode_section("hopper") == expected
        ), "hopper-mode addendum drifted from prompts/fix-senior-hopper.md"

    def test_hopper_constant_removed_from_prompt_module(self):
        from gate import prompt as prompt_mod
        assert not hasattr(prompt_mod, "_HOPPER_MODE_SECTION"), (
            "_HOPPER_MODE_SECTION should live in "
            "prompts/fix-senior-hopper.md, not as a Python constant."
        )

    def test_polish_legacy_mode_returns_empty(self):
        """Back-compat: flipping ``fix_pipeline.mode`` off returns ''."""
        from gate import prompt as prompt_mod
        assert prompt_mod._build_hopper_mode_section("polish_legacy") == ""
        assert prompt_mod._build_hopper_mode_section("") == ""


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
