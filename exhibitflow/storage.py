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
CREATE TABLE IF NOT EXISTS gate_keys(gate TEXT NOT NULL, key_domain TEXT NOT NULL, verify_key TEXT NOT NULL, valid_from INTEGER NOT NULL DEFAULT 0, valid_to INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(gate,key_domain));
CREATE TABLE IF NOT EXISTS attendance_pages(id TEXT PRIMARY KEY, gate TEXT NOT NULL, key_domain TEXT NOT NULL, seq INTEGER NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, rows_json TEXT NOT NULL, effective_at INTEGER NOT NULL, received_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS attendance_sessions(gate TEXT NOT NULL, key_domain TEXT NOT NULL, token TEXT NOT NULL, first_in INTEGER NOT NULL, last_out INTEGER NOT NULL, source_page TEXT NOT NULL, PRIMARY KEY(gate,key_domain,token,first_in,last_out));
CREATE TABLE IF NOT EXISTS display_segments(id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, seg_start INTEGER NOT NULL, seg_end INTEGER NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counter_readings(counter TEXT NOT NULL, effective_at INTEGER NOT NULL, raw INTEGER NOT NULL, wraps INTEGER NOT NULL, corrected INTEGER NOT NULL, modulus INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, PRIMARY KEY(counter,effective_at,raw));
CREATE TABLE IF NOT EXISTS overlap_certificates(id TEXT PRIMARY KEY, subject TEXT NOT NULL, day TEXT NOT NULL, a_domain TEXT NOT NULL, b_domain TEXT NOT NULL, intersection INTEGER NOT NULL, blind_domain TEXT NOT NULL, pages TEXT NOT NULL, state TEXT NOT NULL, effective_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bucket_documents(id TEXT PRIMARY KEY, subject TEXT NOT NULL, day TEXT NOT NULL, seg_start INTEGER NOT NULL, seg_end INTEGER NOT NULL, capacity INTEGER NOT NULL, max_repeat INTEGER NOT NULL, entries INTEGER NOT NULL, exits INTEGER NOT NULL, state TEXT NOT NULL, effective_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS gallery_buckets(doc TEXT NOT NULL, seq INTEGER NOT NULL, b_start INTEGER NOT NULL, b_end INTEGER NOT NULL, ins INTEGER NOT NULL, outs INTEGER NOT NULL, occupancy INTEGER NOT NULL, PRIMARY KEY(doc,seq));
CREATE TABLE IF NOT EXISTS attendance_proofs(id TEXT PRIMARY KEY, subject TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, lower_bound INTEGER NOT NULL, upper_bound INTEGER NOT NULL, settlement INTEGER NOT NULL, basis TEXT NOT NULL, version INTEGER NOT NULL, supersedes TEXT NOT NULL DEFAULT '', revision_reason TEXT NOT NULL DEFAULT '', effective_at INTEGER NOT NULL);
"""

ATTENDANCE_TABLES=('gate_keys','attendance_pages','attendance_sessions','display_segments','counter_readings','overlap_certificates','bucket_documents','gallery_buckets','attendance_proofs')

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
    out={'events':events,'decisions':decisions,'subjects':[dict(x) for x in con.execute('SELECT * FROM subjects ORDER BY id')], 'allocations':[dict(x) for x in con.execute('SELECT * FROM allocations ORDER BY id')], 'receipts':[dict(x) for x in con.execute('SELECT * FROM receipts ORDER BY id')]}
    for t in ATTENDANCE_TABLES: out[t]=[dict(x) for x in con.execute(f'SELECT * FROM {t} ORDER BY 1,2,3')]
    return out
