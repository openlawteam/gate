"""Tests for per-author / per-event / per-repo notification routing (Phase 1.3)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gate.notify import Target, route_for


@pytest.fixture
def routes_file(tmp_path, monkeypatch):
    """Point the routes loader at a temp TOML and return a writer."""
    from gate import config as config_mod

    # The autouse ``isolate_paths`` conftest fixture already creates
    # ``tmp_path / "data"`` and points GATE_DATA_DIR at it; reuse that
    # directory rather than racing it (mkdir without exist_ok would
    # raise FileExistsError).
    fake_data = tmp_path / "data"
    fake_data.mkdir(exist_ok=True)
    monkeypatch.setattr(config_mod, "GATE_DATA_DIR", fake_data)
    target = fake_data / "notification_routes.toml"

    def write(text: str) -> None:
        target.write_text(text)

    yield write


# ── Env defaults ────────────────────────────────────────────


class TestEnvDefaults:
    def test_no_env_no_routes_no_targets(self, routes_file, monkeypatch):
        monkeypatch.delenv("GATE_NTFY_TOPIC", raising=False)
        monkeypatch.delenv("GATE_DISCORD_WEBHOOK", raising=False)
        assert route_for("review_complete") == []

    def test_ntfy_env_only(self, routes_file, monkeypatch):
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops-channel")
        targets = route_for("review_complete")
        assert targets == [Target(kind="ntfy", endpoint="ops-channel")]

    def test_discord_env_only(self, routes_file, monkeypatch):
        monkeypatch.delenv("GATE_NTFY_TOPIC", raising=False)
        monkeypatch.setenv(
            "GATE_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1/x"
        )
        targets = route_for("review_complete")
        assert targets == [
            Target(kind="discord", endpoint="https://discord.com/api/webhooks/1/x")
        ]

    def test_both_env_vars_emit_two_targets(self, routes_file, monkeypatch):
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops")
        monkeypatch.setenv("GATE_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1/x")
        targets = route_for("review_complete")
        kinds = sorted(t.kind for t in targets)
        assert kinds == ["discord", "ntfy"]


# ── Per-author override ─────────────────────────────────────


class TestAuthorOverride:
    def test_author_ntfy_added_to_env_default(self, routes_file, monkeypatch):
        routes_file('[authors.alice]\nntfy = "alice-prs"\n')
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops")
        targets = route_for("review_complete", author="alice")
        topics = [t.endpoint for t in targets if t.kind == "ntfy"]
        assert "ops" in topics
        assert "alice-prs" in topics

    def test_unknown_author_falls_back_to_default(self, routes_file, monkeypatch):
        routes_file('[authors.alice]\nntfy = "alice-prs"\n')
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops")
        targets = route_for("review_complete", author="bob")
        topics = [t.endpoint for t in targets if t.kind == "ntfy"]
        assert topics == ["ops"]

    def test_no_author_uses_only_defaults(self, routes_file, monkeypatch):
        routes_file('[authors.alice]\nntfy = "alice-prs"\n')
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops")
        targets = route_for("review_complete")
        topics = [t.endpoint for t in targets if t.kind == "ntfy"]
        assert topics == ["ops"]

    def test_author_discord_webhook(self, routes_file, monkeypatch):
        routes_file(
            '[authors.alice]\n'
            'discord = "https://discord.com/api/webhooks/2/y"\n'
        )
        monkeypatch.delenv("GATE_NTFY_TOPIC", raising=False)
        monkeypatch.delenv("GATE_DISCORD_WEBHOOK", raising=False)
        targets = route_for("review_complete", author="alice")
        assert targets == [
            Target(kind="discord", endpoint="https://discord.com/api/webhooks/2/y")
        ]


# ── Per-event extras ────────────────────────────────────────


class TestEventExtras:
    def test_event_extras_only_apply_to_named_event(self, routes_file, monkeypatch):
        routes_file(
            '[events.fix_failed]\n'
            'extra_ntfy = ["on-call"]\n'
        )
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops")
        review_targets = route_for("review_complete")
        fix_targets = route_for("fix_failed")
        review_topics = [t.endpoint for t in review_targets if t.kind == "ntfy"]
        fix_topics = [t.endpoint for t in fix_targets if t.kind == "ntfy"]
        assert review_topics == ["ops"]
        assert "on-call" in fix_topics
        assert "ops" in fix_topics


# ── Per-repo extras ─────────────────────────────────────────


class TestRepoExtras:
    def test_repo_extras_only_apply_to_named_repo(self, routes_file, monkeypatch):
        routes_file(
            '[repos."myorg/myrepo"]\n'
            'extra_ntfy = ["myrepo-channel"]\n'
        )
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops")
        a_targets = route_for("review_complete", repo="myorg/myrepo")
        b_targets = route_for("review_complete", repo="other/repo")
        a_topics = [t.endpoint for t in a_targets if t.kind == "ntfy"]
        b_topics = [t.endpoint for t in b_targets if t.kind == "ntfy"]
        assert "myrepo-channel" in a_topics
        assert "myrepo-channel" not in b_topics


# ── Dedup ───────────────────────────────────────────────────


class TestDedup:
    def test_same_target_listed_twice_is_emitted_once(self, routes_file, monkeypatch):
        # Author override duplicates the env default.
        routes_file('[authors.alice]\nntfy = "ops"\n')
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops")
        targets = route_for("review_complete", author="alice")
        assert len(targets) == 1
        assert targets[0] == Target(kind="ntfy", endpoint="ops")


# ── Malformed config tolerance ──────────────────────────────


class TestMalformedConfig:
    def test_invalid_toml_falls_back_to_env(self, routes_file, monkeypatch):
        routes_file("this is not valid toml [[")
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops")
        targets = route_for("review_complete")
        assert targets == [Target(kind="ntfy", endpoint="ops")]

    def test_missing_routes_file_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("GATE_NTFY_TOPIC", "ops")
        targets = route_for("review_complete")
        assert targets == [Target(kind="ntfy", endpoint="ops")]


# ── Convenience wrappers honour author kwarg ────────────────


class TestWrapperAuthor:
    @patch("gate.notify._send_to")
    def test_review_complete_passes_author_through(
        self, mock_send, routes_file, monkeypatch
    ):
        routes_file('[authors.alice]\nntfy = "alice-prs"\n')
        monkeypatch.delenv("GATE_NTFY_TOPIC", raising=False)
        monkeypatch.delenv("GATE_DISCORD_WEBHOOK", raising=False)

        from gate.notify import review_complete

        review_complete(
            42, {"decision": "approve", "summary": "OK", "stats": {}},
            repo="myorg/myrepo", author="alice",
        )
        endpoints = [
            call.args[0].endpoint for call in mock_send.call_args_list
        ]
        assert "alice-prs" in endpoints
