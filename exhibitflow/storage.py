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

-- customs capture: original media bytes, page revisions, columns, signatures
CREATE TABLE IF NOT EXISTS customs_media(id TEXT PRIMARY KEY, document TEXT NOT NULL, page INTEGER NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL, mime TEXT, digest TEXT NOT NULL, bytes BLOB NOT NULL, received_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS customs_pages(id TEXT PRIMARY KEY, document TEXT NOT NULL, page INTEGER NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL, trip TEXT NOT NULL, box TEXT, seal TEXT, export_country TEXT, destination TEXT, permitted TEXT NOT NULL, digest TEXT NOT NULL, media_id TEXT, received_at INTEGER NOT NULL, effective_at INTEGER NOT NULL, supersedes TEXT, UNIQUE(document,page,version));
CREATE TABLE IF NOT EXISTS customs_columns(id TEXT PRIMARY KEY, document TEXT NOT NULL, page INTEGER NOT NULL, version INTEGER NOT NULL, col_no TEXT NOT NULL, destination TEXT NOT NULL, scope TEXT NOT NULL, valid_from INTEGER NOT NULL, valid_to INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL, permitted TEXT NOT NULL, signature_id TEXT, ocr_ok INTEGER NOT NULL DEFAULT 1, UNIQUE(document,page,version,col_no));
CREATE TABLE IF NOT EXISTS customs_signatures(id TEXT PRIMARY KEY, document TEXT NOT NULL, page INTEGER NOT NULL, version INTEGER NOT NULL, col_no TEXT NOT NULL, seal_id TEXT NOT NULL, digest TEXT NOT NULL, bytes BLOB NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL, valid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS customs_tokens(id TEXT PRIMARY KEY, document TEXT NOT NULL, page INTEGER NOT NULL, version INTEGER NOT NULL, rev INTEGER NOT NULL, col_no TEXT NOT NULL, idx INTEGER NOT NULL, ch TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL, confidence REAL NOT NULL, source TEXT NOT NULL, UNIQUE(document,page,version,rev,col_no,idx,source));
CREATE TABLE IF NOT EXISTS customs_corrections(id TEXT PRIMARY KEY, document TEXT NOT NULL, page INTEGER NOT NULL, col_no TEXT NOT NULL, idx INTEGER NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL, operator TEXT NOT NULL, at INTEGER NOT NULL);

-- trip / box association candidates (never per-item proof)
CREATE TABLE IF NOT EXISTS customs_candidates(id TEXT PRIMARY KEY, trip TEXT NOT NULL, box TEXT, seal TEXT, document TEXT NOT NULL, page INTEGER NOT NULL, scope TEXT NOT NULL, authoritative INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL, received_at INTEGER NOT NULL);

-- artifact registry (collection code + physical box/seal at sealing)
CREATE TABLE IF NOT EXISTS customs_artifacts(id TEXT PRIMARY KEY, collection_code TEXT NOT NULL, trip TEXT, box TEXT, seal TEXT, document TEXT, registered_at INTEGER NOT NULL, payload TEXT NOT NULL);

-- per-item evidence: sealing scan or unboxing recheck
CREATE TABLE IF NOT EXISTS customs_evidence(id TEXT PRIMARY KEY, kind TEXT NOT NULL, artifact TEXT NOT NULL, col_no TEXT, document TEXT NOT NULL, page INTEGER, trip TEXT, box TEXT, seal TEXT, collection_code TEXT, actor TEXT, at INTEGER NOT NULL, valid INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL, digest TEXT NOT NULL);

-- append-only per-item attribution versions
CREATE TABLE IF NOT EXISTS customs_attributions(id TEXT PRIMARY KEY, trip TEXT, document TEXT NOT NULL, artifact TEXT NOT NULL, col_no TEXT, status TEXT NOT NULL, basis TEXT NOT NULL, evidence_id TEXT, alternatives TEXT, destination TEXT, scope TEXT, version INTEGER NOT NULL, supersedes TEXT, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);

-- one artifact per release column (transactional uniqueness lock)
CREATE TABLE IF NOT EXISTS customs_column_claims(trip TEXT NOT NULL, document TEXT NOT NULL, col_no TEXT NOT NULL, artifact TEXT NOT NULL, attribution_id TEXT NOT NULL, claimed_at INTEGER NOT NULL, PRIMARY KEY(trip,document,col_no));

-- reverse void chain for erroneous (legacy) associations
CREATE TABLE IF NOT EXISTS customs_voids(id TEXT PRIMARY KEY, artifact TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, reason TEXT NOT NULL, trigger TEXT, at INTEGER NOT NULL, payload TEXT NOT NULL);

-- interception of already sealed / in-transit artifacts
CREATE TABLE IF NOT EXISTS customs_intercepts(id TEXT PRIMARY KEY, artifact TEXT NOT NULL, allocation_id TEXT, attribution_id TEXT, void_id TEXT, at INTEGER NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL);
"""


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_obj(obj):
    return digest_bytes(canonical(obj).encode())


def connect(path):
    if str(path) == ':memory:':
        con = sqlite3.connect(':memory:')
    else:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def record(con, subsystem, event):
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    old = con.execute('SELECT digest FROM events WHERE id=?', (event['id'],)).fetchone()
    if old:
        if old['digest'] != digest:
            raise ValueError('event-id-conflict')
        return False
    con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',
                (event['id'], subsystem, event['action'], event['subject'],
                 int(event.get('effective_at', 0)), int(event.get('received_at', 0)), raw, digest))
    return True


def decide(con, event, subsystem, outcome, reason, state):
    con.execute('INSERT INTO decisions(event_id,subsystem,subject,outcome,reason,state) VALUES(?,?,?,?,?,?)',
                (event['id'], subsystem, event['subject'], outcome, reason, state))
    return {'event': event['id'], 'subsystem': subsystem, 'subject': event['subject'],
            'outcome': outcome, 'reason': reason, 'state': state}


def latest(con, table, key):
    return con.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()


def one(con, sql, args=()):
    return con.execute(sql, args).fetchone()


def rows(con, sql, args=()):
    return con.execute(sql, args).fetchall()


REPORT_TABLES = ('events', 'decisions', 'subjects', 'allocations', 'receipts',
                 'customs_media', 'customs_pages', 'customs_columns', 'customs_signatures',
                 'customs_tokens', 'customs_corrections', 'customs_candidates', 'customs_artifacts',
                 'customs_evidence', 'customs_attributions', 'customs_column_claims',
                 'customs_voids', 'customs_intercepts')


def report(con):
    def table(sql):
        return [dict(x) for x in con.execute(sql)]
    return {
        'events': table('SELECT id,subsystem,action,subject,effective_at,digest FROM events ORDER BY effective_at,id'),
        'decisions': table('SELECT event_id,subsystem,subject,outcome,reason,state FROM decisions ORDER BY id'),
        'subjects': table('SELECT * FROM subjects ORDER BY id'),
        'allocations': table('SELECT * FROM allocations ORDER BY id'),
        'receipts': table('SELECT * FROM receipts ORDER BY id'),
        'customs_media': table('SELECT id,document,page,version,kind,mime,digest,received_at FROM customs_media ORDER BY id'),
        'customs_pages': table('SELECT * FROM customs_pages ORDER BY document,page,version'),
        'customs_columns': table('SELECT * FROM customs_columns ORDER BY document,page,version,col_no'),
        'customs_signatures': table('SELECT id,document,page,version,col_no,seal_id,digest,x,y,w,h,valid FROM customs_signatures ORDER BY id'),
        'customs_tokens': table('SELECT * FROM customs_tokens ORDER BY document,page,version,rev,col_no,source,idx'),
        'customs_corrections': table('SELECT * FROM customs_corrections ORDER BY document,page,col_no,idx,at,id'),
        'customs_candidates': table('SELECT * FROM customs_candidates ORDER BY id'),
        'customs_artifacts': table('SELECT * FROM customs_artifacts ORDER BY id'),
        'customs_evidence': table('SELECT id,kind,artifact,col_no,document,page,trip,box,seal,collection_code,actor,at,valid,digest FROM customs_evidence ORDER BY id'),
        'customs_attributions': table('SELECT * FROM customs_attributions ORDER BY created_at,id'),
        'customs_column_claims': table('SELECT * FROM customs_column_claims ORDER BY trip,document,col_no'),
        'customs_voids': table('SELECT * FROM customs_voids ORDER BY at,id'),
        'customs_intercepts': table('SELECT * FROM customs_intercepts ORDER BY at,id'),
    }
