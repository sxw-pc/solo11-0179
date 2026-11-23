"""海关双栏放行单取证：原件、字格、签章、逐件归属与错关联撤销。

设计要点
--------
* 车次二维码只证明“这一车次被放行”，永远不能推出“车上哪一件对应哪一栏”。
* 车次、箱号、封条只作为关联候选（``customs_candidates``），不参与逐件裁定。
* 逐件归属只接受两种有权确认件号归属的证据：
  ``sealing_scan``（封箱时逐件签名扫描台账，历史时点）与
  ``unboxing_recheck``（海关独立开箱复核，当前实物）。
* 归属结果只有 ``confirmed`` / ``held`` / ``unresolved``（历史遗留关联为 ``provisional``）。
* 同一 (trip, document, col_no) 只能被一件文物占用（``customs_column_claims``）。
* 旧归属与已发裁定永不覆写；由 ``customs_voids`` 追加反向作废链，
  并让已封/在途的分配进入 ``intercept``。
"""
import base64
import hashlib
import itertools
import json

from . import storage as s

NAME = '通关取证'

STATUS_CONFIRMED = 'confirmed'
STATUS_HELD = 'held'
STATUS_UNRESOLVED = 'unresolved'
STATUS_PROVISIONAL = 'provisional'

AUTHORITATIVE_KINDS = ('sealing_scan', 'unboxing_recheck')
LEGACY_BASIS = 'legacy-rpa-page-order'
_RANK = {LEGACY_BASIS: 0, 'sealing_scan': 1, 'unboxing_recheck': 2}
ACTIVE_ALLOCATIONS = ('sealed', 'in-transit', 'broken')


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _hash_id(prefix, *parts):
    raw = '|'.join(str(x) for x in parts).encode('utf-8')
    return prefix + '-' + hashlib.sha256(raw).hexdigest()[:20]


def _decode(value):
    if value is None:
        return b''
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return base64.b64decode(str(value))


def _bbox(value):
    if not value or len(value) != 4:
        return None
    try:
        return [int(v) for v in value]
    except (TypeError, ValueError):
        return None


def _inside(outer, inner):
    if not outer or not inner:
        return False
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and inner[0] + inner[2] <= outer[0] + outer[2]
            and inner[1] + inner[3] <= outer[1] + outer[3])


def _event_at(e):
    return int(e.get('effective_at', 0))


def _received(e):
    return int(e.get('received_at', e.get('effective_at', 0)))


def _decision(c, e, outcome, reason, state, extra=None):
    result = s.decide(c, e, NAME, outcome, reason, state)
    if extra is not None:
        result['data'] = extra
    return result


def _json_load(value, default):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# page / column / token reconstruction
# ---------------------------------------------------------------------------
def latest_pages(c, document):
    """Latest captured version of every page of a document, ordered by page."""
    latest = {}
    for row in c.execute('SELECT * FROM customs_pages WHERE document=? ORDER BY page,version', (document,)):
        row = dict(row)
        key = row['page']
        if key not in latest or row['version'] > latest[key]['version']:
            latest[key] = row
    return [latest[k] for k in sorted(latest)]


def column_map(c, document):
    """col_no -> latest column definition across page revisions."""
    cols = {}
    for page in latest_pages(c, document):
        for row in c.execute('SELECT * FROM customs_columns WHERE document=? AND page=? AND version=? ORDER BY col_no',
                             (document, page['page'], page['version'])):
            col = dict(row)
            col['permitted'] = _json_load(col.get('permitted'), [])
            col['page_received'] = page['received_at']
            col['page_trip'] = page['trip']
            col['page_box'] = page['box']
            col['page_seal'] = page['seal']
            col['page_export_country'] = page['export_country']
            prior = cols.get(col['col_no'])
            if prior is None or (page['received_at'], page['page']) >= (prior['page_received'], prior['page']):
                cols[col['col_no']] = col
    return cols


def token_text(c, document, page, col_no):
    """Reconstruct visible piece number from the latest OCR revision plus
    human corrections.  Original OCR revisions are never mutated."""
    rev = c.execute('SELECT MAX(rev) r FROM customs_tokens WHERE document=? AND page=? AND col_no=?',
                    (document, page, col_no)).fetchone()
    if not rev or rev['r'] is None:
        return None, []
    tokens = c.execute("SELECT * FROM customs_tokens WHERE document=? AND page=? AND col_no=? AND rev=? AND source='ocr' ORDER BY idx",
                       (document, page, col_no, rev['r'])).fetchall()
    fixed = {}
    for row in c.execute('SELECT idx,after,at,id FROM customs_corrections WHERE document=? AND page=? AND col_no=? ORDER BY at,id',
                         (document, page, col_no)):
        fixed[row['idx']] = row['after']
    chars = []
    out = []
    for t in tokens:
        char = fixed.get(t['idx'], t['ch'])
        chars.append(char)
        item = dict(t)
        item['effective_ch'] = char
        out.append(item)
    return ''.join(chars), out


def _column_tokens_ok(tokens, col):
    """Every OCR character cell must sit inside the column's printed region."""
    if not tokens:
        return False
    box = [col['x'], col['y'], col['w'], col['h']]
    for t in tokens:
        if not _inside(box, [t['x'], t['y'], t['w'], t['h']]):
            return False
    return True


def column_is_valid(col, when):
    return int(col['valid_from']) <= int(when) <= int(col['valid_to'])


def column_exports(col):
    return (col is not None and col['scope'] == 'export'
            and col['destination'] in col.get('permitted', []))


def _store_media(c, document, page, version, kind, media, received_at):
    media = media or {}
    raw = _decode(media.get('bytes_b64'))
    computed = _sha(raw) if raw else _sha(canonical(media).encode())
    declared = media.get('digest')
    if declared and computed and declared != computed and raw:
        return None, None, 'media-digest-mismatch'
    digest = declared or computed
    existing = c.execute('SELECT id FROM customs_media WHERE document=? AND page=? AND kind=? AND digest=?',
                         (document, page, kind, digest)).fetchone()
    if existing:
        return existing['id'], digest, None
    mid = media.get('id') or _hash_id('media', document, page, kind, digest)
    c.execute('INSERT INTO customs_media VALUES(?,?,?,?,?,?,?,?,?)',
              (mid, document, page, version, kind, media.get('mime'), digest, raw, received_at))
    return mid, digest, None


def _signature(c, document, page, version, col_no, col_box, signature):
    """Validate and prepare an electronic customs seal for one column.

    The seal rectangle must fully cover the piece-number cell (件号与章的覆盖区域).
    The original seal bytes are persisted, never regenerated.
    """
    if not signature or not signature.get('seal_id'):
        return None, 'signature-missing'
    raw = _decode(signature.get('bytes_b64'))
    if not raw:
        return None, 'signature-bytes-missing'
    digest = _sha(raw)
    if signature.get('digest') and signature['digest'] != digest:
        return None, 'signature-digest-mismatch'
    box = _bbox([signature.get('x'), signature.get('y'), signature.get('w'), signature.get('h')])
    if box is None:
        return None, 'signature-region-missing'
    if not _inside(box, col_box):
        return None, 'signature-does-not-cover-column'
    sid = signature.get('id') or _hash_id('sig', document, page, version, col_no, signature['seal_id'], digest)
    return {'id': sid, 'seal_id': signature['seal_id'], 'digest': digest, 'bytes': raw,
            'box': box, 'valid': 1}, None


