"""Durable monitoring inbox and one delivery attempt per Paris calendar day."""
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
        with self.db() as db:
            row = db.execute('SELECT status,message_id,thread_id,error,updated_at FROM deliveries WHERE day=?', (today(),)).fetchone()
            pending = db.execute('SELECT count(*),coalesce(sum(occurrences),0) FROM events WHERE delivered_on IS NULL').fetchone()
            last = db.execute("SELECT day,message_id,updated_at FROM deliveries WHERE status='sent' ORDER BY day DESC LIMIT 1").fetchone()
        return {'enabled': True, 'timezone': 'Europe/Paris', 'today': dict(row) if row else None,
                'last_sent': dict(last) if last else None,
                'pending_events': pending[0], 'pending_occurrences': pending[1]}

    def reserve(self, review):
        day = today()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM deliveries WHERE day=?', (day,)).fetchone()
            if prior:
                return False, dict(prior)
            rows = [dict(r) for r in db.execute('SELECT * FROM events WHERE delivered_on IS NULL ORDER BY id')]
            cutoff = max((r['id'] for r in rows), default=0)
            lines = [f'Universe monitoring review — {day} (Europe/Paris)', '', review, '',
                     f'Queued monitoring notices: {len(rows)} distinct, {sum(r["occurrences"] for r in rows)} occurrences.',
                     'These counts are notices, not distinct incidents or completed repairs.']
            # The ledger owns full evidence; email lists every source/subject and a
            # bounded sample. An oversized queue still cannot grow the email forever.
            for r in rows[-100:]:
                lines += ['', f'{r["subject"]} (x{r["occurrences"]}; last {r["last_seen"]} UTC)', r['body'][:1000]]
            if len(rows) > 100:
                lines += [f'{len(rows)-100} additional notices retained in the monitoring ledger.']
            body = '\n'.join(lines)
            db.execute('INSERT INTO deliveries(day,status,body,cutoff) VALUES(?,?,?,?)',
                       (day, 'reserved', body, cutoff))
            db.executemany('INSERT INTO delivery_items VALUES(?,?,?)',
                           [(day, r['id'], r['occurrences']) for r in rows])
        return True, {'day': day, 'body': body, 'cutoff': cutoff, 'status': 'reserved'}

    def finish(self, reservation, result=None, error=None):
        with self.db() as db:
            if error:
                db.execute("UPDATE deliveries SET status='uncertain',error=?,updated_at=CURRENT_TIMESTAMP WHERE day=?",
                           (error[:1000], reservation['day']))
            else:
                db.execute("UPDATE deliveries SET status='sent',message_id=?,thread_id=?,updated_at=CURRENT_TIMESTAMP WHERE day=?",
                           (result['message_id'], result.get('thread_id'), reservation['day']))
                # Version equality protects even updates within the same second.
                db.execute('''UPDATE events SET delivered_on=? WHERE EXISTS (
                    SELECT 1 FROM delivery_items i WHERE i.day=? AND i.event_id=events.id
                    AND i.occurrences=events.occurrences)''',
                           (reservation['day'], reservation['day']))
