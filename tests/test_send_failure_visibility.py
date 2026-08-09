"""A send failure must be visible to monitoring, not only in the journal.

Before this, the only trace of a lost email was an ERROR line in
journalctl -u gmail-service. /health reported {"status": "ok"} throughout,
so the blackbox probe that already watches this service could never notice
that mail had stopped going out.
"""

from fastapi.testclient import TestClient

from gmail_service.config import Settings
from gmail_service.server import create_app


class _FailingClient:
    def get_profile(self):
        return {'email_address': 'focusedbluepanda@gmail.com'}

    def send_email(self, **_kwargs):
        raise RuntimeError('Unable to find the server at gmail.googleapis.com')


def _app(tmp_path):
    s = Settings(credentials_path=tmp_path / 'c.json', token_path=tmp_path / 't.json')
    app = create_app(s)
    app.state.gmail = _FailingClient()
    return app


def test_health_reports_last_send_failure(tmp_path):
    client = TestClient(_app(tmp_path))

    assert client.get('/health').json()['last_send_failure'] is None

    client.post('/send', json={'to': 'brunobarrientosf@gmail.com', 'subject': 's', 'body': 'b'})

    failure = client.get('/health').json()['last_send_failure']
    assert failure is not None, 'a failed send must be visible on /health'
    assert 'gmail.googleapis.com' in failure['error']
    assert failure['consecutive'] == 1


def test_consecutive_failures_are_counted(tmp_path):
    client = TestClient(_app(tmp_path))
    for _ in range(3):
        client.post('/send', json={'to': 'b@example.com', 'subject': 's', 'body': 'b'})
    assert client.get('/health').json()['last_send_failure']['consecutive'] == 3