def _store_tokens(c, e, document, page, version, tokens, source):
    rev_row = c.execute('SELECT MAX(rev) r FROM customs_tokens WHERE document=? AND page=? AND version=?',
                        (document, page, version)).fetchone()
    rev = int(rev_row['r'] or 0) + 1
    for i, t in enumerate(tokens):
        col_no = str(t.get('col_no', ''))
        idx = int(t.get('idx', i))
        tid = t.get('id') or _hash_id('tok', document, page, version, rev, col_no, idx, source)
        c.execute('INSERT OR IGNORE INTO customs_tokens VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (tid, document, page, version, rev, col_no, idx, str(t.get('ch', '')),
                   int(t.get('x', 0)), int(t.get('y', 0)), int(t.get('w', 0)), int(t.get('h', 0)),
                   float(t.get('confidence', 1.0)), source))
    return rev


def _refresh_ocr(c, document, page, version, col_nos):
    result = {}
    for col_no in sorted(col_nos):
        text, tokens = token_text(c, document, page, col_no)
        col = c.execute('SELECT * FROM customs_columns WHERE document=? AND page=? AND version=? AND col_no=?',
                        (document, page, version, col_no)).fetchone()
        if not col:
            continue
        inside = _column_tokens_ok(tokens, col)
        # no OCR at all is "not checked"; provided OCR must both match and sit
        # inside the column's region.
        ok = (text is None) or (text == col_no and inside)
        c.execute('UPDATE customs_columns SET ocr_ok=? WHERE id=?', (1 if ok else 0, col['id']))
        result[col_no] = {'text': text, 'ok': ok, 'inside': inside, 'tokens': len(tokens)}
    return result


def capture(c, e):
    p = e.get('data', {})
    document = p.get('document') or e['subject']
    page = int(p.get('page', 1))
    trip = p.get('trip') or p.get('qr')
    if not document or not trip:
        return _decision(c, e, 'blocked', 'document-or-trip-missing', 'blocked')
    if p.get('qr') and p['qr'] != trip:
        return _decision(c, e, 'blocked', 'qr-trip-mismatch', 'blocked')

    prior = c.execute('SELECT MAX(version) v FROM customs_pages WHERE document=? AND page=?',
                      (document, page)).fetchone()['v']
    version = int(p.get('version', (prior or 0) + 1))
    if version <= (prior or 0):
        existing = c.execute('SELECT id FROM customs_pages WHERE document=? AND page=? AND version=?',
                             (document, page, version)).fetchone()
        if existing:
            return _decision(c, e, 'idempotent', 'page-version-already-captured', 'captured',
                             {'document': document, 'page': page, 'version': version})
        return _decision(c, e, 'blocked', 'page-version-not-newer', 'blocked')

    media_id, media_digest, err = _store_media(c, document, page, version, 'image', p.get('media'), _received(e))
    if err:
        return _decision(c, e, 'blocked', err, 'blocked')
    if p.get('signature_media'):
        _, _, err = _store_media(c, document, page, version, 'signature-image', p['signature_media'], _received(e))
        if err:
            return _decision(c, e, 'blocked', err, 'blocked')

    columns = p.get('columns') or []
    if not columns:
        return _decision(c, e, 'blocked', 'columns-missing', 'blocked')
    permitted = p.get('permitted_destinations')
    if permitted is None:
        permitted = [str(col.get('destination', '')) for col in columns]

    prepared = []
    seen = set()
    for col in columns:
        col_no = str(col.get('col_no', ''))
        col_box = _bbox([col.get('x'), col.get('y'), col.get('w'), col.get('h')])
        if not col_no or col_box is None:
            return _decision(c, e, 'blocked', 'column-number-or-region-missing', 'blocked')
        if col_no in seen:
            return _decision(c, e, 'blocked', 'duplicate-column-number', 'blocked')
        seen.add(col_no)
        if not col.get('destination') or col.get('scope') not in ('export', 'local'):
            return _decision(c, e, 'blocked', 'destination-or-scope-missing', 'blocked')
        sig, err = _signature(c, document, page, version, col_no, col_box, col.get('signature'))
        if err:
            return _decision(c, e, 'blocked', err, 'blocked', {'col_no': col_no})
        col_permitted = col.get('permitted_destinations') or permitted
        prepared.append((col_no, col_box, col, sig, col_permitted))

    page_digest = _sha(canonical({
        'media': media_digest, 'trip': trip, 'box': p.get('box'), 'seal': p.get('seal'),
        'columns': [{k: col.get(k) for k in (
            'col_no', 'destination', 'scope', 'valid_from', 'valid_to', 'x', 'y', 'w', 'h')}
                    for col_no, _, col, _, _ in prepared],
        'permitted': {col_no: cp for col_no, _, _, _, cp in prepared},
        'signatures': {col_no: sig['digest'] for col_no, _, _, sig, _ in prepared},
    }).encode())
    latest_page = c.execute('SELECT * FROM customs_pages WHERE document=? AND page=? ORDER BY version DESC LIMIT 1',
                            (document, page)).fetchone()
    if (latest_page and latest_page['digest'] == page_digest
            and latest_page['trip'] == trip and latest_page['box'] == p.get('box')
            and latest_page['seal'] == p.get('seal')):
        return _decision(c, e, 'idempotent', 'identical-page-replay', 'captured',
                         {'document': document, 'page': page, 'version': latest_page['version'],
                          'media_id': latest_page['media_id'], 'digest': page_digest})
    page_id = _hash_id('page', document, page, version)
    c.execute('INSERT INTO customs_pages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (page_id, document, page, version, p.get('kind', 'release-sheet'), trip,
               p.get('box'), p.get('seal'), p.get('export_country'), p.get('destination'),
               canonical(permitted), page_digest, media_id, _received(e), _event_at(e), p.get('supersedes')))

    for col_no, col_box, col, sig, col_permitted in prepared:
        c.execute('INSERT INTO customs_signatures VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (sig['id'], document, page, version, col_no, sig['seal_id'], sig['digest'],
                   sig['bytes'], sig['box'][0], sig['box'][1], sig['box'][2], sig['box'][3], 1))
        c.execute('INSERT INTO customs_columns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (_hash_id('col', document, page, version, col_no), document, page, version, col_no,
                   str(col['destination']), col['scope'], int(col.get('valid_from', 0)),
                   int(col.get('valid_to', 10 ** 9)), col_box[0], col_box[1], col_box[2], col_box[3],
                   canonical(col_permitted), sig['id'], 1))

    tokens = p.get('tokens') or []
    unknown = sorted({str(t.get('col_no')) for t in tokens} - seen)
    if unknown:
        return _decision(c, e, 'blocked', 'token-column-unknown', 'blocked', {'columns': unknown})
    if tokens:
        _store_tokens(c, e, document, page, version, tokens, p.get('token_source', 'ocr'))
    ocr_state = _refresh_ocr(c, document, page, version, seen) if tokens else \
        {col_no: {'text': None, 'ok': True, 'inside': True, 'tokens': 0} for col_no in seen}

    for cand in p.get('candidates') or []:
        _store_candidate(c, cand, document, page, trip)
    if p.get('candidate'):
        _store_candidate(c, p['candidate'], document, page, trip)

    ok = all(v['ok'] for v in ocr_state.values())
    return _decision(c, e, 'captured' if ok else 'captured-with-ocr-mismatch',
                     'signature-covers-column' if ok else 'ocr-text-requires-correction',
                     'captured',
                     {'document': document, 'page': page, 'version': version,
                      'media_id': media_id, 'digest': page_digest, 'ocr': ocr_state})


