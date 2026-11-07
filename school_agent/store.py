"""SQLite state: sources, observations (what each source says), obligations (reconciled truth), review queue."""
import json
import sqlite3
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sources (
    source_key TEXT PRIMARY KEY, course TEXT, title TEXT, url TEXT,
    fingerprint TEXT NOT NULL, extracted_fp TEXT, contents TEXT NOT NULL,
    source_updated TEXT, first_seen TEXT, scanned_at TEXT NOT NULL, last_seen_scan INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS observations (
    source_key TEXT NOT NULL, item_key TEXT NOT NULL, course TEXT, kind TEXT, title TEXT,
    date TEXT, time TEXT, status TEXT, prev_date TEXT, confidence REAL, evidence TEXT,
    url TEXT, structured INTEGER DEFAULT 0, PRIMARY KEY (source_key, item_key)
);
CREATE TABLE IF NOT EXISTS obligations (
    oid TEXT PRIMARY KEY, course TEXT, kind TEXT, title TEXT, date TEXT, time TEXT,
    status TEXT NOT NULL DEFAULT 'active', members TEXT NOT NULL, evidence TEXT, urls TEXT,
    notes TEXT, desired_fp TEXT, synced_fp TEXT, todoist_id TEXT, calendar_id TEXT,
    adopted INTEGER DEFAULT 0, remote_fp TEXT, held TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY AUTOINCREMENT, rkey TEXT UNIQUE NOT NULL, type TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', resolution TEXT,
    created_at TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (akey TEXT PRIMARY KEY, last_sent TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL, detail TEXT
);
'''


class Store:
    def __init__(self, filename):
        if filename != ':memory:':
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(filename)
        self.conn.row_factory = sqlite3.Row
        if filename != ':memory:':
            self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- runs / scans
    def start_run(self, now):
        cur = self.conn.execute("INSERT INTO runs(started_at,status) VALUES (?,'running')", (now,))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id, now, status, detail):
        self.conn.execute('UPDATE runs SET completed_at=?, status=?, detail=? WHERE id=?',
                          (now, status, json.dumps(detail), run_id))
        self.conn.commit()

    # --- sources
    def source(self, key):
        row = self.conn.execute('SELECT * FROM sources WHERE source_key=?', (key,)).fetchone()
        return dict(row) if row else None

    def upsert_source(self, m, fingerprint, now, run_id):
        existing = self.source(m['key'])
        self.conn.execute('''INSERT INTO sources(source_key,course,title,url,fingerprint,contents,source_updated,first_seen,scanned_at,last_seen_scan)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET course=excluded.course,
            title=excluded.title, url=excluded.url, fingerprint=excluded.fingerprint, contents=excluded.contents,
            source_updated=excluded.source_updated, scanned_at=excluded.scanned_at, last_seen_scan=excluded.last_seen_scan''',
            (m['key'], m['course'], m['title'], m['url'], fingerprint, m['text'][:200000],
             m.get('updated_at') or '', existing['first_seen'] if existing else now, now, run_id))
        self.conn.commit()

    def touch_source(self, key, run_id):
        self.conn.execute('UPDATE sources SET last_seen_scan=? WHERE source_key=?', (run_id, key))
        self.conn.commit()

    def needs_extraction(self, key, fingerprint):
        row = self.source(key)
        return not (row and row['fingerprint'] == fingerprint and row['extracted_fp'] == fingerprint)

    def mark_extracted(self, key, fingerprint):
        self.conn.execute('UPDATE sources SET extracted_fp=? WHERE source_key=?', (fingerprint, key))
        self.conn.commit()

    def missing_sources(self, run_id):
        return [dict(r) for r in self.conn.execute('SELECT source_key,course,title,url FROM sources WHERE last_seen_scan<?', (run_id,))]

    # --- observations
    def replace_observations(self, source_key, items):
        self.conn.execute('DELETE FROM observations WHERE source_key=?', (source_key,))
        for i in items:
            self.conn.execute('''INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (source_key, i['item_key'], i['course'], i['kind'], i['title'], i['date'], i.get('time'),
                 i.get('status', 'scheduled'), i.get('prev_date'), i['confidence'], i['evidence'],
                 i['source_url'], 1 if i.get('structured') else 0))
        self.conn.commit()

    def observations(self):
        return [dict(r) for r in self.conn.execute(
            'SELECT o.*, s.source_updated, s.first_seen, s.last_seen_scan FROM observations o JOIN sources s USING(source_key)')]

    # --- obligations
    def obligations(self):
        out = []
        for r in self.conn.execute('SELECT * FROM obligations'):
            d = dict(r)
            d['members'] = json.loads(d['members'])
            d['urls'] = json.loads(d['urls'] or '[]')
            out.append(d)
        return out

    def save_obligation(self, o):
        self.conn.execute('''INSERT INTO obligations(oid,course,kind,title,date,time,status,members,evidence,urls,notes,
            desired_fp,synced_fp,todoist_id,calendar_id,adopted,remote_fp,held,created_at,updated_at)
            VALUES (:oid,:course,:kind,:title,:date,:time,:status,:members,:evidence,:urls,:notes,:desired_fp,:synced_fp,
            :todoist_id,:calendar_id,:adopted,:remote_fp,:held,:created_at,:updated_at)
            ON CONFLICT(oid) DO UPDATE SET course=excluded.course,kind=excluded.kind,title=excluded.title,date=excluded.date,
            time=excluded.time,status=excluded.status,members=excluded.members,evidence=excluded.evidence,urls=excluded.urls,
            notes=excluded.notes,desired_fp=excluded.desired_fp,synced_fp=excluded.synced_fp,todoist_id=excluded.todoist_id,
            calendar_id=excluded.calendar_id,adopted=excluded.adopted,remote_fp=excluded.remote_fp,held=excluded.held,
            updated_at=excluded.updated_at''',
            {**o, 'members': json.dumps(o['members']), 'urls': json.dumps(o.get('urls', []))})
        self.conn.commit()

    # --- review queue
    def add_review(self, rkey, rtype, payload, now):
        """Idempotent: an existing open OR resolved review with the same key is never duplicated."""
        cur = self.conn.execute('INSERT OR IGNORE INTO review(rkey,type,payload,created_at) VALUES (?,?,?,?)',
                                (rkey, rtype, json.dumps(payload), now))
        self.conn.commit()
        return cur.rowcount == 1

    def reviews(self, status='open'):
        rows = self.conn.execute('SELECT * FROM review WHERE status=? ORDER BY id', (status,)).fetchall()
        return [{**dict(r), 'payload': json.loads(r['payload']),
                 'resolution': json.loads(r['resolution']) if r['resolution'] else None} for r in rows]

    def review(self, rid):
        rows = [r for r in self.reviews('open') + self.reviews('approved') + self.reviews('rejected') if r['id'] == rid]
        return rows[0] if rows else None

    def resolve_review(self, rid, status, resolution, now):
        self.conn.execute('UPDATE review SET status=?, resolution=?, resolved_at=? WHERE id=?',
                          (status, json.dumps(resolution), now, rid))
        self.conn.commit()

    def close_reviews_where_obsolete(self, rtype, valid_keys, now):
        """Auto-close open reviews of a type whose condition no longer holds."""
        for r in self.reviews('open'):
            if r['type'] == rtype and r['rkey'] not in valid_keys:
                self.resolve_review(r['id'], 'rejected', {'auto': 'condition cleared'}, now)

    # --- alerts
    def alert_last(self, key):
        row = self.conn.execute('SELECT last_sent FROM alerts WHERE akey=?', (key,)).fetchone()
        return row[0] if row else None

    def alert_mark(self, key, now):
        self.conn.execute('INSERT INTO alerts VALUES (?,?) ON CONFLICT(akey) DO UPDATE SET last_sent=excluded.last_sent', (key, now))
        self.conn.commit()
