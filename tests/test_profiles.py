"""Tests for gate.profiles module."""

from pathlib import Path

from gate.profiles import PROFILES, detect_project_type, resolve_profile


class TestDetectProjectType:
    def test_detects_node(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert detect_project_type(tmp_path) == "node"

    def test_detects_python_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        assert detect_project_type(tmp_path) == "python"

    def test_detects_python_setup(self, tmp_path):
        (tmp_path / "setup.py").write_text("")
        assert detect_project_type(tmp_path) == "python"

    def test_detects_go(self, tmp_path):
        (tmp_path / "go.mod").write_text("")
        assert detect_project_type(tmp_path) == "go"

    def test_detects_rust(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("")
        assert detect_project_type(tmp_path) == "rust"

    def test_detects_none(self, tmp_path):
        assert detect_project_type(tmp_path) == "none"

    def test_node_takes_priority_over_python(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pyproject.toml").write_text("")
        assert detect_project_type(tmp_path) == "node"


class TestResolveProfile:
    def test_explicit_project_type(self):
        profile = resolve_profile({"project_type": "python"})
        assert profile["language"] == "Python"
        assert profile["lint_cmd"] == "ruff check ."
        assert profile["project_type"] == "python"

    def test_auto_detect_fallback(self, tmp_path):
        (tmp_path / "go.mod").write_text("")
        profile = resolve_profile({}, repo_path=tmp_path)
        assert profile["project_type"] == "go"
        assert profile["language"] == "Go"

    def test_build_overrides(self):
        repo_cfg = {
            "project_type": "python",
            "build": {
                "lint_cmd": "ruff check gate/ tests/",
                "test_cmd": "python -m pytest tests/ -x",
            },
        }
        profile = resolve_profile(repo_cfg)
        assert profile["lint_cmd"] == "ruff check gate/ tests/"
        assert profile["test_cmd"] == "python -m pytest tests/ -x"
        assert profile["typecheck_cmd"] == ""

    def test_unknown_type_falls_back_to_none(self):
        profile = resolve_profile({"project_type": "fortran"})
        assert profile["language"] == "Unknown"
        assert profile["project_type"] == "fortran"

    def test_empty_config_no_path(self):
        profile = resolve_profile({})
        assert profile["project_type"] == ""
        assert profile["language"] == "Unknown"

    def test_all_profiles_have_consistent_keys(self):
        expected_keys = set(PROFILES["node"].keys())
        for name, profile in PROFILES.items():
            assert set(profile.keys()) == expected_keys, f"Profile '{name}' has mismatched keys"