def _store_candidate(c, cand, document, page, trip):
    trip = cand.get('trip') or trip
    cid = cand.get('id') or _hash_id('cand', trip, cand.get('box'), cand.get('seal'), document, page)
    c.execute('INSERT OR REPLACE INTO customs_candidates VALUES(?,?,?,?,?,?,?,?,?,?)',
              (cid, trip, cand.get('box'), cand.get('seal'), document, page,
               cand.get('scope', 'trip'), 1 if cand.get('authoritative') else 0,
               canonical(cand), int(cand.get('received_at', 0))))


def ocr(c, e):
    p = e.get('data', {})
    document = p.get('document') or e['subject']
    page = int(p.get('page', 1))
    page_row = c.execute('SELECT * FROM customs_pages WHERE document=? AND page=? ORDER BY version DESC LIMIT 1',
                         (document, page)).fetchone()
    if not page_row:
        return _decision(c, e, 'blocked', 'page-not-captured', 'blocked')
    tokens = p.get('tokens') or []
    if not tokens:
        return _decision(c, e, 'blocked', 'tokens-missing', 'blocked')
    known = {r['col_no'] for r in c.execute('SELECT col_no FROM customs_columns WHERE document=? AND page=? AND version=?',
                                            (document, page, page_row['version']))}
    if any(str(t.get('col_no')) not in known for t in tokens):
        return _decision(c, e, 'blocked', 'token-column-unknown', 'blocked')
    rev = _store_tokens(c, e, document, page, page_row['version'], tokens, p.get('token_source', 'ocr'))
    state = _refresh_ocr(c, document, page, page_row['version'], known)
    ok = all(v['ok'] for v in state.values())
    return _decision(c, e, 'ocr-reread' if ok else 'ocr-mismatch',
                     'tokens-versioned-original-preserved', 'ocr-reread' if ok else 'ocr-mismatch',
                     {'revision': rev, 'columns': state})


def correct(c, e):
    p = e.get('data', {})
    document = p.get('document') or e['subject']
    page = int(p.get('page', 1))
    col_no = str(p.get('col_no', ''))
    idx = int(p.get('idx', 0))
    before = str(p.get('before', ''))
    after = str(p.get('after', ''))
    operator = p.get('operator') or e['subject']
    if not col_no or not after:
        return _decision(c, e, 'blocked', 'correction-incomplete', 'blocked')
    page_row = c.execute('SELECT * FROM customs_pages WHERE document=? AND page=? ORDER BY version DESC LIMIT 1',
                         (document, page)).fetchone()
    if not page_row:
        return _decision(c, e, 'blocked', 'page-not-captured', 'blocked')
    cid = p.get('id') or _hash_id('corr', document, page, col_no, idx, before, after, operator, _event_at(e))
    c.execute('INSERT OR IGNORE INTO customs_corrections VALUES(?,?,?,?,?,?,?,?,?)',
              (cid, document, page, col_no, idx, before, after, operator, _event_at(e)))
    state = _refresh_ocr(c, document, page, page_row['version'], {col_no})
    ok = state.get(col_no, {}).get('ok', False)
    return _decision(c, e, 'corrected' if ok else 'still-mismatch',
                     'human-correction-preserves-original' if ok else 'corrected-text-not-column-number',
                     'corrected' if ok else 'mismatch',
                     {'col_no': col_no, 'correction_id': cid, 'ocr': state.get(col_no)})


def link(c, e):
    """Record a trip/box/seal association candidate.  Never per-item proof."""
    p = e.get('data', {})
    trip = p.get('trip') or p.get('qr')
    document = p.get('document') or e['subject']
    if not trip or not document:
        return _decision(c, e, 'blocked', 'trip-or-document-missing', 'blocked')
    _store_candidate(c, dict(p, trip=trip), document, int(p.get('page', 1)), trip)
    return _decision(c, e, 'candidate-linked', 'trip-qr-never-proves-per-item-column', 'candidate',
                     {'trip': trip, 'box': p.get('box'), 'seal': p.get('seal')})


# ---------------------------------------------------------------------------
# manifest / evidence / per-item attribution
# ---------------------------------------------------------------------------
def manifest_map(c, trip=None, box=None):
    sql = 'SELECT * FROM customs_artifacts WHERE 1=1'
    args = []
    if trip:
        sql += ' AND (trip=? OR trip IS NULL)'
        args.append(trip)
    if box:
        sql += ' AND (box=? OR box IS NULL)'
        args.append(box)
    return {r['id']: dict(r) for r in c.execute(sql, args)}


def _status_for(col, observed_at):
    if col is None or not column_is_valid(col, observed_at):
        return STATUS_HELD
    return STATUS_CONFIRMED if column_exports(col) else STATUS_HELD


def _complete_matchings(artifacts, columns, fixed):
    """All complete one-artifact-per-column matchings consistent with fixed
    assignments.  Used both to finish a per-item ledger by elimination and to
    present the competing matchings when attribution stays unresolved."""
    arts = list(artifacts)
    cols = list(columns)
    if len(arts) != len(cols):
        return []
    out = []
    for perm in itertools.permutations(cols):
        mapping = dict(zip(arts, perm))
        if all(mapping.get(a) == col for a, col in fixed.items()):
            out.append(mapping)
    return out


def _next_generation(c, trip, document):
    row = c.execute('SELECT MAX(version) v FROM customs_attributions WHERE trip=? AND document=?',
                    (trip, document)).fetchone()
    return int(row['v'] or 0) + 1


def effective_attribution(c, artifact, trip=None, document=None):
    sql = ("SELECT * FROM customs_attributions WHERE artifact=? AND id NOT IN "
           "(SELECT target_id FROM customs_voids WHERE target_kind='attribution')")
    args = [artifact]
    if trip:
        sql += ' AND (trip=? OR trip IS NULL)'
        args.append(trip)
    if document:
        sql += ' AND document=?'
        args.append(document)
    found = [dict(r) for r in c.execute(sql, args)]
    if not found:
        return None
    # current physical evidence outranks historical evidence, which outranks
    # legacy associations; within a rank the latest generation wins.
    found.sort(key=lambda r: (_RANK.get(r['basis'], 0), r['version'], r['created_at'], r['id']))
    return found[-1]


