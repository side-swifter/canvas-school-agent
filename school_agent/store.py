"""SQLite scan memory and idempotent action registry."""
import json
import sqlite3
from pathlib import Path

class Store:
    def __init__(self, filename):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(filename)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS sources (
                source_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                contents TEXT NOT NULL, scanned_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS obligations (
                obligation_key TEXT PRIMARY KEY, source_key TEXT NOT NULL,
                content TEXT NOT NULL, fingerprint TEXT NOT NULL,
                todoist_id TEXT, calendar_id TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
                completed_at TEXT, status TEXT NOT NULL, detail TEXT
            );
        ''')
        self.conn.commit()

    def unchanged(self, key, fingerprint):
        row = self.conn.execute('SELECT fingerprint FROM sources WHERE source_key=?', (key,)).fetchone()
        return bool(row and row[0] == fingerprint)

    def save_source(self, key, fingerprint, content, now):
        self.conn.execute('''INSERT INTO sources VALUES (?, ?, ?, ?) ON CONFLICT(source_key)
             DO UPDATE SET fingerprint=excluded.fingerprint, contents=excluded.contents,
             scanned_at=excluded.scanned_at''', (key, fingerprint, content, now))
        self.conn.commit()

    def obligation(self, key):
        row = self.conn.execute('SELECT content,fingerprint,todoist_id,calendar_id FROM obligations WHERE obligation_key=?', (key,)).fetchone()
        return {'content': json.loads(row[0]), 'fingerprint': row[1], 'todoist_id': row[2], 'calendar_id': row[3]} if row else None

    def save_obligation(self, key, source_key, data, fingerprint, todoist_id, calendar_id, now):
        self.conn.execute('''INSERT INTO obligations VALUES (?,?,?,?,?,?,?) ON CONFLICT(obligation_key)
             DO UPDATE SET content=excluded.content, fingerprint=excluded.fingerprint,
             todoist_id=excluded.todoist_id, calendar_id=excluded.calendar_id,
             updated_at=excluded.updated_at''',
            (key, source_key, json.dumps(data), fingerprint, todoist_id, calendar_id, now))
        self.conn.commit()
