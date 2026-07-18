"""Regression test for the 2026-07-18 invalid_grant incident.

/health used to only parse local token expiry, so it reported the falsely
reassuring "token_expired_refreshable" for ~5 hours while the refresh token
was actually dead (invalid_grant) and every Gmail-backed endpoint kept
re-hitting Google's token endpoint on every request with no backoff.
"""

from fastapi.testclient import TestClient

from gmail_service.config import Settings
from gmail_service.server import create_app


class _FakeClient:
    def __init__(self):
        self.calls = 0

    def get_profile(self):
        self.calls += 1
        raise RuntimeError("invalid_grant: Token has been expired or revoked.")


def _settings(tmp_path):
    return Settings(
        credentials_path=tmp_path / "missing-credentials.json",
        token_path=tmp_path / "missing-token.json",
    )


def test_health_reports_unhealthy_on_real_check_failure(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        fake = _FakeClient()
        app.state.gmail = fake

        resp = client.get("/health")
        auth = resp.json()["gmail_auth"]

        assert resp.status_code == 200
        assert auth == {
            "authenticated": True,
            "valid": False,
            "reason": "live_check_failed",
            "error": "invalid_grant: Token has been expired or revoked.",
        }
        assert fake.calls == 1


def test_circuit_breaker_short_circuits_after_invalid_grant(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        fake = _FakeClient()
        app.state.gmail = fake

        first = client.get("/profile")
        assert first.status_code == 502
        assert fake.calls == 1

        second = client.get("/profile")
        assert second.status_code == 503
        assert "cooling down" in second.json()["detail"]
        assert fake.calls == 1  # circuit breaker skipped a second real call
