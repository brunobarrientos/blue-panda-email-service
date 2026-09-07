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


def test_daily_body_does_not_replay_resolved_canary_or_obsolete_notices(tmp_path):
    c, gmail = client(tmp_path)
    for subject in ['[Universe Alert] RESOLVED old-outage', '[Universe Alert] alerting canary',
                    '[bridge-probe] verification', '[Universe Alert] FIRING obsolete-outage']:
        c.post('/send', json=event(subject))
    c.post('/monitoring/digest', json={'body': 'ACTION REQUIRED: current disk capacity problem.'})
    body = gmail.sent[0]['body']
    assert 'current disk capacity problem' in body
    assert 'old-outage' not in body
    assert 'canary' not in body
    assert 'obsolete-outage' not in body
    assert 'verification' not in body
    # Evidence is retained internally, with a durable disposition for this digest.
    from gmail_service.monitoring import MonitoringStore
    with MonitoringStore(tmp_path/'monitoring.sqlite3').db() as db:
        assert db.execute('select count(*) from events').fetchone()[0] == 4


def test_quiet_assessment_never_sends_or_reserves_and_survives_restart(tmp_path):
    from gmail_service.monitoring import MonitoringStore, today
    c, gmail = client(tmp_path)
    c.post('/send', json=event('[Universe Alert] RESOLVED historical condition'))
    response = c.post('/monitoring/digest', json={'body': '', 'quiet': True})
    assert response.status_code == 200
    assert response.json()['delivery_status'] == 'quiet'
    assert response.json()['day'] == today()
    assert gmail.sent == []
    c2, _ = client(tmp_path, gmail)
    status = c2.get('/monitoring/status').json()
    assert status['today'] is None
    assert status['last_assessment']['status'] == 'quiet'
    assert status['last_assessment']['day'] == today()
    assert status['last_assessment']['updated_at']
    with MonitoringStore(tmp_path/'monitoring.sqlite3').db() as db:
        assert db.execute('select count(*) from deliveries').fetchone()[0] == 0
        assert db.execute('select count(*) from events').fetchone()[0] == 1


def test_quiet_then_actionable_problem_can_send_once_that_day(tmp_path):
    c, gmail = client(tmp_path)
    assert c.post('/monitoring/digest', json={'body': '', 'quiet': True}).json()['delivery_status'] == 'quiet'
    assert c.post('/monitoring/digest', json={'body': 'Current disk problem'}).json()['delivery_status'] == 'sent'
    assert c.post('/monitoring/digest', json={'body': 'Another problem'}).json()['delivery_status'] == 'already_sent'
    assert len(gmail.sent) == 1
    assert c.get('/monitoring/status').json()['last_assessment']['status'] == 'sent'


def test_uncertain_send_cannot_be_hidden_by_earlier_or_later_quiet(tmp_path):
    c, gmail = client(tmp_path)
    c.post('/monitoring/digest', json={'body': '', 'quiet': True})
    gmail.fail = True
    assert c.post('/monitoring/digest', json={'body': 'Current actionable problem'}).status_code == 502
    assert c.post('/monitoring/digest', json={'body': '', 'quiet': True}).status_code == 409
    c2, _ = client(tmp_path, gmail)
    status = c2.get('/monitoring/status').json()
    assert status['today']['status'] == 'uncertain'
    assert status['last_assessment']['status'] == 'uncertain'
    assert len(gmail.sent) == 1


def test_sent_then_quiet_keeps_delivery_identity_and_sent_assessment(tmp_path):
    c, gmail = client(tmp_path)
    c.post('/monitoring/digest', json={'body': 'Actionable problem'})
    result = c.post('/monitoring/digest', json={'body': '', 'quiet': True}).json()
    assert result['delivery_status'] == 'already_sent'
    assert result['message_id'] == 'gmail-123'
    assert len(gmail.sent) == 1
    assert c.get('/monitoring/status').json()['last_assessment']['status'] == 'sent'


def test_quiet_preserves_sender_preflight_and_never_fakes_success(tmp_path):
    c, gmail = client(tmp_path)
    gmail.get_profile = lambda: {'email_address': 'wrong-account@example.com'}
    assert c.post('/monitoring/digest', json={'body': '', 'quiet': True}).status_code == 503
    assert c.get('/monitoring/status').json()['last_assessment'] is None
    assert gmail.sent == []