def _effective_map(c, trip, document):
    latest = {}
    for row in c.execute(
            "SELECT * FROM customs_attributions WHERE trip=? AND document=? AND id NOT IN "
            "(SELECT target_id FROM customs_voids WHERE target_kind='attribution') "
            "ORDER BY version,created_at,rowid", (trip, document)):
        row = dict(row)
        prior = latest.get(row['artifact'])
        key = (_RANK.get(row['basis'], 0), row['version'], row['created_at'], row['id'])
        if prior is None:
            latest[row['artifact']] = row
        else:
            pkey = (_RANK.get(prior['basis'], 0), prior['version'], prior['created_at'], prior['id'])
            if key > pkey:
                latest[row['artifact']] = row
    return latest


def _column_owners(latest):
    """col_no -> effective attribution owning it.  Conflicts are resolved by
    evidence authority; every non-owner is reported separately so downstream
    gates can hold it instead of double-clearing."""
    owners = {}
    conflicts = {}
    ordered = sorted(latest.values(),
                     key=lambda r: (-_RANK.get(r['basis'], 0), -r['version'], r['artifact']))
    for row in ordered:
        col = row['col_no']
        if not col:
            continue
        if col not in owners:
            owners[col] = row
        elif owners[col]['artifact'] != row['artifact']:
            conflicts.setdefault(row['artifact'], owners[col])
            conflicts.setdefault(owners[col]['artifact'], row)
    return owners, conflicts


def register(c, e):
    p = e.get('data', {})
    artifacts = p.get('artifacts') or []
    if not artifacts:
        return _decision(c, e, 'blocked', 'artifacts-missing', 'blocked')
    for a in artifacts:
        aid = a.get('artifact') or a.get('id')
        code = a.get('collection_code')
        if not aid or not code:
            return _decision(c, e, 'blocked', 'artifact-or-collection-code-missing', 'blocked')
        c.execute('INSERT OR REPLACE INTO customs_artifacts VALUES(?,?,?,?,?,?,?,?)',
                  (aid, code, p.get('trip'), p.get('box'), p.get('seal'), p.get('document'),
                   _event_at(e), canonical(a)))
    return _decision(c, e, 'manifested', 'collection-codes-registered', 'manifested',
                     {'trip': p.get('trip'), 'box': p.get('box'), 'artifacts': [a.get('artifact') or a.get('id') for a in artifacts]})


def _ingest_evidence(c, e, document, page, trip, box, seal, kind, assignments,
                     actor, observed_at, media=None):
    if kind not in AUTHORITATIVE_KINDS:
        return None, 'unsupported-evidence-kind'
    columns = column_map(c, document)
    if not columns:
        return None, 'no-release-columns'
    manifest = manifest_map(c, trip, box)
    # a sealing scan is bound to the physical box/seal it was taken from; an
    # unboxing recheck is bound to the same trip.  Evidence for the wrong box
    # or seal must never be accepted as proof for this sheet.
    sheet_boxes = {r['box'] for r in c.execute('SELECT box FROM customs_pages WHERE document=? AND box IS NOT NULL',
                                               (document,))}
    sheet_seals = {r['seal'] for r in c.execute('SELECT seal FROM customs_pages WHERE document=? AND seal IS NOT NULL',
                                                (document,))}
    if box and sheet_boxes and box not in sheet_boxes:
        return None, 'box-does-not-match-release-sheet'
    if seal and sheet_seals and seal not in sheet_seals:
        return None, 'seal-does-not-match-release-sheet'
    seen_artifacts = set()
    seen_columns = set()
    prepared = []
    for a in assignments:
        artifact = a.get('artifact')
        col_no = str(a.get('col_no', ''))
        code = a.get('collection_code')
        if not artifact or not col_no:
            return None, 'assignment-incomplete'
        if artifact in seen_artifacts:
            return None, 'artifact-assigned-twice'
        if col_no in seen_columns:
            return None, 'column-claimed-by-two-artifacts'
        seen_artifacts.add(artifact)
        seen_columns.add(col_no)
        col = columns.get(col_no)
        if col is None:
            return None, 'column-not-captured'
        if not column_is_valid(col, observed_at):
            return None, 'column-outside-validity'
        reg = manifest.get(artifact)
        if reg and code and reg['collection_code'] != code:
            return None, 'collection-code-mismatch'
        if not code and reg:
            code = reg['collection_code']
        if kind == 'sealing_scan' and not code:
            return None, 'sealing-scan-requires-collection-code'
        if a.get('signature_id') and col['signature_id'] != a['signature_id']:
            return None, 'signature-reference-mismatch'
        prepared.append({'artifact': artifact, 'col_no': col_no, 'collection_code': code,
                         'destination': col['destination'], 'scope': col['scope'],
                         'signature_id': col['signature_id']})
    if not prepared:
        return None, 'assignments-missing'
    media_id = media_digest = None
    if media:
        media_id, media_digest, err = _store_media(c, document, page, 1, kind, media, _received(e))
        if err:
            return None, err
    ledger = {'kind': kind, 'document': document, 'page': page, 'trip': trip,
              'box': box, 'seal': seal, 'observed_at': observed_at, 'actor': actor,
              'media': media_digest, 'assignments': prepared}
    ledger_digest = _sha(canonical(ledger).encode())
    ids = {}
    for a in prepared:
        eid = _hash_id('ev', document, trip, kind, observed_at, a['artifact'], a['col_no'])
        c.execute('INSERT OR IGNORE INTO customs_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (eid, kind, a['artifact'], a['col_no'], document, page, trip, box, seal,
                   a['collection_code'], actor, observed_at, 1,
                   canonical(dict(ledger, artifact=a['artifact'])), ledger_digest))
        ids[a['artifact']] = eid
    return {'ids': ids, 'assignments': prepared, 'digest': ledger_digest, 'columns': columns,
            'manifest': manifest, 'media_id': media_id, 'media_digest': media_digest}, None


def _sync_export_state(c, effective, columns, trip):
    """Align transit allocations with the current per-item certificate.

    An artifact that is not currently ``confirmed`` for export must not stay
    sealed/in-transit; it is intercepted.  This is one-way and only changes
    allocation state; all decisions and handoff receipts stay untouched.
    """
    for artifact, row in effective.items():
        col = columns.get(row['col_no']) if row['col_no'] else None
        allowed = (row['basis'] != LEGACY_BASIS and row['status'] == STATUS_CONFIRMED
                   and col is not None and column_is_valid(col, row['created_at'])
                   and column_exports(col))
        if allowed:
            continue
        for a in c.execute('SELECT * FROM allocations WHERE subject=?', (artifact,)):
            if a['state'] in ACTIVE_ALLOCATIONS:
                _intercept(c, artifact, row['id'], None,
                           'customs-item-not-confirmed', row['created_at'])


