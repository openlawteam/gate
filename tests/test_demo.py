"""Acceptance tests for the zero-credential demo and Flight Recorder."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gate.demo import run
from gate.flight_recorder import FlightRecorderScreen, GateDemoTUI, ReviewArchiveDetailScreen
from gate.review_store import LiveReviewDataSource, ReplayReviewDataSource
from gate.state import persist_terminal_review


def test_headless_json_contract(capsys):
    with patch("socket.socket") as socket_mock, patch("subprocess.run") as subprocess_mock:
        assert run(["--headless", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "network_calls": 0,
        "outcome": "blocked",
        "scenario": "risky-auth-refactor",
        "valid": True,
    }
    socket_mock.assert_not_called()
    subprocess_mock.assert_not_called()


def test_demo_cli_modes_and_tui_dispatch(capsys):
    assert run(["--json"]) == 2
    assert "requires --headless" in capsys.readouterr().out

    assert run(["--headless"]) == 0
    assert "PASS: risky-auth-refactor -> blocked" in capsys.readouterr().out

    with patch("gate.flight_recorder.run_demo_tui", return_value=7) as launch:
        assert run([]) == 7
    launch.assert_called_once()


def test_malformed_demo_resources_exit_nonzero(tmp_path, capsys):
    with patch("gate.review_store.resource_path", return_value=tmp_path):
        assert run(["--headless"]) == 1
    assert "malformed demo resources" in capsys.readouterr().err


def test_replay_supports_seek_reset_and_finish():
    source = ReplayReviewDataSource()
    source.step(1)
    assert source.paused is True
    source.reset()
    assert source.complete is False
    source.finish()
    assert source.validation_result()["valid"] is True
    detail = source.detail("demo-gate-pr42")
    assert detail is not None
    assert detail.summary.audit_flags == ("external_check_contradiction",)
    assert detail.findings[0]["evidence_level"] == "test_confirmed"
    assert detail.evidence["lineage"]["parent_review_id"] == "demo-gate-pr42-pre-fix"
    assert [item.phase for item in source.history()] == ["post-fix", "pre-fix"]


def test_replay_speed_change_preserves_event_position():
    source = ReplayReviewDataSource(speed=1)
    source.step(5)
    index = source._index
    source.set_speed(4)
    assert source._index == index
    assert source.speed == 4
    source.toggle_pause()
    source.refresh(now_ms=source._started_ms)
    assert source._index == index


def test_replay_clock_processes_protocol_events_and_bounds_speed():
    source = ReplayReviewDataSource(speed=1)
    source.set_speed(99)
    assert source.speed == 4
    source.refresh(now_ms=source._started_ms + 1_000_000)
    assert source.complete is True
    assert source.dashboard().active == ()
    assert source.dashboard().recent[0].decision == "request_changes"
    assert source.detail("missing-review") is None


@pytest.mark.parametrize(
    ("size", "expected_class"),
    [((90, 34), "narrow"), ((120, 40), "standard"), ((160, 44), None)],
)
async def test_flight_recorder_responsive_layouts(size, expected_class):
    source = ReplayReviewDataSource()
    app = GateDemoTUI(source)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FlightRecorderScreen)
        classes = set(screen.query_one("#recorder-main").classes)
        if expected_class:
            assert expected_class in classes
        else:
            assert "narrow" not in classes
            assert "standard" not in classes
        await pilot.press("end")
        await pilot.pause()
        assert source.validation_result()["valid"] is True
        app.exit()


async def test_narrow_layout_cycles_single_visible_section():
    source = ReplayReviewDataSource()
    source.finish()
    app = GateDemoTUI(source)
    async with app.run_test(size=(90, 34)) as pilot:
        await pilot.pause()
        main = app.screen.query_one("#recorder-main")
        assert "section-reviews" in main.classes
        assert app.screen.query_one("#recorder-reviews").display is True
        assert app.screen.query_one("#recorder-timeline").display is False
        await pilot.press("tab")
        await pilot.pause()
        assert "section-timeline" in main.classes
        assert app.screen.query_one("#recorder-reviews").display is False
        assert app.screen.query_one("#recorder-timeline").display is True
        app.exit()


async def test_flight_recorder_filters_and_opens_full_screen_detail():
    source = ReplayReviewDataSource()
    source.finish()
    app = GateDemoTUI(source)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FlightRecorderScreen)
        assert len(screen._row_ids) == 2
        await pilot.press("p")
        await pilot.pause()
        assert len(screen._row_ids) == 1
        await pilot.press("x")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ReviewArchiveDetailScreen)
        assert "LINEAGE" in str(app.screen.query_one("#detail-identity").render())
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, FlightRecorderScreen)
        app.exit()


def test_live_source_reads_terminal_archives(tmp_path):
    live = tmp_path / "live"
    with (
        patch("gate.state.state_dir", lambda: tmp_path),
        patch("gate.review_store.state_dir", lambda: tmp_path),
        patch("gate.review_store.live_dir", lambda: live),
    ):
        ref = persist_terminal_review(
            9,
            "forksha",
            repo="org/repo",
            outcome="untrusted",
            decision="error",
            summary="fork refused",
        )
        assert ref is not None
        log = live / "org-repo" / "pr9.log"
        log.parent.mkdir(parents=True)
        log.write_text("[12:00:00] [gate] fork refused\n")
        contradiction = ref.path.parent.parent / "contradictions" / "check.json"
        contradiction.parent.mkdir()
        contradiction.write_text(json.dumps({"sha": "forksha", "check": {"name": "tests"}}))
        server = SimpleNamespace(
            reviews=[], review_queue=[{"repo": "org/repo", "pr_number": 10}], health={}
        )
        source = LiveReviewDataSource(server)
        history = source.history()
        assert len(history) == 1
        assert history[0].outcome == "untrusted"
        assert history[0].audit_flags == ("external_check_contradiction",)
        assert source.dashboard().queue[0]["pr_number"] == 10
        detail = source.detail(ref.review_id)
        assert detail is not None
        assert detail.summary.repo == "org/repo"
        assert detail.evidence["contradictions"][0]["check"]["name"] == "tests"
        assert detail.activity == ("[12:00:00] [gate] fork refused",)


def test_live_source_handles_missing_history_and_active_detail(tmp_path):
    with patch("gate.review_store.state_dir", lambda: tmp_path):
        server = SimpleNamespace(
            reviews=[
                {
                    "id": "live-1",
                    "repo": "org/repo",
                    "pr_number": 12,
                    "head_sha": "abc",
                    "status": "fixing",
                    "stage": "fix-senior",
                }
            ],
            review_queue=[],
            health={"github": {"ok": True}},
        )
        source = LiveReviewDataSource(server)
        assert source.history() == []
        source.refresh()
        detail = source.detail("live-1")
        assert detail is not None
        assert detail.summary.phase == "auto-fix"
        assert detail.stages[0].status == "running"
        assert source.detail("missing") is None
