"""Tests for the sticky review summary comment (Phase 1.1).

Covers:

* Renderer correctness — marker presence, decision label switching,
  blocking vs. non-blocking finding partitioning, missing-summary
  defensiveness.
* Template override mechanism — placing a file in
  ``.gate/templates/sticky.md.j2`` (CWD) takes precedence over the
  package default.
* Upsert flow — first call POSTs (no existing comment), second call
  with same body skips the PATCH (idempotent), second call with
  different body issues a PATCH.
"""

from __future__ import annotations

from unittest.mock import patch

from gate.sticky import (
    STICKY_MARKER,
    _find_sticky_comment,
    render_sticky,
    upsert_sticky_summary,
)


def _verdict(decision: str = "request_changes") -> dict:
    return {
        "decision": decision,
        "confidence": "high",
        "summary": "Two blocking issues found.",
        "review_time_seconds": 47,
        "findings": [
            {
                "severity": "critical",
                "file": "src/auth.py",
                "line": 42,
                "message": "SQL injection in login()",
                "finding_id": "F-1",
                "introduced_by_pr": True,
            },
            {
                "severity": "error",
                "file": "src/api.py",
                "line": 88,
                "message": "Unhandled None in serializer",
                "finding_id": "F-2",
            },
            {
                "severity": "warning",
                "file": "src/utils.py",
                "line": 12,
                "message": "Style: prefer f-string",
            },
        ],
        "stats": {"stages_run": 4, "total_findings": 3},
    }


def _build() -> dict:
    return {
        "typecheck": {"pass": True},
        "lint": {"pass": True},
        "tests": {"pass": False, "exit_code": 1},
    }


# ── Renderer ────────────────────────────────────────────────


class TestRender:
    def test_marker_is_first_line(self):
        out = render_sticky(_verdict(), _build())
        assert out.splitlines()[0] == STICKY_MARKER

    def test_request_changes_label(self):
        out = render_sticky(_verdict("request_changes"), _build())
        assert "Changes requested" in out

    def test_approve_label(self):
        v = _verdict("approve")
        v["findings"] = []
        out = render_sticky(v, _build())
        assert ":white_check_mark:" in out
        assert "Approved" in out

    def test_blocking_findings_split(self):
        out = render_sticky(_verdict(), _build())
        assert "Blocking findings (2)" in out
        assert "Non-blocking notes (1)" in out
        assert "F-1" in out and "F-2" in out
        assert "Style: prefer f-string" in out

    def test_pre_existing_finding_demoted_to_non_blocking(self):
        v = _verdict()
        v["findings"][0]["introduced_by_pr"] = False
        out = render_sticky(v, _build())
        # Critical that's pre-existing should NOT count as blocking.
        assert "Blocking findings (1)" in out

    def test_missing_summary_defensive(self):
        v = _verdict()
        v["summary"] = ""
        out = render_sticky(v, _build())
        assert "_(no summary)_" in out

    def test_no_build_section_when_build_empty(self):
        out = render_sticky(_verdict(), None)
        assert "### Build" not in out

    def test_override_labels_present(self):
        out = render_sticky(_verdict(), _build())
        assert "gate-skip" in out
        assert "gate-rerun" in out
        assert "gate-emergency-bypass" in out
        assert "gate-no-fix" in out


# ── Template override ───────────────────────────────────────


class TestTemplateOverride:
    def test_cwd_override_wins(self, tmp_path, monkeypatch):
        templates = tmp_path / ".gate" / "templates"
        templates.mkdir(parents=True)
        (templates / "sticky.md.j2").write_text(
            "{{ marker }}\n# CUSTOM TEMPLATE for {{ decision_label }}\n"
        )
        monkeypatch.chdir(tmp_path)
        out = render_sticky(_verdict("approve"), _build())
        assert out.splitlines()[0] == STICKY_MARKER
        assert "CUSTOM TEMPLATE for Approved" in out


# ── Sticky lookup + upsert ──────────────────────────────────


class TestFindSticky:
    @patch("gate.sticky._gh", create=True)
    @patch("gate.github._gh")
    def test_find_returns_id_when_marker_present(self, mock_gh, _unused):
        # Two comments — second one is the sticky.
        mock_gh.return_value = (
            '{"id": 100, "body": "regular comment"}\n'
            '{"id": 200, "body": "' + STICKY_MARKER + '\\n## sticky"}\n'
        )
        cid = _find_sticky_comment("o/r", 1)
        assert cid == 200

    @patch("gate.github._gh")
    def test_find_returns_none_when_no_sticky(self, mock_gh):
        mock_gh.return_value = '{"id": 1, "body": "no sticky here"}\n'
        assert _find_sticky_comment("o/r", 1) is None

    @patch("gate.github._gh")
    def test_find_handles_array_response(self, mock_gh):
        # Older `gh` returns the comments array as a single line.
        mock_gh.return_value = (
            '[{"id":7,"body":"' + STICKY_MARKER + '"}]'
        )
        assert _find_sticky_comment("o/r", 1) == 7

    @patch("gate.github._gh")
    def test_find_returns_none_on_gh_error(self, mock_gh):
        import subprocess
        mock_gh.side_effect = subprocess.CalledProcessError(1, "gh")
        assert _find_sticky_comment("o/r", 1) is None


class TestUpsert:
    @patch("gate.github._gh")
    def test_first_invocation_posts(self, mock_gh):
        # Comments list returns no sticky.
        mock_gh.side_effect = [
            "",  # _find_sticky_comment list call
            "",  # POST
        ]
        upsert_sticky_summary("o/r", 1, _verdict(), _build())
        calls = mock_gh.call_args_list
        # Expect 2 _gh calls: list, POST.
        assert len(calls) == 2
        post_args = calls[1].args[0]
        assert "POST" in post_args
        assert "issues/1/comments" in post_args[1]

    @patch("gate.github._gh")
    def test_second_invocation_skips_patch_when_unchanged(self, mock_gh):
        rendered = render_sticky(_verdict(), _build()).strip()
        # 1st gh: list comments (already-existing sticky id=42)
        # 2nd gh: get existing body (returns the rendered text)
        mock_gh.side_effect = [
            f'{{"id": 42, "body": "{STICKY_MARKER}"}}',
            rendered,
        ]
        upsert_sticky_summary("o/r", 1, _verdict(), _build())
        # Only two calls — list + body fetch. NO third PATCH call.
        assert mock_gh.call_count == 2

    @patch("gate.github._gh")
    def test_second_invocation_patches_when_changed(self, mock_gh):
        # 1st gh: list returns a sticky
        # 2nd gh: existing body is something different
        # 3rd gh: PATCH
        mock_gh.side_effect = [
            f'{{"id": 99, "body": "{STICKY_MARKER}"}}',
            f"{STICKY_MARKER}\n## old version\n",
            "",
        ]
        upsert_sticky_summary("o/r", 1, _verdict(), _build())
        calls = mock_gh.call_args_list
        assert mock_gh.call_count == 3
        patch_args = calls[2].args[0]
        assert "PATCH" in patch_args
        assert "issues/comments/99" in patch_args[1]
