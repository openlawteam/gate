"""Tests for gate.logger module."""

import json
from unittest.mock import patch

from gate.logger import log_review, read_recent_decisions, write_live_log, write_sidecar_meta


class TestLogReview:
    def test_appends_to_jsonl(self, tmp_path):
        logs_dir = tmp_path / "logs"
        jsonl = logs_dir / "reviews.jsonl"

        with patch("gate.logger.LOGS_DIR", logs_dir), patch("gate.logger.REVIEWS_JSONL", jsonl):
            verdict = {
                "decision": "approve",
                "confidence": "high",
                "findings": [],
                "stats": {"total_findings": 0, "stages_run": 4},
            }
            log_review(42, verdict, None, 120)

            assert jsonl.exists()
            entry = json.loads(jsonl.read_text().strip())
            assert entry["pr"] == 42
            assert entry["decision"] == "approve"
            assert entry["review_time_seconds"] == 120
            assert entry["mode"] == "enforcement"

    def test_multiple_entries(self, tmp_path):
        logs_dir = tmp_path / "logs"
        jsonl = logs_dir / "reviews.jsonl"

        with patch("gate.logger.LOGS_DIR", logs_dir), patch("gate.logger.REVIEWS_JSONL", jsonl):
            for i in range(3):
                log_review(
                    i, {"decision": "approve", "findings": [], "stats": {}}, None, 60
                )
            lines = jsonl.read_text().strip().split("\n")
            assert len(lines) == 3

    def test_includes_repo_field(self, tmp_path):
        logs_dir = tmp_path / "logs"
        jsonl = logs_dir / "reviews.jsonl"

        with patch("gate.logger.LOGS_DIR", logs_dir), patch("gate.logger.REVIEWS_JSONL", jsonl):
            log_review(42, {"decision": "approve", "findings": [], "stats": {}}, None, 60, repo="org/repo")
            entry = json.loads(jsonl.read_text().strip())
            assert entry["repo"] == "org/repo"


class TestReadRecentDecisions:
    def test_empty_file(self, tmp_path):
        jsonl = tmp_path / "reviews.jsonl"
        with patch("gate.logger.REVIEWS_JSONL", jsonl):
            assert read_recent_decisions() == []

    def test_reads_last_n(self, tmp_path):
        jsonl = tmp_path / "reviews.jsonl"
        entries = [
            json.dumps({"decision": "approve"}),
            json.dumps({"decision": "error"}),
            json.dumps({"decision": "error"}),
            json.dumps({"decision": "error"}),
        ]
        jsonl.write_text("\n".join(entries) + "\n")

        with patch("gate.logger.REVIEWS_JSONL", jsonl):
            recent = read_recent_decisions(3)
            assert recent == ["error", "error", "error"]


class TestWriteLiveLog:
    def test_creates_log_file(self, tmp_path):
        live_dir = tmp_path / "live"
        with patch("gate.logger.LIVE_DIR", live_dir):
            write_live_log(42, "Triage starting", "stage")
            log_file = live_dir / "pr42.log"
            assert log_file.exists()
            content = log_file.read_text()
            assert "Triage starting" in content
            assert "[stage]" in content


    def test_repo_namespaced_log(self, tmp_path):
        live_dir = tmp_path / "live"
        with patch("gate.logger.LIVE_DIR", live_dir):
            write_live_log(42, "Starting review", "stage", repo="org/repo")
            log_file = live_dir / "org-repo" / "pr42.log"
            assert log_file.exists()
            assert "Starting review" in log_file.read_text()

    def test_no_repo_uses_flat(self, tmp_path):
        live_dir = tmp_path / "live"
        with patch("gate.logger.LIVE_DIR", live_dir):
            write_live_log(42, "Starting review", "stage")
            log_file = live_dir / "pr42.log"
            assert log_file.exists()


class TestWriteSidecarMeta:
    def test_writes_meta_file(self, tmp_path):
        meta = {"stage": "triage", "elapsed_seconds": 30, "model": "sonnet"}
        write_sidecar_meta(tmp_path, "triage", meta)
        path = tmp_path / "triage_meta.json"
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["stage"] == "triage"