def _write_resolution(c, e, document, trip, kind, assignments, observed_at, evidence):
    columns = evidence['columns']
    manifest = evidence['manifest']
    artifacts = sorted(set(manifest) | {a['artifact'] for a in assignments})
    fixed = {a['artifact']: a['col_no'] for a in assignments}
    matchings = _complete_matchings(artifacts, list(columns), fixed)
    gen = _next_generation(c, trip, document)
    statuses = {}
    ids = {}
    superseded = {}
    for artifact in artifacts:
        # only an artifact actually observed in this authorized ledger may be
        # assigned a column.  A manifest-only artifact is never inferred in by
        # elimination: it stays unresolved with the competing matchings.
        col_no = fixed.get(artifact)
        alternatives = None
        if col_no:
            col = columns[col_no]
            status = _status_for(col, observed_at)
            destination = col['destination']
            scope = col['scope']
            basis = kind
        else:
            status = STATUS_UNRESOLVED
            destination = scope = None
            basis = kind
            # never choose one column by page order/upload time: keep every
            # complete matching as a competing alternative.
            alternatives = canonical(matchings or [dict(fixed)])
        prior = effective_attribution(c, artifact, trip, document)
        superseded[artifact] = dict(prior) if prior else None
        att_id = _hash_id('att', document, trip, gen, artifact)
        c.execute('INSERT OR IGNORE INTO customs_attributions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (att_id, trip, document, artifact, col_no or None, status, basis,
                   evidence['ids'].get(artifact), alternatives, destination, scope, gen,
                   prior['id'] if prior else None, 1, observed_at))
        statuses[artifact] = status
        ids[artifact] = att_id

    # an artifact that loses a previously export-eligible designation must not
    # stay sealed/in-transit: intercept active allocations (never rewrite the
    # already-issued escort decisions themselves)
    intercepts = []
    for artifact in artifacts:
        if statuses[artifact] == STATUS_CONFIRMED:
            continue
        old = superseded.get(artifact)
        was_export = bool(old and old['col_no'] and old['basis'] != LEGACY_BASIS
                          and _status_for(columns.get(old['col_no']), old['created_at']) == STATUS_CONFIRMED)
        if was_export:
            intercepts.extend({'artifact': artifact, **x} for x in
                              _intercept(c, artifact, ids[artifact], None,
                                         'authoritative-evidence-revokes-export', _event_at(e)))

    # append-only void chain, so stale provisional rows cannot keep a release
    # column occupied while claims are rebuilt
    voids, void_intercepts = _void_legacy(c, e, trip, document, kind)
    intercepts.extend(void_intercepts)

    # rebuild the per-column claim lock from the effective attribution view.
    # A column is claimed by at most one artifact; the highest-authority
    # evidence wins (unboxing recheck > sealing scan > legacy association).
    c.execute('DELETE FROM customs_column_claims WHERE trip=? AND document=?', (trip, document))
    effective = _effective_map(c, trip, document)
    ordered = sorted(effective.values(),
                     key=lambda r: (-_RANK.get(r['basis'], 0), -r['version'], r['artifact']))
    used = set()
    for row in ordered:
        if not row['col_no'] or row['col_no'] in used:
            continue
        used.add(row['col_no'])
        c.execute('INSERT INTO customs_column_claims VALUES(?,?,?,?,?,?)',
                  (trip, document, row['col_no'], row['artifact'], row['id'], observed_at))
    _sync_export_state(c, effective, columns, trip)

    return {'generation': gen, 'attributions': ids, 'statuses': statuses,
            'mapping': dict(fixed), 'voids': voids, 'intercepts': intercepts,
            'alternatives': matchings if len(matchings) > 1 else []}


def attribute(c, e):
    """Ingest an authoritative per-item ledger (sealing scan or unboxing
    recheck), rebuild the artifact->column attribution and reverse any legacy
    association it contradicts."""
    p = e.get('data', {})
    document = p.get('document')
    trip = p.get('trip')
    kind = p.get('kind')
    assignments = p.get('assignments') or []
    observed_at = int(p.get('observed_at', _event_at(e)))
    if not document or not trip:
        return _decision(c, e, 'blocked', 'document-or-trip-missing', 'blocked')
    if kind not in AUTHORITATIVE_KINDS:
        return _decision(c, e, 'blocked', 'unsupported-evidence-kind', 'blocked')
    existing = c.execute('SELECT id FROM customs_attributions WHERE trip=? AND document=? AND basis=? AND created_at=? LIMIT 1',
                         (trip, document, kind, observed_at)).fetchone()
    if existing:
        # retry after an interrupted write: the resolution for this exact
        # evidence time already committed; never mint a second attribution or
        # let a second artifact grab the same release column.
        return _decision(c, e, 'idempotent', 'attribution-generation-already-committed', 'attributed')
    evidence, err = _ingest_evidence(c, e, document, int(p.get('page', 1)), trip,
                                     p.get('box'), p.get('seal'), kind, assignments,
                                     p.get('actor') or e['subject'], observed_at,
                                     media=p.get('media'))
    if err:
        return _decision(c, e, 'blocked', err, 'blocked')
    resolution = _write_resolution(c, e, document, trip, kind, evidence['assignments'],
                                   observed_at, evidence)
    outcome = 'attributed' if not resolution['voids'] else 'attributed-with-voids'
    state = 'confirmed' if any(v == STATUS_CONFIRMED for v in resolution['statuses'].values()) else 'unresolved'
    return _decision(c, e, outcome, 'per-item-evidence-versioned', state,
                     dict(resolution, evidence_digest=evidence['digest']))


def legacy_associate(c, e):
    """Reproduce the old RPA defect: a trip-level QR is used to bind an
    artifact to a column.  Recorded as provisional and never treated as a
    per-item proof by the new gates."""
    p = e.get('data', {})
    document = p.get('document')
    trip = p.get('trip')
    artifact = p.get('artifact') or e['subject']
    col_no = str(p.get('col_no', ''))
    if not document or not trip or not col_no:
        return _decision(c, e, 'blocked', 'legacy-link-incomplete', 'blocked')
    col = column_map(c, document).get(col_no)
    if col is None:
        return _decision(c, e, 'blocked', 'column-not-captured', 'blocked')
    gen = _next_generation(c, trip, document)
    prior = effective_attribution(c, artifact, trip, document)
    att_id = _hash_id('att', document, trip, gen, artifact, 'legacy')
    c.execute('INSERT OR IGNORE INTO customs_attributions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (att_id, trip, document, artifact, col_no, STATUS_PROVISIONAL, LEGACY_BASIS,
               None, None, col['destination'], col['scope'], gen,
               prior['id'] if prior else None, 1, _event_at(e)))
    c.execute('DELETE FROM customs_column_claims WHERE trip=? AND document=? AND col_no=?',
              (trip, document, col_no))
    c.execute('INSERT INTO customs_column_claims VALUES(?,?,?,?,?,?)',
              (trip, document, col_no, artifact, att_id, _event_at(e)))
    return _decision(c, e, 'legacy-associated', 'provisional-never-per-item-proof', 'provisional',
                     {'artifact': artifact, 'col_no': col_no, 'attribution_id': att_id,
                      'destination': col['destination']})


