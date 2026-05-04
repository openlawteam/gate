"""Tests for the slash command parser, dispatcher, and audit log (Phase 1.2)."""

from __future__ import annotations

from unittest.mock import patch

from gate import actions as actions_mod
from gate.slash_commands import (
    Command,
    _validate_bypass_link,
    dispatch_command,
    parse_command,
)

# ── Parser ──────────────────────────────────────────────────


class TestParse:
    def test_simple_verb(self):
        cmd = parse_command("/gate rerun")
        assert cmd is not None
        assert cmd.verb == "rerun"
        assert cmd.args == []

    def test_verb_with_args(self):
        cmd = parse_command("/gate skip docs only PR")
        assert cmd is not None
        assert cmd.verb == "skip"
        assert cmd.args == ["docs", "only", "PR"]

    def test_verb_with_quoted_args(self):
        cmd = parse_command('/gate skip "docs only"')
        assert cmd is not None
        assert cmd.args == ["docs only"]

    def test_command_must_be_on_own_line(self):
        # Bare mention in prose doesn't trigger.
        assert parse_command("see /gate status") is None

    def test_command_on_one_of_many_lines(self):
        body = "Hello team\n\n/gate status\n\nplease check"
        cmd = parse_command(body)
        assert cmd is not None
        assert cmd.verb == "status"

    def test_unknown_verb_rejected(self):
        assert parse_command("/gate hack") is None

    def test_no_verb_rejected(self):
        assert parse_command("/gate") is None

    def test_empty_body(self):
        assert parse_command("") is None
        assert parse_command(None) is None  # type: ignore[arg-type]

    def test_first_match_wins(self):
        body = "/gate rerun\n/gate skip later"
        cmd = parse_command(body)
        assert cmd is not None
        assert cmd.verb == "rerun"

    def test_indented_command_still_parsed(self):
        cmd = parse_command("  /gate rerun")
        assert cmd is not None and cmd.verb == "rerun"

    def test_unmatched_quote_swallowed_silently(self):
        # shlex raises on unbalanced quotes — we treat as not-a-command
        # rather than crashing.
        assert parse_command('/gate skip "unbalanced') is None


# ── Bypass link validation ──────────────────────────────────


class TestBypassLink:
    def test_linear_link_accepted(self):
        assert _validate_bypass_link(
            "https://linear.app/myorg/issue/PRO-123", None
        )

    def test_jira_link_accepted(self):
        assert _validate_bypass_link(
            "https://acme.atlassian.net/browse/PROJ-99", None
        )

    def test_pagerduty_link_accepted(self):
        assert _validate_bypass_link(
            "https://acme.pagerduty.com/incidents/Q1ABCDEF", None
        )

    def test_random_url_rejected(self):
        assert not _validate_bypass_link("https://example.com/whatever", None)

    def test_empty_link_rejected(self):
        assert not _validate_bypass_link("", None)

    def test_repo_override_pattern(self):
        repo_cfg = {"bypass_link_patterns": [r"^https://internal\.acme\.com/incidents/"]}
        assert _validate_bypass_link("https://internal.acme.com/incidents/42", repo_cfg)
        # Default Linear no longer accepted because override replaces defaults.
        assert not _validate_bypass_link(
            "https://linear.app/myorg/issue/X", repo_cfg
        )


# ── Dispatch — auth gate ────────────────────────────────────


class TestDispatchAuth:
    def test_open_verbs_pass_without_allowed_commanders(self, tmp_path):
        cmd = Command(
            verb="status", args=[], pr_number=1, repo="o/r",
            commenter="anyone",
        )
        with patch("gate.slash_commands.gate_client.list_reviews", return_value=[]):
            with patch("gate.slash_commands.gate_client.list_queue", return_value=[]):
                outcome, reply = dispatch_command(
                    cmd, socket_path=tmp_path / "fake.sock", repo_config={}
                )
        assert outcome == "ok"
        assert "PR #1" in reply

    def test_mutating_verb_rejected_when_no_commanders_configured(self, tmp_path):
        cmd = Command(
            verb="rerun", args=[], pr_number=1, repo="o/r",
            commenter="alice",
        )
        outcome, reply = dispatch_command(
            cmd, socket_path=tmp_path / "fake.sock", repo_config={}
        )
        assert outcome == "rejected"
        assert "allowed_commanders" in reply

    def test_mutating_verb_rejected_when_commenter_not_in_list(self, tmp_path):
        cmd = Command(
            verb="rerun", args=[], pr_number=1, repo="o/r",
            commenter="mallory",
        )
        repo_cfg = {"allowed_commanders": ["alice", "bob"]}
        outcome, reply = dispatch_command(
            cmd, socket_path=tmp_path / "fake.sock", repo_config=repo_cfg
        )
        assert outcome == "rejected"
        assert "not authorised" in reply

    def test_authorised_commenter_proceeds(self, tmp_path):
        cmd = Command(
            verb="rerun", args=[], pr_number=1, repo="o/r",
            commenter="alice",
        )
        with patch(
            "gate.slash_commands.gate_client.send_message", return_value=None
        ):
            outcome, reply = dispatch_command(
                cmd,
                socket_path=tmp_path / "fake.sock",
                repo_config={"allowed_commanders": ["alice"]},
            )
        assert outcome == "ok"
        assert "Re-running" in reply


