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
CREATE TABLE IF NOT EXISTS chrono_sources(source_id TEXT PRIMARY KEY, kind TEXT NOT NULL, summary TEXT NOT NULL, page TEXT, era_system TEXT NOT NULL, translation_version TEXT, reviewer TEXT, revised_at INTEGER NOT NULL DEFAULT 0, withdrawn_at INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chrono_readings(id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, object_id TEXT NOT NULL, candidate TEXT NOT NULL, low_year INTEGER NOT NULL, high_year INTEGER NOT NULL, confidence TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chrono_editorials(id INTEGER PRIMARY KEY AUTOINCREMENT, object_id TEXT NOT NULL, language TEXT NOT NULL, revision INTEGER NOT NULL, received_at INTEGER NOT NULL, title TEXT NOT NULL, date_label TEXT NOT NULL, source_id TEXT NOT NULL, translation_version TEXT, state TEXT NOT NULL, withdrawn_at INTEGER NOT NULL DEFAULT 0, UNIQUE(object_id,language,revision));
CREATE TABLE IF NOT EXISTS chrono_edges(edge_id TEXT PRIMARY KEY, earlier TEXT NOT NULL, later TEXT NOT NULL, delta INTEGER NOT NULL, source_id TEXT NOT NULL, reviewer TEXT, status TEXT NOT NULL, revised_at INTEGER NOT NULL DEFAULT 0, withdrawn_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS catalog_versions(version_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, published_at INTEGER NOT NULL, status TEXT NOT NULL, basis TEXT NOT NULL, solution_count INTEGER NOT NULL, payload TEXT NOT NULL, print_run TEXT);
CREATE TABLE IF NOT EXISTS catalog_errata(erratum_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, old_version_id TEXT NOT NULL, new_version_id TEXT NOT NULL, affected_labels TEXT NOT NULL, justification TEXT NOT NULL, created_at INTEGER NOT NULL);
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
    return {'events':events,'decisions':decisions,'subjects':[dict(x) for x in con.execute('SELECT * FROM subjects ORDER BY id')], 'allocations':[dict(x) for x in con.execute('SELECT * FROM allocations ORDER BY id')], 'receipts':[dict(x) for x in con.execute('SELECT * FROM receipts ORDER BY id')],
            'chrono_sources':[dict(x) for x in con.execute('SELECT * FROM chrono_sources ORDER BY source_id')],
            'chrono_readings':[dict(x) for x in con.execute('SELECT source_id,object_id,candidate,low_year,high_year,confidence,state FROM chrono_readings ORDER BY id')],
            'chrono_editorials':[dict(x) for x in con.execute('SELECT object_id,language,revision,received_at,title,date_label,source_id,translation_version,state,withdrawn_at FROM chrono_editorials ORDER BY id')],
            'chrono_edges':[dict(x) for x in con.execute('SELECT * FROM chrono_edges ORDER BY edge_id')],
            'catalog_versions':[dict(x) for x in con.execute('SELECT version_id,sequence,published_at,status,basis,solution_count,print_run FROM catalog_versions ORDER BY sequence')],
            'catalog_errata':[dict(x) for x in con.execute('SELECT erratum_id,old_version_id,new_version_id,affected_labels,justification,created_at FROM catalog_errata ORDER BY created_at,erratum_id')]}
