"""Tests for gate.profiles module."""


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


class TestVerifyCmdField:
    """Phase 6: verify_cmd must exist on every profile as an empty
    string default. Absent-means-disabled is the contract the logic
    prompt's proof-verification section relies on."""

    def test_every_profile_declares_verify_cmd(self):
        for name, profile in PROFILES.items():
            assert "verify_cmd" in profile, f"{name} missing verify_cmd"
            assert profile["verify_cmd"] == "", f"{name} must default to empty"

    def test_resolve_exposes_verify_cmd(self):
        profile = resolve_profile({"project_type": "rust"})
        assert profile["verify_cmd"] == ""

    def test_build_override_populates_verify_cmd(self):
        repo_cfg = {
            "project_type": "rust",
            "build": {"verify_cmd": "verus verify lib.rs"},
        }
        profile = resolve_profile(repo_cfg)
        assert profile["verify_cmd"] == "verus verify lib.rs"


class TestBuildVarsExposesVerifyCmd:
    """prompt.build_vars must surface verify_cmd for prompt templates."""

    def test_default_empty(self, tmp_path):
        from gate import prompt as prompt_mod

        (tmp_path / "diff.txt").write_text("")
        (tmp_path / "changed_files.txt").write_text("")
        (tmp_path / "pr-metadata.json").write_text(
            '{"pr_title":"t","pr_body":"b","pr_author":"a"}'
        )
        vars_ = prompt_mod.build_vars(
            tmp_path, "logic",
            {"pr_title": "t", "pr_body": "b", "pr_author": "a"},
            {"repo": {"project_type": "node"}},
        )
        assert vars_["verify_cmd"] == ""

    def test_override_surfaces(self, tmp_path):
        from gate import prompt as prompt_mod

        (tmp_path / "diff.txt").write_text("")
        (tmp_path / "changed_files.txt").write_text("")
        (tmp_path / "pr-metadata.json").write_text(
            '{"pr_title":"t","pr_body":"b","pr_author":"a"}'
        )
        vars_ = prompt_mod.build_vars(
            tmp_path, "logic",
            {"pr_title": "t", "pr_body": "b", "pr_author": "a"},
            {
                "repo": {
                    "project_type": "rust",
                    "build": {"verify_cmd": "verus verify {FILE}"},
                },
            },
        )
        assert vars_["verify_cmd"] == "verus verify {FILE}"


class TestPromptAnchorsForProofConfirmed:
    """Ensure the prompt templates advertise the proof_confirmed tier."""

    def _read(self, name):
        from gate.prompt import load
        return load(name)

    def test_logic_lists_proof_confirmed_enum(self):
        text = self._read("logic")
        assert "proof_confirmed" in text

    def test_verdict_lists_proof_confirmed_enum(self):
        text = self._read("verdict")
        assert "proof_confirmed" in text

    def test_logic_has_proof_section_gated_on_verify_cmd(self):
        text = self._read("logic")
        assert "$verify_cmd" in text
        assert "Proof-Based Verification" in text

    def test_verdict_exempts_proof_confirmed_from_mutation_downgrade(self):
        text = self._read("verdict")
        # Exemption clause must mention the tier name and the word "exempt"
        # so the rule can't silently regress.
        assert "proof_confirmed" in text.lower()
        assert "exempt" in text.lower() or "never be downgraded" in text.lower()