# ── Dispatch — verb-specific behaviour ──────────────────────


class TestDispatchVerbs:
    def test_skip_requires_reason(self, tmp_path):
        cmd = Command(
            verb="skip", args=[], pr_number=1, repo="o/r",
            commenter="alice",
        )
        outcome, reply = dispatch_command(
            cmd,
            socket_path=tmp_path / "fake.sock",
            repo_config={"allowed_commanders": ["alice"]},
        )
        assert outcome == "rejected"
        assert "reason" in reply

    def test_skip_adds_label_when_authorised(self, tmp_path):
        cmd = Command(
            verb="skip", args=["docs", "only"], pr_number=1, repo="o/r",
            commenter="alice",
        )
        with patch("gate.slash_commands._add_label") as mock_label:
            outcome, reply = dispatch_command(
                cmd,
                socket_path=tmp_path / "fake.sock",
                repo_config={"allowed_commanders": ["alice"]},
            )
        assert outcome == "ok"
        mock_label.assert_called_once_with("o/r", 1, "gate-skip")
        assert "_docs only_" in reply

    def test_bypass_requires_link(self, tmp_path):
        cmd = Command(
            verb="bypass", args=[], pr_number=1, repo="o/r",
            commenter="alice",
        )
        outcome, _ = dispatch_command(
            cmd,
            socket_path=tmp_path / "fake.sock",
            repo_config={"allowed_commanders": ["alice"]},
        )
        assert outcome == "rejected"

    def test_bypass_rejects_random_link(self, tmp_path):
        cmd = Command(
            verb="bypass", args=["https://example.com/incident/1"],
            pr_number=1, repo="o/r", commenter="alice",
        )
        outcome, reply = dispatch_command(
            cmd,
            socket_path=tmp_path / "fake.sock",
            repo_config={"allowed_commanders": ["alice"]},
        )
        assert outcome == "rejected"
        assert "incident-tracker" in reply

    def test_bypass_with_linear_link_succeeds(self, tmp_path):
        cmd = Command(
            verb="bypass",
            args=["https://linear.app/myorg/issue/PRO-123"],
            pr_number=1, repo="o/r", commenter="alice",
        )
        with patch("gate.slash_commands._add_label") as mock_label:
            outcome, reply = dispatch_command(
                cmd,
                socket_path=tmp_path / "fake.sock",
                repo_config={"allowed_commanders": ["alice"]},
            )
        assert outcome == "ok"
        mock_label.assert_called_once_with("o/r", 1, "gate-emergency-bypass")
        assert "Bypass recorded" in reply


# ── Action audit log ────────────────────────────────────────


class TestActionsLog:
    def test_record_action_writes_line(self):
        actions_mod.record_action(
            verb="skip", actor="alice", repo="o/r", pr_number=1,
            args={"reason": "docs"}, outcome="ok",
        )
        rows = list(actions_mod.read_actions())
        assert any(r["verb"] == "skip" and r["actor"] == "alice" for r in rows)

    def test_record_action_filters_unrelated_verbs(self):
        actions_mod.record_action(verb="skip", actor="a", repo="o/r", pr_number=1)
        actions_mod.record_action(verb="rerun", actor="b", repo="o/r", pr_number=2)
        skips = list(actions_mod.read_actions(verb="skip"))
        reruns = list(actions_mod.read_actions(verb="rerun"))
        assert len(skips) == 1 and skips[0]["actor"] == "a"
        assert len(reruns) == 1 and reruns[0]["actor"] == "b"

    def test_dispatch_records_authorised_action(self, tmp_path):
        cmd = Command(
            verb="skip", args=["docs"], pr_number=1, repo="o/r",
            commenter="alice",
        )
        with patch("gate.slash_commands._add_label"):
            dispatch_command(
                cmd,
                socket_path=tmp_path / "fake.sock",
                repo_config={"allowed_commanders": ["alice"]},
            )
        rows = list(actions_mod.read_actions(verb="skip"))
        assert any(r["actor"] == "alice" and r["outcome"] == "ok" for r in rows)

    def test_dispatch_records_rejected_action(self, tmp_path):
        cmd = Command(
            verb="skip", args=["docs"], pr_number=1, repo="o/r",
            commenter="mallory",
        )
        dispatch_command(
            cmd,
            socket_path=tmp_path / "fake.sock",
            repo_config={"allowed_commanders": ["alice"]},
        )
        rows = list(actions_mod.read_actions(verb="skip"))
        assert any(r["outcome"] == "rejected" for r in rows)
