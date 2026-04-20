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
        with patch("gate.quota.quota_cache_path", lambda: cache_path):
            _write_cache({"five_hour": {"utilization": 50}, "seven_day": {"utilization": 30}})
            cached = _read_cache()
            assert cached is not None
            assert cached["five_hour"]["utilization"] == 50

    def test_read_missing(self, tmp_path):
        cache_path = tmp_path / "missing.json"
        with patch("gate.quota.quota_cache_path", lambda: cache_path):
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
            patch("gate.quota.quota_cache_path", lambda: cache_path),
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
            patch("gate.quota.quota_cache_path", lambda: cache_path),
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
            patch("gate.quota.quota_cache_path", lambda: cache_path),
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


class TestAuthDrift:
    """Group 4A: 401/403 on usage API must surface as auth_drift=True
    and fire (at most) one ntfy alert per 24h window."""

    def test_http_401_raises_auth_drift_error(self, tmp_path):
        import urllib.error

        from gate.quota import QuotaAuthDriftError, _fetch_usage

        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with patch("gate.quota.urllib.request.urlopen", side_effect=err):
            try:
                _fetch_usage("bad-token")
            except QuotaAuthDriftError:
                pass
            else:
                assert False, "expected QuotaAuthDriftError"

    def test_http_500_does_not_raise_auth_drift(self, tmp_path):
        import urllib.error

        from gate.quota import QuotaAuthDriftError, _fetch_usage

        err = urllib.error.HTTPError("u", 500, "ise", {}, None)
        with patch("gate.quota.urllib.request.urlopen", side_effect=err):
            try:
                _fetch_usage("token")
            except QuotaAuthDriftError:
                assert False, "500 must be treated as transient, not drift"
            except urllib.error.HTTPError:
                pass

    def test_check_quota_marks_auth_drift_and_fires_alert_once(self, tmp_path):
        from gate import notify
        from gate.quota import QuotaAuthDriftError, check_quota

        cache = tmp_path / "quota-cache.json"
        marker = tmp_path / "quota-auth-drift-alerted.txt"
        with (
            patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "bad"}),
            patch("gate.quota.quota_cache_path", lambda: cache),
            patch("gate.quota._auth_drift_marker_path", lambda: marker),
            patch("gate.quota._fetch_usage", side_effect=QuotaAuthDriftError("401")),
            patch.object(notify, "quota_auth_drift") as mock_alert,
        ):
            r1 = check_quota()
            check_quota()
            assert r1["auth_drift"] is True
            assert r1["quota_ok"] is True
            assert mock_alert.call_count == 1

    def test_successful_auth_clears_drift_marker(self, tmp_path):
        from gate.quota import check_quota

        cache = tmp_path / "quota-cache.json"
        marker = tmp_path / "quota-auth-drift-alerted.txt"
        marker.write_text("123.0")
        with (
            patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "good"}),
            patch("gate.quota.quota_cache_path", lambda: cache),
            patch("gate.quota._auth_drift_marker_path", lambda: marker),
            patch(
                "gate.quota._fetch_usage",
                return_value={
                    "five_hour": {"utilization": 10},
                    "seven_day": {"utilization": 5},
                },
            ),
        ):
            result = check_quota()
            assert result["quota_ok"] is True
            assert not marker.exists(), "marker must be cleared on successful auth"
