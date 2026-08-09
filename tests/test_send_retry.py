"""Regression tests for the 2026-08-07/08 transient-DNS send failures.

gmail-service had no retry. A single transient name-resolution failure lost the
email outright and surfaced as HTTP 502:

    httplib2.error.ServerNotFoundError: Unable to find the server at
    gmail.googleapis.com

The star journal shows these clustering at boot-time network churn: Docker/k3s
bring up ~15 veth interfaces, tailscaled does a major LinkChange rebind, and
systemd-resolved flushes its caches and drops to a degraded UDP feature set.
Anything sending during that window failed once and was gone. On 2026-08-08 it
took out a real alert digest at 20:10.

Transient network faults must be retried. Genuine 4xx rejections must not be,
because retrying a bad request just wastes the window.
"""

import httplib2
import pytest
from googleapiclient.errors import HttpError

from gmail_service.gmail_client import GmailClient


class _FakeResponse:
    def __init__(self, status):
        self.status = status
        self.reason = 'boom'


class _FakeSend:
    """Stands in for service.users().messages().send(...)."""

    def __init__(self, failures, exc, result=None):
        self._remaining = failures
        self._exc = exc
        self._result = result or {'id': 'msg-1', 'threadId': 'thr-1'}
        self.attempts = 0

    def execute(self):
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        return self._result


class _FakeService:
    def __init__(self, sender):
        self._sender = sender

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, **_kwargs):
        return self._sender


def _client(sender):
    c = GmailClient.__new__(GmailClient)
    c._service = _FakeService(sender)
    return c


def _send(client):
    return client.send_email(to='brunobarrientosf@gmail.com', subject='s', body='b')


DNS_ERROR = httplib2.error.ServerNotFoundError('Unable to find the server at gmail.googleapis.com')


def test_transient_dns_failure_is_retried_and_succeeds(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _s: None)
    sender = _FakeSend(failures=2, exc=DNS_ERROR)
    result = _send(_client(sender))
    assert result['success'] is True
    assert sender.attempts == 3, 'must retry the transient failure, not lose the mail'


def test_persistent_dns_failure_eventually_raises(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _s: None)
    sender = _FakeSend(failures=99, exc=DNS_ERROR)
    with pytest.raises(httplib2.error.ServerNotFoundError):
        _send(_client(sender))
    assert sender.attempts > 1, 'must have actually retried before giving up'


def test_backoff_actually_sleeps_between_attempts(monkeypatch):
    slept = []
    monkeypatch.setattr('time.sleep', lambda s: slept.append(s))
    sender = _FakeSend(failures=2, exc=DNS_ERROR)
    _send(_client(sender))
    assert len(slept) == 2, 'one sleep per retry'
    assert slept == sorted(slept), 'backoff must not shrink'
    assert all(s > 0 for s in slept)


def test_server_side_5xx_is_retried(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _s: None)
    sender = _FakeSend(failures=1, exc=HttpError(_FakeResponse(503), b'unavailable'))
    result = _send(_client(sender))
    assert result['success'] is True
    assert sender.attempts == 2


def test_rate_limit_429_is_retried(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _s: None)
    sender = _FakeSend(failures=1, exc=HttpError(_FakeResponse(429), b'rate limited'))
    assert _send(_client(sender))['success'] is True
    assert sender.attempts == 2


def test_client_error_4xx_is_not_retried(monkeypatch):
    """A malformed or unauthorized request will fail identically every time."""
    monkeypatch.setattr('time.sleep', lambda _s: None)
    sender = _FakeSend(failures=99, exc=HttpError(_FakeResponse(400), b'bad request'))
    with pytest.raises(HttpError):
        _send(_client(sender))
    assert sender.attempts == 1, 'a 4xx must fail fast, not burn the retry budget'


def test_success_on_first_attempt_does_not_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr('time.sleep', lambda s: slept.append(s))
    sender = _FakeSend(failures=0, exc=DNS_ERROR)
    assert _send(_client(sender))['success'] is True
    assert sender.attempts == 1
    assert slept == []