def _intercept(c, artifact, attribution_id, void_id, reason, at):
    found = []
    for alloc in c.execute('SELECT * FROM allocations WHERE subject=?', (artifact,)):
        if alloc['state'] not in ACTIVE_ALLOCATIONS:
            continue
        iid = _hash_id('int', artifact, alloc['id'], attribution_id)
        c.execute('INSERT OR IGNORE INTO customs_intercepts VALUES(?,?,?,?,?,?,?,?)',
                  (iid, artifact, alloc['id'], attribution_id, void_id, at, reason,
                   canonical({'allocation': alloc['id'], 'prior_state': alloc['state']})))
        c.execute("UPDATE allocations SET state='intercept' WHERE id=?", (alloc['id'],))
        found.append({'allocation': alloc['id'], 'prior_state': alloc['state'],
                      'intercept_id': iid})
    return found


def _void_legacy(c, e, trip, document, trigger):
    """Append reverse-void records for provisional legacy associations that the
    current authoritative attribution contradicts.  Old rows and old escort
    decisions are never rewritten; active allocations are intercepted."""
    latest = _effective_map(c, trip, document)
    voided_ids = {r['target_id'] for r in c.execute(
        "SELECT target_id FROM customs_voids WHERE target_kind='attribution'")}
    columns = column_map(c, document)
    now = _event_at(e)
    voids, intercepts = [], []
    legacy_rows = c.execute(
        "SELECT * FROM customs_attributions WHERE trip=? AND document=? AND basis=? "
        "ORDER BY version,created_at,rowid", (trip, document, LEGACY_BASIS)).fetchall()
    for row in legacy_rows:
        if row['id'] in voided_ids:
            continue
        new = latest.get(row['artifact'])
        if new is None or new['basis'] == LEGACY_BASIS:
            # no authoritative per-item evidence yet: a late page, OCR rerun
            # or declaration correction may have stripped the column of its
            # export validity.  If so the legacy association is now
            # demonstrably erroneous even without new per-item evidence.
            col = columns.get(row['col_no'])
            if col is None or not column_exports(col) or not column_is_valid(col, now):
                reason = 'column-no-longer-export-eligible'
            else:
                continue
        elif new['status'] == STATUS_UNRESOLVED:
            reason = 'authoritative-evidence-leaves-artifact-unresolved'
        elif new['col_no'] != row['col_no']:
            reason = 'authoritative-evidence-assigns-different-column'
        elif new['status'] != STATUS_CONFIRMED:
            reason = 'authoritative-evidence-denies-export'
        elif any(other['col_no'] == row['col_no'] and other['artifact'] != row['artifact']
                 and other['basis'] != LEGACY_BASIS for other in latest.values()):
            reason = 'column-reassigned-to-another-artifact'
        else:
            continue
        vid = _hash_id('void', row['id'], trigger)
        c.execute('INSERT OR IGNORE INTO customs_voids VALUES(?,?,?,?,?,?,?,?)',
                  (vid, row['artifact'], 'attribution', row['id'], reason,
                   trigger, now, canonical({'attribution': row['id'], 'column': row['col_no']})))
        # a voided provisional claim must not keep occupying the release column
        c.execute('DELETE FROM customs_column_claims WHERE attribution_id=? AND artifact=?',
                  (row['id'], row['artifact']))
        voids.append({'artifact': row['artifact'], 'attribution_id': row['id'],
                      'void_id': vid, 'reason': reason, 'column': row['col_no']})
        intercepts.extend({'artifact': row['artifact'], **x}
                          for x in _intercept(c, row['artifact'], row['id'], vid,
                                              reason, now))
    return voids, intercepts


def reconcile(c, e):
    """Re-run legacy contradiction detection when a late page, OCR rerun or
    declaration correction changes the picture.  No new evidence is invented."""
    p = e.get('data', {})
    document = p.get('document') or e['subject']
    trip = p.get('trip')
    if not document or not trip:
        return _decision(c, e, 'blocked', 'document-or-trip-missing', 'blocked')
    voids, intercepts = _void_legacy(c, e, trip, document, p.get('trigger', 'reconcile'))
    return _decision(c, e, 'reconciled' if not voids else 'reconciled-with-voids',
                     'legacy-contradiction-checked', 'reconciled',
                     {'voids': voids, 'intercepts': intercepts})


# ---------------------------------------------------------------------------
# per-item resolution, certificate package and downstream release gate
# ---------------------------------------------------------------------------
def _documents_for(c, trip=None, box=None, seal=None, document=None):
    if document:
        return [document]
    docs = set()
    if trip:
        docs |= {r['document'] for r in c.execute('SELECT DISTINCT document FROM customs_pages WHERE trip=?', (trip,))}
        docs |= {r['document'] for r in c.execute('SELECT DISTINCT document FROM customs_candidates WHERE trip=?', (trip,))}
    if box:
        docs |= {r['document'] for r in c.execute('SELECT DISTINCT document FROM customs_pages WHERE box=?', (box,))}
    if seal:
        docs |= {r['document'] for r in c.execute('SELECT DISTINCT document FROM customs_pages WHERE seal=?', (seal,))}
    return sorted(docs)


def _artifact_known(c, artifact, documents, trip):
    if c.execute('SELECT 1 FROM customs_artifacts WHERE id=? AND (trip=? OR trip IS NULL)',
                 (artifact, trip)).fetchone():
        return True
    for doc in documents:
        if effective_attribution(c, artifact, trip, doc):
            return True
    return False


def release_check(c, artifact, trip=None, box=None, seal=None, document=None):
    """Downstream gate consumed by loan/transit.

    A captured customs sheet is not proof of per-item attribution: only a
    ``confirmed`` per-item certificate permits export.  ``provisional`` legacy
    associations are history, never a release basis.  Artifacts absent from the
    sheet's manifest are not affected by it (no qualification contagion), and
    one release column can never clear two artifacts.
    """
    docs = _documents_for(c, trip, box, seal, document)
    if not docs:
        return {'required': False, 'allowed': True, 'status': 'no-customs-evidence', 'documents': []}
    if not _artifact_known(c, artifact, docs, trip):
        return {'required': False, 'allowed': True, 'status': 'not-on-customs-sheet', 'documents': docs}
    intercepted = c.execute('SELECT * FROM customs_intercepts WHERE artifact=?', (artifact,)).fetchone()
    results = []
    for doc in docs:
        latest = _effective_map(c, trip, doc)
        row = latest.get(artifact)
        if row is None or row['basis'] == LEGACY_BASIS:
            continue
        owners, conflicts = _column_owners(latest)
        status = row['status']
        base = {'required': True, 'documents': docs, 'attribution_id': row['id'],
                'column': row['col_no'], 'destination': row['destination'], 'scope': row['scope']}
        if row['artifact'] in conflicts or (row['col_no'] and
                                            owners.get(row['col_no'], {}).get('artifact') != artifact):
            rival = conflicts.get(artifact) or owners.get(row['col_no'])
            return dict(base, allowed=False, status=STATUS_HELD,
                        reason='column-claimed-by-other-artifact',
                        claimed_by=rival['artifact'] if rival else None,
                        intercept=bool(intercepted))
        cols = column_map(c, doc)
        col = cols.get(row['col_no'])
        if status == STATUS_CONFIRMED and col is not None and column_is_valid(col, row['created_at']):
            results.append(dict(base, allowed=True, status=STATUS_CONFIRMED, intercept=bool(intercepted)))
        else:
            results.append(dict(base, allowed=False, status=status, intercept=bool(intercepted)))
    if not results:
        return {'required': True, 'allowed': False, 'status': STATUS_UNRESOLVED,
                'documents': docs, 'intercept': bool(intercepted),
                'reason': 'no-per-item-attribution'}
    if all(r['allowed'] for r in results):
        return results[0]
    return results[0]


