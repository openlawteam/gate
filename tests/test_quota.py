"""Tests for gate.quota module."""

from unittest.mock import patch

from gate.quota import (
    _fail_open,
    _read_cache,
    _write_cache,
    check_quota,
    check_quota_fast,
)


class TestFailOpen:
    def test_returns_quota_ok(self):
        result = _fail_open("test reason")
        assert result["quota_ok"] is True
        assert "fail-open" in result["reason"]


class TestCache:
    def test_write_and_read(self, tmp_path):
        cache_path = tmp_path / "quota-cache.json"
        with patch("gate.quota.QUOTA_CACHE_PATH", cache_path):
            _write_cache({"five_hour": {"utilization": 50}, "seven_day": {"utilization": 30}})
            cached = _read_cache()
            assert cached is not None
            assert cached["five_hour"]["utilization"] == 50

    def test_read_missing(self, tmp_path):
        cache_path = tmp_path / "missing.json"
        with patch("gate.quota.QUOTA_CACHE_PATH", cache_path):
            assert _read_cache() is None


class TestCheckQuota:
    @patch("gate.quota._fetch_usage")
    def test_ok_quota(self, mock_fetch, tmp_path):
        mock_fetch.return_value = {
            "five_hour": {"utilization": 30, "resets_at": "2026-01-01T00:00:00Z"},
            "seven_day": {"utilization": 20},
        }
        cache_path = tmp_path / "quota-cache.json"
        with (
            patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}),
            patch("gate.quota.QUOTA_CACHE_PATH", cache_path),
        ):
            result = check_quota()
            assert result["quota_ok"] is True
            assert result["five_hour_pct"] == 30

    @patch("gate.quota._fetch_usage")
    def test_quota_exceeded(self, mock_fetch, tmp_path):
        mock_fetch.return_value = {
            "five_hour": {"utilization": 90},
            "seven_day": {"utilization": 96},
        }
        cache_path = tmp_path / "quota-cache.json"
        with (
            patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}),
            patch("gate.quota.QUOTA_CACHE_PATH", cache_path),
        ):
            result = check_quota()
            assert result["quota_ok"] is False
            assert "5-hour" in result["reason"]

    def test_no_token_fail_open(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gate.quota.read_keychain_token", return_value=None),
        ):
            result = check_quota()
            assert result["quota_ok"] is True
            assert "fail-open" in result["reason"]

    @patch("gate.quota._fetch_usage", side_effect=Exception("network error"))
    def test_api_error_fail_open(self, mock_fetch, tmp_path):
        cache_path = tmp_path / "missing-cache.json"
        with (
            patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}),
            patch("gate.quota.QUOTA_CACHE_PATH", cache_path),
        ):
            result = check_quota()
            assert result["quota_ok"] is True


class TestCheckQuotaFast:
    @patch("gate.quota._fetch_usage")
    def test_returns_status(self, mock_fetch):
        mock_fetch.return_value = {"five_hour": {"utilization": 50}}
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}):
            result = check_quota_fast()
            assert result is not None
            assert result["exhausted"] is False
            assert result["pct"] == 50

    @patch("gate.quota._fetch_usage")
    def test_exhausted(self, mock_fetch):
        mock_fetch.return_value = {"five_hour": {"utilization": 96}}
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}):
            result = check_quota_fast()
            assert result["exhausted"] is True

    def test_no_token(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gate.quota.read_keychain_token", return_value=None),
        ):
            assert check_quota_fast() is None
