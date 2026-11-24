"""SQLite event evidence and transactional domain state."""
import sqlite3, json, hashlib
from pathlib import Path

SCHEMA="""
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, effective_at INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, subsystem TEXT NOT NULL, subject TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subjects(id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(id TEXT PRIMARY KEY, subject TEXT NOT NULL, resource TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS media_originals(id TEXT PRIMARY KEY, artifact TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, sha256 TEXT NOT NULL, archived_day INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS media_derivatives(id TEXT NOT NULL, cms_version INTEGER NOT NULL, artifact TEXT NOT NULL, original_id TEXT NOT NULL, original_sha256 TEXT NOT NULL, ops TEXT NOT NULL, layers TEXT NOT NULL, assets TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, sha256 TEXT NOT NULL, registered_day INTEGER NOT NULL, PRIMARY KEY(id, cms_version));
CREATE TABLE IF NOT EXISTS media_publications(id TEXT NOT NULL, version INTEGER NOT NULL, derivative_id TEXT NOT NULL, derivative_cms_version INTEGER NOT NULL, url TEXT NOT NULL, cdn_sha256 TEXT NOT NULL, published_day INTEGER NOT NULL, takedown_day INTEGER, cdn_purge_day INTEGER, superseded_day INTEGER, state TEXT NOT NULL, PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS media_licenses(id TEXT PRIMARY KEY, artifact TEXT NOT NULL, region TEXT NOT NULL, max_width INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, signer TEXT NOT NULL, page TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS media_license_revocations(event_id TEXT PRIMARY KEY, license_id TEXT NOT NULL, day INTEGER NOT NULL, page TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_assets(id TEXT PRIMARY KEY, kind TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, note TEXT NOT NULL);
"""

def connect(path):
    if str(path)==':memory:': con=sqlite3.connect(':memory:')
    else:
        path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        con=sqlite3.connect(path)
    con.row_factory=sqlite3.Row
    con.executescript(SCHEMA)
    return con

def record(con, subsystem, event):
    raw=json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    digest=hashlib.sha256(raw.encode()).hexdigest()
    old=con.execute('SELECT digest FROM events WHERE id=?',(event['id'],)).fetchone()
    if old:
        if old['digest'] != digest: raise ValueError('event-id-conflict')
        return False
    con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',(event['id'],subsystem,event['action'],event['subject'],int(event.get('effective_at',0)),int(event.get('received_at',0)),raw,digest))
    return True

def decide(con,event,subsystem,outcome,reason,state):
    con.execute('INSERT INTO decisions(event_id,subsystem,subject,outcome,reason,state) VALUES(?,?,?,?,?,?)',(event['id'],subsystem,event['subject'],outcome,reason,state))
    return {'event':event['id'],'subsystem':subsystem,'subject':event['subject'],'outcome':outcome,'reason':reason,'state':state}

def latest(con, table, key):
    return con.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()

def report(con):
    events=[dict(x) for x in con.execute('SELECT id,subsystem,action,subject,effective_at,digest FROM events ORDER BY effective_at,id')]
    decisions=[dict(x) for x in con.execute('SELECT event_id,subsystem,subject,outcome,reason,state FROM decisions ORDER BY id')]
    return {'events':events,'decisions':decisions,'subjects':[dict(x) for x in con.execute('SELECT * FROM subjects ORDER BY id')], 'allocations':[dict(x) for x in con.execute('SELECT * FROM allocations ORDER BY id')], 'receipts':[dict(x) for x in con.execute('SELECT * FROM receipts ORDER BY id')]}