def _observed_status(row, columns):
    if row is None:
        return STATUS_UNRESOLVED
    if row['basis'] == LEGACY_BASIS:
        return STATUS_UNRESOLVED
    if row['status'] == STATUS_UNRESOLVED:
        return STATUS_UNRESOLVED
    col = columns.get(row['col_no'])
    if col is None:
        return STATUS_UNRESOLVED
    return _status_for(col, row['created_at'])


def resolution(c, trip, document):
    """Current per-artifact attribution view, including competing complete
    matchings when attribution cannot be decided."""
    columns = column_map(c, document)
    manifest = manifest_map(c, trip)
    latest = _effective_map(c, trip, document)
    artifacts = sorted(set(manifest) | set(latest))
    matchings = _complete_matchings(artifacts, list(columns), {})
    items = {}
    for artifact in artifacts:
        row = latest.get(artifact)
        status = _observed_status(row, columns)
        alternatives = None
        if status == STATUS_UNRESOLVED:
            alts = [m for m in matchings if m.get(artifact)] or matchings
            alternatives = [dict(m) for m in alts]
        assigned_col = row['col_no'] if row and status != STATUS_UNRESOLVED else None
        items[artifact] = {
            'artifact': artifact,
            'status': status,
            'column': assigned_col,
            'destination': columns[assigned_col]['destination'] if assigned_col in columns else None,
            'scope': columns[assigned_col]['scope'] if assigned_col in columns else None,
            'basis': row['basis'] if row else None,
            'attribution_id': row['id'] if row else None,
            'collection_code': manifest.get(artifact, {}).get('collection_code'),
            'alternatives': alternatives,
        }
    unresolved = [a for a, v in items.items() if v['status'] == STATUS_UNRESOLVED]
    return {'trip': trip, 'document': document,
            'columns': {k: {'destination': v['destination'], 'scope': v['scope'],
                            'valid_from': v['valid_from'], 'valid_to': v['valid_to'],
                            'export_eligible': column_exports(v)} for k, v in columns.items()},
            'manifest': {a: manifest[a]['collection_code'] for a in manifest},
            'items': items,
            'complete_matchings': [dict(m) for m in matchings],
            'unresolved': unresolved,
            'missing_originals': _missing_originals(c, trip, document, columns, items)}


def _missing_originals(c, trip, document, columns, items):
    missing = []
    authoritative = c.execute(
        'SELECT 1 FROM customs_evidence WHERE trip=? AND document=? AND kind IN (?,?) LIMIT 1',
        (trip, document, *AUTHORITATIVE_KINDS)).fetchone()
    if not authoritative:
        missing.append('sealing-time-signed-per-item-scan-ledger')
        missing.append('customs-independent-unboxing-recheck')
    for page in latest_pages(c, document):
        if not page['media_id']:
            missing.append('original-release-sheet-image:page-%d' % page['page'])
    for col_no, col in sorted(columns.items()):
        if not col['signature_id']:
            missing.append('customs-seal-for-column:%s' % col_no)
        if not col.get('ocr_ok'):
            missing.append('corrected-piece-number-cell:%s' % col_no)
    manifest = manifest_map(c, trip)
    for artifact in sorted(items):
        if not manifest.get(artifact, {}).get('collection_code'):
            missing.append('artifact-collection-code:%s' % artifact)
        if not c.execute('SELECT 1 FROM customs_evidence WHERE artifact=? AND valid=1',
                         (artifact,)).fetchone():
            missing.append('per-item-evidence-for:%s' % artifact)
    return sorted(set(missing))


def package(c, artifact, trip, document):
    """Traceable customs certificate package for one artifact."""
    columns = column_map(c, document)
    manifest = manifest_map(c, trip)
    latest = _effective_map(c, trip, document)
    row = latest.get(artifact)
    status = _observed_status(row, columns)
    chain = [dict(r) for r in c.execute(
        'SELECT * FROM customs_attributions WHERE artifact=? AND trip=? AND document=? '
        'ORDER BY version,created_at,rowid', (artifact, trip, document))]
    voids = [dict(r) for r in c.execute('SELECT * FROM customs_voids WHERE artifact=? ORDER BY at,id',
                                        (artifact,))]
    intercepts = [dict(r) for r in c.execute('SELECT * FROM customs_intercepts WHERE artifact=? ORDER BY at,id',
                                             (artifact,))]
    evidence = [dict(r) for r in c.execute(
        'SELECT id,kind,col_no,document,page,trip,box,seal,collection_code,actor,at,valid,digest '
        'FROM customs_evidence WHERE artifact=? ORDER BY at,id', (artifact,))]
    media = [dict(r) for r in c.execute(
        'SELECT id,document,page,version,kind,mime,digest,received_at FROM customs_media '
        'WHERE document=? ORDER BY page,version,id', (document,))]
    pages = [dict(r) for r in c.execute(
        'SELECT id,document,page,version,kind,trip,box,seal,export_country,permitted,digest,media_id,received_at '
        'FROM customs_pages WHERE document=? ORDER BY page,version', (document,))]
    signatures = [dict(r) for r in c.execute(
        'SELECT id,document,page,version,col_no,seal_id,digest,x,y,w,h,valid FROM customs_signatures '
        'WHERE document=? ORDER BY page,version,col_no', (document,))]
    corrections = [dict(r) for r in c.execute(
        'SELECT * FROM customs_corrections WHERE document=? ORDER BY page,col_no,idx,at,id', (document,))]
    tokens = [dict(r) for r in c.execute(
        'SELECT document,page,version,rev,col_no,idx,ch,x,y,w,h,confidence,source '
        'FROM customs_tokens WHERE document=? ORDER BY page,version,rev,col_no,source,idx', (document,))]
    matchings = _complete_matchings(sorted(set(manifest) | set(latest)), list(columns), {})
    alternatives = None
    if status == STATUS_UNRESOLVED:
        alts = [m for m in matchings if m.get(artifact)] or matchings
        alternatives = [dict(m) for m in alts]
    payload = {
        'artifact': artifact, 'trip': trip, 'document': document,
        'status': status,
        'column': row['col_no'] if row and status != STATUS_UNRESOLVED else None,
        'destination': columns[row['col_no']]['destination'] if row and row['col_no'] in columns else None,
        'scope': columns[row['col_no']]['scope'] if row and row['col_no'] in columns else None,
        'collection_code': manifest.get(artifact, {}).get('collection_code'),
        'basis': row['basis'] if row else None,
        'attribution_id': row['id'] if row else None,
        'alternatives': alternatives,
        'attributions': chain,
        'evidence': evidence,
        'voids': voids,
        'intercepts': intercepts,
        'originals': {'media': media, 'pages': pages, 'signatures': signatures,
                      'tokens': tokens, 'corrections': corrections},
        'missing_originals': _missing_originals(c, trip, document, columns,
                                                {artifact: {'status': status}}),
    }
    payload['digest'] = _sha(canonical(payload).encode())
    payload['limitations'] = _limitations(c, trip, document, row, status)
    return payload


