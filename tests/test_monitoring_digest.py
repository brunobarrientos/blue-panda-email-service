from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from gmail_service.config import Settings
from gmail_service.server import create_app


class Gmail:
    def __init__(self):
        self.sent = []
        self.fail = False

    def get_profile(self):
        return {'email_address': 'focusedbluepanda@gmail.com'}

    def send_email(self, **kwargs):
        self.sent.append(kwargs)
        if self.fail:
            raise TimeoutError('ambiguous transport result')
        return {'success': True, 'message_id': 'gmail-123', 'thread_id': 'thread-123'}


def client(tmp_path, gmail=None, host='127.0.0.1'):
    app = create_app(Settings(token_path=tmp_path/'token.json', monitoring_enabled=True))
    app.state.gmail = gmail or Gmail()
    return TestClient(app, client=(host, 12345)), app.state.gmail


def event(subject='[Universe Alert] test'):
    return {'to': 'brunobarrientosf@gmail.com', 'subject': subject, 'body': 'test alert'}


def test_all_observed_monitoring_senders_queue_without_sending(tmp_path):
    c, gmail = client(tmp_path)
    for s in ['[Universe Alert] digest', '[Universe Alert: star] digest',
              '[Universe Alert: netcup] digest', '[Universe Alert] alerting canary',
              '[last-mile] 5 unlanded conditions', '[CHAT ARCHIVE RED] failure',
              '[bridge-probe] direct send test']:
        r = c.post('/send', json=event(s))
        assert r.status_code == 200
        assert r.json()['delivery_status'] == 'queued'
        assert r.json()['message_id'] is None
    assert gmail.sent == []
    assert c.get('/monitoring/status').json()['pending_events'] == 7


def test_normal_mail_still_sends(tmp_path):
    c, gmail = client(tmp_path)
    assert c.post('/send', json=event('Requested report')).json()['message_id'] == 'gmail-123'
    assert len(gmail.sent) == 1


def test_queue_survives_restart_and_collapses_exact_repeats(tmp_path):
    c, gmail = client(tmp_path)
    for _ in range(4):
        c.post('/send', json=event())
    c2, _ = client(tmp_path)
    status = c2.get('/monitoring/status').json()
    assert status['pending_events'] == 1
    assert status['pending_occurrences'] == 4


def test_concurrent_daily_send_is_once_and_survives_restart(tmp_path):
    c, gmail = client(tmp_path)
    c.post('/send', json=event())
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: c.post('/monitoring/digest', json={'body': 'AI review'}), range(6)))
    assert len(gmail.sent) == 1
    assert gmail.sent[0]['max_attempts'] == 1
    c2, _ = client(tmp_path, gmail)
    c2.post('/monitoring/digest', json={'body': 'second attempt'})
    assert len(gmail.sent) == 1
    assert c2.get('/monitoring/status').json()['today']['status'] == 'sent'


def test_ambiguous_send_is_not_retried_or_reported_sent(tmp_path):
    c, gmail = client(tmp_path)
    gmail.fail = True
    c.post('/send', json=event())
    assert c.post('/monitoring/digest', json={'body': 'review'}).status_code == 502
    assert c.post('/monitoring/digest', json={'body': 'review'}).status_code == 409
    assert len(gmail.sent) == 1
    assert c.get('/monitoring/status').json()['today']['status'] == 'uncertain'
    assert c.get('/monitoring/status').json()['pending_events'] == 1


def test_remote_cannot_trigger_digest_or_read_queue(tmp_path):
    c, gmail = client(tmp_path, host='100.1.2.3')
    assert c.post('/monitoring/digest', json={'body': 'review'}).status_code == 403
    assert c.get('/monitoring/events').status_code == 403
    assert not gmail.sent


def test_queue_works_while_gmail_is_unavailable(tmp_path):
    c, _ = client(tmp_path)
    c.app.state.gmail = None
    assert c.post('/send', json=event()).json()['delivery_status'] == 'queued'
    assert c.post('/monitoring/digest', json={'body': 'review'}).status_code == 503
    assert c.get('/monitoring/status').json()['today'] is None


def test_monitoring_cannot_bypass_gate_through_cc(tmp_path):
    c, gmail = client(tmp_path)
    data = event()
    data.update(to='someone@example.com', cc='brunobarrientosf@gmail.com')
    assert c.post('/send', json=data).status_code == 422
    assert not gmail.sent


def test_new_occurrence_during_send_is_retained(tmp_path):
    from gmail_service.monitoring import MonitoringStore
    s = MonitoringStore(tmp_path/'queue.sqlite3')
    s.enqueue('alert', 'body')
    _, reservation = s.reserve('review')
    s.enqueue('alert', 'body')
    s.finish(reservation, result={'message_id': 'sent'})
    assert s.status()['pending_events'] == 1
    assert s.events()[0]['occurrences'] == 2


def test_new_paris_day_has_new_budget(tmp_path, monkeypatch):
    from gmail_service import monitoring
    s = monitoring.MonitoringStore(tmp_path/'queue.sqlite3')
    monkeypatch.setattr(monitoring, 'today', lambda: '2026-09-06')
    ok, first = s.reserve('day one')
    assert ok
    s.finish(first, result={'message_id': 'sent'})
    assert not s.reserve('repeat')[0]
    monkeypatch.setattr(monitoring, 'today', lambda: '2026-09-07')
    assert s.reserve('day two')[0]


def test_enqueue_captures_date_once_at_midnight(tmp_path, monkeypatch):
    from gmail_service import monitoring
    dates = iter(['2026-09-06', '2026-09-07'])
    monkeypatch.setattr(monitoring, 'today', lambda: next(dates))
    s = monitoring.MonitoringStore(tmp_path/'queue.sqlite3')
    assert s.enqueue('alert', 'body')['queue_id'] == 1
    assert s.events()[0]['day'] == '2026-09-06'
