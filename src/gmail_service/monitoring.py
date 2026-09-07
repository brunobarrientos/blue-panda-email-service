"""Durable monitoring assessments and at most one send attempt per Paris day."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from email.utils import getaddresses
from pathlib import Path
from zoneinfo import ZoneInfo

RECIPIENT = 'brunobarrientosf@gmail.com'
SENDER = 'focusedbluepanda@gmail.com'
PREFIXES = ('[universe alert', '[last-mile]', '[chat archive ', '[bridge-probe]')


def today():
    return datetime.now(ZoneInfo('Europe/Paris')).date().isoformat()


def matches(to, cc, bcc, subject):
    recipients = {address.lower() for _, address in getaddresses([s for s in (to, cc, bcc) if s.strip()])}
    return RECIPIENT in recipients and subject.strip().lower().startswith(PREFIXES)


class MonitoringStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, day TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    subject TEXT NOT NULL, body TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, delivered_on TEXT,
                    UNIQUE(day, fingerprint));
                CREATE TABLE IF NOT EXISTS deliveries (
                    day TEXT PRIMARY KEY, status TEXT NOT NULL, body TEXT NOT NULL,
                    cutoff INTEGER NOT NULL, message_id TEXT, thread_id TEXT, error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS delivery_items (
                    day TEXT NOT NULL, event_id INTEGER NOT NULL, occurrences INTEGER NOT NULL,
                    PRIMARY KEY(day,event_id));
                CREATE TABLE IF NOT EXISTS daily_assessments (
                    day TEXT PRIMARY KEY, status TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS assessment_items (
                    day TEXT NOT NULL, event_id INTEGER NOT NULL, occurrences INTEGER NOT NULL,
                    disposition TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(day,event_id,occurrences));
                INSERT OR IGNORE INTO daily_assessments(day,status,updated_at)
                    SELECT day,status,updated_at FROM deliveries;
            ''')
        path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, subject, body):
        # Refuse oversized input; silently truncating would lose incident evidence.
        if len(subject) > 500 or len(body) > 100000:
            raise ValueError('Monitoring message exceeds queue size limit')
        digest = hashlib.sha256(json.dumps([subject, body]).encode()).hexdigest()
        day = today()
        with self.db() as db:
            db.execute('''INSERT INTO events(day,fingerprint,subject,body) VALUES(?,?,?,?)
                ON CONFLICT(day,fingerprint) DO UPDATE SET occurrences=occurrences+1,
                last_seen=CURRENT_TIMESTAMP, delivered_on=NULL''', (day, digest, subject, body))
            row = db.execute('SELECT id FROM events WHERE day=? AND fingerprint=?',
                             (day, digest)).fetchone()
        return {'success': True, 'delivery_status': 'queued', 'queue_id': row['id']}

    def events(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE delivered_on IS NULL ORDER BY id')]

    def status(self):
        day = today()
        with self.db() as db:
            # A single read snapshot keeps delivery and assessment statuses aligned.
            db.execute('BEGIN')
            row = db.execute('SELECT status,message_id,thread_id,error,updated_at FROM deliveries WHERE day=?', (day,)).fetchone()
            pending = db.execute('SELECT count(*),coalesce(sum(occurrences),0) FROM events WHERE delivered_on IS NULL').fetchone()
            last = db.execute("SELECT day,message_id,updated_at FROM deliveries WHERE status='sent' ORDER BY day DESC LIMIT 1").fetchone()
            assessment = db.execute('SELECT day,status,updated_at FROM daily_assessments ORDER BY day DESC LIMIT 1').fetchone()
        return {'enabled': True, 'timezone': 'Europe/Paris', 'today': dict(row) if row else None,
                'last_sent': dict(last) if last else None,
                'last_assessment': dict(assessment) if assessment else None,
                'pending_events': pending[0], 'pending_occurrences': pending[1]}

    @staticmethod
    def _assessment(db, day, status, rows=()):
        db.execute('''INSERT INTO daily_assessments(day,status) VALUES(?,?)
            ON CONFLICT(day) DO UPDATE SET status=excluded.status,updated_at=CURRENT_TIMESTAMP''',
            (day, status))
        db.executemany('''INSERT INTO assessment_items(day,event_id,occurrences,disposition)
            VALUES(?,?,?,?) ON CONFLICT(day,event_id,occurrences)
            DO UPDATE SET disposition=excluded.disposition,updated_at=CURRENT_TIMESTAMP''',
            [(day, row['id'], row['occurrences'], 'available') for row in rows])

    def reserve(self, review, quiet=False):
        day = today()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM deliveries WHERE day=?', (day,)).fetchone()
            if prior:
                # An earlier quiet assessment must never hide a later in-flight,
                # uncertain, or completed delivery. Quiet cannot release its budget.
                self._assessment(db, day, prior['status'])
                return False, dict(prior)
            rows = [dict(r) for r in db.execute('SELECT * FROM events WHERE delivered_on IS NULL ORDER BY id')]
            if quiet:
                self._assessment(db, day, 'quiet', rows)
                # Available evidence is retained; this does not assert that each
                # queued notice was individually reviewed or resolved.
                return False, {'day': day, 'status': 'quiet'}
            cutoff = max((r['id'] for r in rows), default=0)
            # The caller supplies only currently actionable findings. Queue bodies
            # remain internal evidence and never get replayed into this email.
            body = review
            db.execute('INSERT INTO deliveries(day,status,body,cutoff) VALUES(?,?,?,?)',
                       (day, 'reserved', body, cutoff))
            db.executemany('INSERT INTO delivery_items VALUES(?,?,?)',
                           [(day, r['id'], r['occurrences']) for r in rows])
            self._assessment(db, day, 'reserved', rows)
        return True, {'day': day, 'body': body, 'cutoff': cutoff, 'status': 'reserved'}

    def finish(self, reservation, result=None, error=None):
        with self.db() as db:
            status = 'uncertain' if error else 'sent'
            self._assessment(db, reservation['day'], status)
            if error:
                db.execute("UPDATE deliveries SET status='uncertain',error=?,updated_at=CURRENT_TIMESTAMP WHERE day=?",
                           (error[:1000], reservation['day']))
            else:
                db.execute("UPDATE deliveries SET status='sent',message_id=?,thread_id=?,updated_at=CURRENT_TIMESTAMP WHERE day=?",
                           (result['message_id'], result.get('thread_id'), reservation['day']))
                # Delivery proves only the supplied summary body was sent. Queue
                # evidence remains available; no per-event delivery or assessment
                # follows from this summary-level result. Keep delivered_on only
                # for historical records from the previous digest behavior.