def _limitations(c, trip, document, row, status):
    notes = []
    if row and row['basis'] == 'sealing_scan':
        notes.append('sealing-scan-confirms-historical-boxing-not-current-box-contents')
    if row and row['basis'] == 'unboxing_recheck':
        notes.append('unboxing-recheck-confirms-current-physical-artifact-not-sealing-time-scan-order')
    if status == STATUS_UNRESOLVED:
        notes.append('competing-complete-matchings-retained-no-column-favoured')
    if status == STATUS_PROVISIONAL:
        notes.append('legacy-association-is-not-per-item-proof')
    if row and c.execute("SELECT 1 FROM customs_voids WHERE artifact=? AND target_kind='attribution'",
                         (row['artifact'],)).fetchone():
        notes.append('prior-association-voided-see-void-chain')
    return notes


def certificates(c, trip, document):
    res = resolution(c, trip, document)
    return {'trip': trip, 'document': document,
            'certificates': {a: package(c, a, trip, document) for a in sorted(res['items'])},
            'resolution': res}


def _maybe_reconcile(c, e, document, trip):
    if not document or not trip:
        return [], []
    return _void_legacy(c, e, trip, document, e['action'])


# ---------------------------------------------------------------------------
# action dispatch
# ---------------------------------------------------------------------------
def certificate(c, e):
    """Read-only per-artifact certificate package as a decision record."""
    p = e.get('data', {})
    document = p.get('document') or e['subject']
    trip = p.get('trip')
    artifact = p.get('artifact')
    if not trip or not artifact:
        return _decision(c, e, 'blocked', 'trip-or-artifact-missing', 'blocked')
    pkg = package(c, artifact, trip, document)
    return _decision(c, e, 'certificate', 'per-item-package-materialized', pkg['status'], pkg)


def report_resolution(c, e):
    p = e.get('data', {})
    document = p.get('document') or e['subject']
    trip = p.get('trip')
    if not trip:
        return _decision(c, e, 'blocked', 'trip-missing', 'blocked')
    res = resolution(c, trip, document)
    return _decision(c, e, 'resolution', 'per-item-attribution-view', 'resolution', res)


def certificates_report(c, e):
    p = e.get('data', {})
    document = p.get('document') or e['subject']
    trip = p.get('trip')
    if not trip:
        return _decision(c, e, 'blocked', 'trip-missing', 'blocked')
    report = certificates(c, trip, document)
    return _decision(c, e, 'certificates', 'per-artifact-packages-materialized', 'certificates', report)


HANDLERS = {
    'capture': capture,
    'ocr': ocr,
    'correct': correct,
    'link': link,
    'register': register,
    'attribute': attribute,
    'legacy_associate': legacy_associate,
    'reconcile': reconcile,
    'resolve': report_resolution,
    'certificate': certificate,
    'certificates': certificates_report,
}


def fixture_events(fx, evidence='sealing_scan', legacy=False):
    """Expand a scenario fixture (e.g. ``fixtures/customs-columns.json``) into a
    replayable event stream.  ``evidence`` selects which authoritative path is
    used for the per-item list: ``sealing_scan`` or ``unboxing_recheck``.
    A trip-level QR is only ever submitted as a candidate, never as proof."""
    trip = fx['trip']
    document = fx['document']
    columns = [{k: v for k, v in col.items() if k != 'ocr_tokens'} for col in fx['columns']]
    tokens = [t for col in fx['columns'] for t in col.get('ocr_tokens', [])]
    events = [
        {'subsystem': 'customs', 'event': {
            'id': 'customs-manifest', 'subject': document, 'action': 'register',
            'data': fx['manifest'], 'effective_at': 1, 'received_at': 1}},
        {'subsystem': 'customs', 'event': {
            'id': 'customs-capture-1', 'subject': document, 'action': 'capture',
            'data': {'document': document, 'page': 1, 'trip': trip, 'box': fx['box'],
                     'seal': fx['seal'], 'export_country': fx.get('export_country', 'A'),
                     'columns': columns, 'tokens': tokens,
                     'permitted_destinations': fx['columns'][0]['permitted_destinations'],
                     'media': {'mime': 'image/png', 'bytes_b64': 'ZGVtby1pbWFnZQ=='},
                     'candidates': [fx['qr']]},
            'effective_at': 2, 'received_at': 2}},
    ]
    if legacy:
        events.append({'subsystem': 'customs', 'event': {
            'id': 'customs-legacy', 'subject': document, 'action': 'legacy_associate',
            'data': {'document': document, 'trip': trip, 'artifact': 'artifact-1', 'col_no': 'X-31'},
            'effective_at': 3, 'received_at': 3}})
    if evidence == 'sealing_scan':
        rows = [{'artifact': r['artifact'], 'collection_code': r['collection_code'],
                 'col_no': r['col_no']} for r in fx['seal_scan']['events']]
        events.append({'subsystem': 'customs', 'event': {
            'id': 'customs-sealing-scan', 'subject': document, 'action': 'attribute',
            'data': {'document': document, 'trip': trip, 'box': fx['box'], 'seal': fx['seal'],
                     'kind': 'sealing_scan', 'observed_at': 4, 'assignments': rows,
                     'media': fx['seal_scan'].get('media'),
                     'actor': 'lender-registrar'},
            'effective_at': 4, 'received_at': 4}})
    elif evidence == 'unboxing_recheck':
        rows = [{'artifact': r['artifact'], 'collection_code': r['engraved_code'],
                 'col_no': r['col_no']} for r in fx['unboxing_recheck']['rows']]
        events.append({'subsystem': 'customs', 'event': {
            'id': 'customs-unboxing-recheck', 'subject': document, 'action': 'attribute',
            'data': {'document': document, 'trip': trip, 'box': fx['box'], 'seal': fx['seal'],
                     'kind': 'unboxing_recheck',
                     'observed_at': fx['unboxing_recheck']['observed_at'],
                     'assignments': rows, 'actor': fx['unboxing_recheck']['actor']},
            'effective_at': 6, 'received_at': 6}})
    return events


def apply(c, e):
    action = e.get('action')
    handler = HANDLERS.get(action)
    if handler is None:
        return _decision(c, e, 'blocked', 'unknown-action', 'blocked')
    if action in ('capture', 'ocr', 'correct'):
        result = handler(c, e)
        if result.get('outcome') in ('captured', 'captured-with-ocr-mismatch', 'ocr-reread', 'ocr-mismatch',
                                     'corrected', 'still-mismatch'):
            p = e.get('data', {})
            voids, intercepts = _maybe_reconcile(c, e, p.get('document') or e['subject'],
                                                 p.get('trip') or p.get('qr'))
            if voids:
                result['data'] = dict(result.get('data') or {}, voids=voids, intercepts=intercepts)
        return result
    return handler(c, e)

