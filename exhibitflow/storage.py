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

-- route capture: archived external evidence, one row per captured source
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, kind TEXT NOT NULL, digest TEXT NOT NULL, event_id TEXT NOT NULL, captured_at INTEGER NOT NULL);
-- issuer-scoped road notices; never merged across issuers even for the same road_id
CREATE TABLE IF NOT EXISTS announcements(id TEXT PRIMARY KEY, issuer TEXT NOT NULL, road_id TEXT NOT NULL, notice_no TEXT NOT NULL, valid_from INTEGER NOT NULL, valid_to INTEGER NOT NULL, status TEXT NOT NULL, published_at INTEGER NOT NULL, retracts TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL);
-- one row per recurring-trip occurrence; RECURRENCE-ID overrides only that row
CREATE TABLE IF NOT EXISTS trips(id TEXT PRIMARY KEY, series_uid TEXT NOT NULL, recurrence_id TEXT NOT NULL, occurrence TEXT NOT NULL, vehicle TEXT NOT NULL, route TEXT NOT NULL, artifact TEXT NOT NULL, depart_at INTEGER NOT NULL, arrive_at INTEGER NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reroutes(id TEXT PRIMARY KEY, trip TEXT NOT NULL, from_route TEXT NOT NULL, to_route TEXT NOT NULL, cancelled INTEGER NOT NULL, received_at INTEGER NOT NULL, effective_at INTEGER NOT NULL, disposition TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roads(id TEXT PRIMARY KEY, capacity INTEGER NOT NULL, clear_height REAL NOT NULL, permit_required INTEGER NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS permits(id TEXT PRIMARY KEY, road_id TEXT NOT NULL, window_from INTEGER NOT NULL, window_to INTEGER NOT NULL, issuer TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vehicles(id TEXT PRIMARY KEY, max_height REAL NOT NULL, routes TEXT NOT NULL, escort_ok INTEGER NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cargo(id TEXT PRIMARY KEY, artifact TEXT NOT NULL, box TEXT NOT NULL, height REAL NOT NULL, part TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seal_history(id TEXT PRIMARY KEY, seal TEXT NOT NULL, vehicle TEXT NOT NULL, artifact TEXT NOT NULL, effective_at INTEGER NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL);
-- cross-vehicle conflict edges written to SQLite, grouped by plan label
CREATE TABLE IF NOT EXISTS conflict_edges(id TEXT PRIMARY KEY, graph TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, digest TEXT NOT NULL);
-- dispatch history; current dispatch table is the non-superseded handoff orders
CREATE TABLE IF NOT EXISTS convoy_plans(id TEXT PRIMARY KEY, event_id TEXT NOT NULL, protocol TEXT NOT NULL, algorithm TEXT NOT NULL, trip TEXT NOT NULL, vehicle TEXT NOT NULL, route TEXT NOT NULL, artifact TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, effective_at INTEGER NOT NULL, sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS handoff_orders(id TEXT PRIMARY KEY, event_id TEXT NOT NULL, subject TEXT NOT NULL, trip TEXT NOT NULL, route TEXT NOT NULL, state TEXT NOT NULL, effective_at INTEGER NOT NULL, superseded_by TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS compensation_events(id TEXT PRIMARY KEY, event_id TEXT NOT NULL, subject TEXT NOT NULL, trip TEXT NOT NULL, route TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL, effective_at INTEGER NOT NULL);
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

def digest(obj):
    return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

TABLES=('events','decisions','subjects','documents','allocations','receipts','sources','announcements',
        'trips','reroutes','roads','permits','vehicles','cargo','seal_history','conflict_edges',
        'convoy_plans','handoff_orders','compensation_events')

def report(con):
    events=[dict(x) for x in con.execute('SELECT id,subsystem,action,subject,effective_at,digest FROM events ORDER BY effective_at,id')]
    decisions=[dict(x) for x in con.execute('SELECT event_id,subsystem,subject,outcome,reason,state FROM decisions ORDER BY id')]
    out={'events':events,'decisions':decisions,
         'subjects':[dict(x) for x in con.execute('SELECT * FROM subjects ORDER BY id')],
         'allocations':[dict(x) for x in con.execute('SELECT * FROM allocations ORDER BY id')],
         'receipts':[dict(x) for x in con.execute('SELECT * FROM receipts ORDER BY id')]}
    extra=('sources','announcements','trips','reroutes','roads','permits','vehicles','cargo','seal_history',
           'conflict_edges','convoy_plans','handoff_orders','compensation_events')
    for table in extra:
        out[table]=[dict(x) for x in con.execute(f'SELECT * FROM {table} ORDER BY id')]
    out['documents']=[dict(x) for x in con.execute('SELECT * FROM documents ORDER BY id')]
    return out