def test_pending_reservation_cannot_be_overwritten_by_quiet(tmp_path):
    from gmail_service.monitoring import MonitoringStore
    c, gmail = client(tmp_path)
    store = MonitoringStore(tmp_path/'monitoring.sqlite3')
    store.reserve('Current problem')
    assert c.get('/monitoring/status').json()['last_assessment']['status'] == 'reserved'
    assert c.post('/monitoring/digest', json={'body': '', 'quiet': True}).status_code == 409
    assert c.get('/monitoring/status').json()['last_assessment']['status'] == 'reserved'
    assert not gmail.sent


def test_quiet_versions_retain_new_occurrences_as_unassessed(tmp_path):
    from gmail_service.monitoring import MonitoringStore
    c, _ = client(tmp_path)
    c.post('/send', json=event())
    c.post('/monitoring/digest', json={'body': '', 'quiet': True})
    c.post('/send', json=event())
    with MonitoringStore(tmp_path/'monitoring.sqlite3').db() as db:
        version = db.execute('select occurrences,disposition from assessment_items').fetchone()
        assert tuple(version) == (1, 'available')
        assert db.execute('select occurrences from events').fetchone()[0] == 2
        assert db.execute('select delivered_on from events').fetchone()[0] is None


def test_concurrent_quiet_and_actionable_requests_cannot_hide_sent(tmp_path):
    c, gmail = client(tmp_path)
    payloads = [{'body': '', 'quiet': True}, {'body': 'Actionable current issue'}] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda payload: c.post('/monitoring/digest', json=payload), payloads))
    assert len(gmail.sent) == 1
    status = c.get('/monitoring/status').json()
    assert status['today']['status'] == status['last_assessment']['status'] == 'sent'


def test_quiet_and_send_contract_rejects_contradictory_or_empty_summaries(tmp_path):
    c, gmail = client(tmp_path)
    assert c.post('/monitoring/digest', json={'body': 'An unresolved problem', 'quiet': True}).status_code == 422
    assert c.post('/monitoring/digest', json={'body': ''}).status_code == 422
    assert not gmail.sent
    assert c.get('/monitoring/status').json()['last_assessment'] is None


def test_existing_delivery_ledger_backfills_assessment_on_upgrade(tmp_path):
    from gmail_service.monitoring import MonitoringStore, today
    path = tmp_path/'monitoring.sqlite3'
    store = MonitoringStore(path)
    _, reservation = store.reserve('Current unresolved problem')
    store.finish(reservation, result={'message_id': 'historical-confirmed-id'})
    with store.db() as db:
        db.execute('drop table daily_assessments')
    upgraded = MonitoringStore(path)
    assert upgraded.status()['last_assessment']['status'] == 'sent'
    assert upgraded.status()['last_assessment']['day'] == today()
    acquired, result = upgraded.reserve('', quiet=True)
    assert not acquired
    assert result['status'] == 'sent'
    assert result['message_id'] == 'historical-confirmed-id'


def test_quiet_new_day_updates_assessment_without_touching_previous_budget(tmp_path, monkeypatch):
    from gmail_service import monitoring
    store = monitoring.MonitoringStore(tmp_path/'monitoring.sqlite3')
    monkeypatch.setattr(monitoring, 'today', lambda: '2026-09-06')
    _, reservation = store.reserve('Yesterday problem')
    store.finish(reservation, result={'message_id': 'yesterday-id'})
    monkeypatch.setattr(monitoring, 'today', lambda: '2026-09-07')
    store.reserve('', quiet=True)
    status = store.status()
    assert status['today'] is None
    assert status['last_sent']['message_id'] == 'yesterday-id'
    assert status['last_assessment']['day'] == '2026-09-07'
    assert status['last_assessment']['status'] == 'quiet'
    assert store.reserve('Today new problem')[0]


def test_summary_send_does_not_dispose_unrelated_queued_evidence(tmp_path):
    from gmail_service.monitoring import MonitoringStore
    c, gmail = client(tmp_path)
    c.post('/send', json=event('[Universe Alert] unrelated unassessed storage notice'))
    summary = 'Current verified problem: service heartbeat unavailable.'
    assert c.post('/monitoring/digest', json={'body': summary}).json()['delivery_status'] == 'sent'
    evidence = c.get('/monitoring/events').json()['events']
    assert len(evidence) == 1
    assert evidence[0]['subject'] == '[Universe Alert] unrelated unassessed storage notice'
    assert evidence[0]['delivered_on'] is None
    assert gmail.sent[0]['body'] == summary
    with MonitoringStore(tmp_path/'monitoring.sqlite3').db() as db:
        assert db.execute('select disposition from assessment_items').fetchone()[0] == 'available'
        delivery = db.execute('select body,status,message_id from deliveries').fetchone()
        assert tuple(delivery) == (summary, 'sent', 'gmail-123')
