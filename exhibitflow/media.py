"""Media lineage: pixel-level source attribution for licensed exhibition images.

Every published file is traced back to the set of source pixels it covers on
the archived original scan.  The size/period gate (permit_render) is necessary
but not sufficient: a render scaled down to the licensed width can still
sample an unlicensed region of the original.  This module keeps, as
replayable events, the archived original's byte digest, the derivation chain
(crop / rotate / scale / montage layers) with CMS versions, lender-signed
region licenses and their revocation pages, and the actual CDN files.  For
each public file and day it computes whether the file is publishable, waiting
on a supplemental license, or must be taken down -- together with the
out-of-bounds source pixel area -- jointly constrained by permit_render and
the loan period written by loan.authorize.
"""
import json, math
from . import storage as s
from . import loan

NAME = '媒体谱系'

# ---------------------------------------------------------------- geometry
# Each transform op maps output coordinates back to its input (parent)
# coordinates as an affine matrix (a,b,c,d,e,f):
#     x_parent = a*x + b*y + c
#     y_parent = d*x + e*y + f
# Chaining the ops of a derivative therefore yields the pre-image of the
# output rectangle on the original scan, for any crop/rotate/scale pipeline.

def _affine(op):
    kind = op['op']
    if kind == 'crop':
        x, y, w, h = (float(v) for v in op['rect'])
        return (1.0, 0.0, x, 0.0, 1.0, y)
    if kind == 'scale':
        x, y, w, h = (float(v) for v in op['from'])
        cw, ch = (float(v) for v in op['size'])
        return (w / cw, 0.0, x, 0.0, h / ch, y)
    if kind == 'rotate':
        theta = math.radians(float(op['degrees']))
        cx, cy = (float(v) for v in op['center'])
        a = math.cos(theta); b = math.sin(theta); d = -b; e = a
        return (a, b, cx - a * cx - b * cy, d, e, cy - d * cx - e * cy)
    raise ValueError('unknown-op')

def _compose(outer, inner):
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (a1 * a2 + b1 * d2, a1 * b2 + b1 * e2, a1 * c2 + b1 * f2 + c1,
            d1 * a2 + e1 * d2, d1 * b2 + e1 * e2, d1 * c2 + e1 * f2 + f1)

def chain_matrix(ops):
    """Compose the output->original affine map for a derivation chain."""
    m = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    for op in ops:  # each later op maps one stage further back towards the original
        m = _compose(m, _affine(op))
    return m

def _apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + b * y + c, d * x + e * y + f)

def preimage_quad(ops, width, height):
    """Pre-image of the output rectangle on the original scan (4 corners)."""
    m = chain_matrix(ops)
    return [_apply(m, x, y) for (x, y) in ((0, 0), (width, 0), (width, height), (0, height))]

def polygon_area(poly):
    return abs(sum(x1 * y2 - x2 * y1
                   for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]))) / 2.0

def clip_to_rect(poly, rect):
    """Sutherland-Hodgman clip of a convex polygon against an axis-aligned rect."""
    rx, ry, rw, rh = (float(v) for v in rect)
    def clip(poly, inside, intersect):
        out = []
        for p, q in zip(poly, poly[1:] + poly[:1]):
            if inside(q):
                if not inside(p):
                    out.append(intersect(p, q))
                out.append(q)
            elif inside(p):
                out.append(intersect(p, q))
        return out
    def ix(p, q, x):
        t = (x - p[0]) / (q[0] - p[0])
        return (x, p[1] + t * (q[1] - p[1]))
    def iy(p, q, y):
        t = (y - p[1]) / (q[1] - p[1])
        return (p[0] + t * (q[0] - p[0]), y)
    poly = clip(poly, lambda p: p[0] >= rx, lambda p, q: ix(p, q, rx))
    poly = clip(poly, lambda p: p[0] <= rx + rw, lambda p, q: ix(p, q, rx + rw))
    poly = clip(poly, lambda p: p[1] >= ry, lambda p, q: iy(p, q, ry))
    poly = clip(poly, lambda p: p[1] <= ry + rh, lambda p, q: iy(p, q, ry + rh))
    return poly

# ---------------------------------------------------------------- baseline

def permit_render(render, license_record):
    """Return whether the rendered image fits the stored size and time window."""
    return (render['artifact'] == license_record['artifact']
            and render['width'] <= license_record['max_width']
            and license_record['start'] <= render['day'] <= license_record['end'])

# ---------------------------------------------------------------- event apply

def _j(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True)

def apply(c, e):
    a = e['action']; p = e.get('data', {}); day = int(e.get('effective_at', 0))
    if a == 'archive_original':
        c.execute('INSERT OR REPLACE INTO media_originals VALUES(?,?,?,?,?,?)',
                  (p['original'], p['artifact'], int(p['width']), int(p['height']), p['sha256'], day))
        return s.decide(c, e, NAME, 'archived', 'original-bytes-digested', 'archived')
    if a == 'register_asset':
        c.execute('INSERT OR REPLACE INTO media_assets VALUES(?,?,?,?,?)',
                  (p['asset'], p['kind'], int(p['start']), int(p['end']), p.get('note', '')))
        return s.decide(c, e, NAME, 'asset-registered', 'asset-license-window-recorded', 'registered')
    if a == 'sign_license':
        c.execute('INSERT OR REPLACE INTO media_licenses VALUES(?,?,?,?,?,?,?,?,?)',
                  (p['license'], p['artifact'], _j(p['region']), int(p['max_width']),
                   int(p['start']), int(p['end']), p.get('signer', ''), p.get('page', ''),
                   int(p.get('version', 1))))
        fee = int(p.get('fee', 0))
        if fee:
            c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',
                      (e['id'], p['artifact'], 'license-fee', f"{p['license']}:{p['start']}:{p['end']}", fee))
        return s.decide(c, e, NAME, 'license-signed', 'region-license-recorded', 'signed')
    if a == 'revoke_license':
        c.execute('INSERT INTO media_license_revocations VALUES(?,?,?,?)',
                  (e['id'], p['license'], day, p.get('page', '')))
        return s.decide(c, e, NAME, 'license-revoked', 'revocation-page-recorded', 'revoked')
    if a == 'register_derivative':
        version = int(p['cms_version'])
        known = c.execute('SELECT 1 FROM media_derivatives WHERE id=? AND cms_version=?',
                          (p['derivative'], version)).fetchone()
        if known:
            return s.decide(c, e, NAME, 'blocked', 'cms-version-conflict', 'blocked')
        c.execute('INSERT INTO media_derivatives VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  (p['derivative'], version, p['artifact'], p.get('original', ''),
                   p.get('original_sha256', ''), _j(p.get('ops', [])), _j(p.get('layers', [])),
                   _j(p.get('assets', [])), int(p['width']), int(p['height']), p['sha256'], day))
        return s.decide(c, e, NAME, 'registered', 'derivation-chain-recorded', 'registered')
    if a == 'publish':
        der = c.execute('SELECT * FROM media_derivatives WHERE id=? AND cms_version=?',
                        (p['derivative'], int(p['cms_version']))).fetchone()
        if not der:
            return s.decide(c, e, NAME, 'blocked', 'unknown-derivative', 'blocked')
        if p.get('cdn_sha256') != der['sha256']:
            return s.decide(c, e, NAME, 'blocked', 'cdn-hash-mismatch', 'blocked')
        row = c.execute('SELECT MAX(version) v FROM media_publications WHERE id=?',
                        (p['publication'],)).fetchone()
        version = (row['v'] or 0) + 1
        if version > 1:  # never silently overwrite the old URL occupant
            c.execute('UPDATE media_publications SET superseded_day=? WHERE id=? AND version=? AND superseded_day IS NULL',
                      (day, p['publication'], version - 1))
        c.execute('INSERT INTO media_publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (p['publication'], version, p['derivative'], der['cms_version'], p['url'],
                   p['cdn_sha256'], day, None, None, None, 'live'))
        return s.decide(c, e, NAME, 'published', 'cdn-file-recorded', 'live')
    if a == 'takedown':
        rows = c.execute('SELECT * FROM media_publications WHERE id=? AND takedown_day IS NULL',
                         (p['publication'],)).fetchall()
        if not rows:
            return s.decide(c, e, NAME, 'blocked', 'not-live', 'blocked')
        purge = int(p.get('cdn_purge_day', day))
        for r in rows:  # the CDN cache keeps serving the file until its purge day
            c.execute('UPDATE media_publications SET takedown_day=?, cdn_purge_day=? WHERE id=? AND version=?',
                      (day, purge, r['id'], r['version']))
        return s.decide(c, e, NAME, 'takedown', 'cdn-purge-scheduled', 'taken-down')
    return s.decide(c, e, NAME, 'blocked', 'unknown-action', 'blocked')

def ingest(c, event):
    """Record a media event and apply it in one transaction; retries are idempotent."""
    with c:
        if not s.record(c, NAME, event):
            return {'event': event['id'], 'outcome': 'idempotent'}
        return apply(c, event)

MEDIA_TABLES = ('media_originals', 'media_derivatives', 'media_publications',
                'media_licenses', 'media_license_revocations', 'media_assets')

def recompute(c):
    """Rebuild media state from the recorded events, ordered by effective day.

    Heals domain rows lost to failed database writes (the events survive) and
    places late-arriving revocations at their effective day, so recomputation
    yields the same historical exposure and current disposition for every
    public file regardless of arrival order.
    """
    with c:
        for table in MEDIA_TABLES:
            c.execute(f'DELETE FROM {table}')
        c.execute('DELETE FROM decisions WHERE subsystem=?', (NAME,))
        c.execute("DELETE FROM receipts WHERE kind='license-fee'")
        rows = c.execute("SELECT payload FROM events WHERE subsystem=? ORDER BY effective_at, received_at, id",
                         (NAME,)).fetchall()
        for row in rows:
            apply(c, json.loads(row['payload']))

def snapshot(c):
    return {t: [dict(r) for r in c.execute(f'SELECT * FROM {t} ORDER BY 1, 2').fetchall()]
            for t in MEDIA_TABLES}

# ---------------------------------------------------------------- lineage

def _derivative(c, der_id, cms_version=None):
    if cms_version is None:
        return c.execute('SELECT * FROM media_derivatives WHERE id=? ORDER BY cms_version DESC LIMIT 1',
                         (der_id,)).fetchone()
    return c.execute('SELECT * FROM media_derivatives WHERE id=? AND cms_version=?',
                     (der_id, cms_version)).fetchone()

def _verified(c, row):
    """A derivative is verified when its claimed original digest matches the archive."""
    layers = json.loads(row['layers'] or '[]')
    if layers:
        for lay in layers:
            sub = _derivative(c, lay['derivative'], lay.get('cms_version'))
            if sub is None or not _verified(c, sub):
                return False
        return True
    orig = c.execute('SELECT * FROM media_originals WHERE id=?', (row['original_id'],)).fetchone()
    return bool(orig and orig['sha256'] == row['original_sha256']
                and orig['artifact'] == row['artifact'])

def coverage(c, der_id, cms_version=None, prefix=None):
    """Pixel coverage of a derivative on the original scan, per source layer.

    Montage layers keep their own attribution: a layer scaled down to a small
    tile still reports the full source footprint it samples on the original.
    """
    row = _derivative(c, der_id, cms_version)
    if row is None:
        return None
    key = prefix or row['id']
    layers = json.loads(row['layers'] or '[]')
    assets = list(json.loads(row['assets'] or '[]'))
    if layers:
        merged = {}; total = 0.0
        for lay in layers:
            sub = coverage(c, lay['derivative'], lay.get('cms_version'),
                           f"{key}/{lay['derivative']}")
            if sub is None:
                continue
            merged.update(sub['layers'])
            total += sub['total_area']
            assets += sub['assets']
        return {'derivative': row['id'], 'cms_version': row['cms_version'],
                'width': row['width'], 'height': row['height'],
                'layers': merged, 'total_area': total, 'assets': assets}
    quad = preimage_quad(json.loads(row['ops']), row['width'], row['height'])
    area = polygon_area(quad)
    return {'derivative': row['id'], 'cms_version': row['cms_version'],
            'width': row['width'], 'height': row['height'],
            'layers': {key: {'quad': quad, 'area': area}}, 'total_area': area,
            'assets': assets}

def active_licenses(c, artifact, day):
    """Lender-signed region licenses in force on a day (revocation page applied)."""
    rows = c.execute('SELECT * FROM media_licenses WHERE artifact=? AND start<=? AND end>=?',
                     (artifact, day, day)).fetchall()
    out = []
    for r in rows:
        revoked = c.execute('SELECT MIN(day) d FROM media_license_revocations WHERE license_id=?',
                            (r['id'],)).fetchone()['d']
        if revoked is not None and revoked <= day:
            continue
        out.append(dict(r))
    return out

def _assess(c, row, day):
    """Gate a derivative row for a day.

    Combines the permit_render size/period baseline, the loan period written
    by loan.authorize, the pixel pre-image intersection with the licensed
    region mask, and the independent watermark/caption asset licenses.
    """
    reasons = []
    verified = _verified(c, row)
    if not verified:
        reasons.append('source-unverified')
    lics = active_licenses(c, row['artifact'], day)
    if not any(permit_render({'artifact': row['artifact'], 'width': row['width'], 'day': day},
                             {'artifact': lic['artifact'], 'max_width': lic['max_width'],
                              'start': lic['start'], 'end': lic['end']}) for lic in lics):
        reasons.append('baseline-gate')
    mask = [json.loads(lic['region']) for lic in lics]
    cov = coverage(c, row['id'], row['cms_version'])
    layers = {}; out_area = 0.0
    for key, lay in cov['layers'].items():
        covered = min(sum(polygon_area(clip_to_rect(lay['quad'], rect)) for rect in mask),
                      lay['area'])
        out = lay['area'] - covered
        layers[key] = {'area': lay['area'], 'covered': covered, 'out_area': out}
        out_area += out
    if out_area > 0:
        reasons.append('out-of-bounds')
    loan_ok = loan.covers(c, row['artifact'], day)
    if not loan_ok:
        reasons.append('loan-period-miss')
    for asset in cov['assets']:
        ar = c.execute('SELECT * FROM media_assets WHERE id=?', (asset['asset'],)).fetchone()
        if not ar or not (ar['start'] <= day <= ar['end']):
            reasons.append(f"asset-unlicensed:{asset['asset']}")
    return {'ok': not reasons, 'reasons': reasons, 'out_area': out_area, 'layers': layers,
            'licenses': [lic['id'] for lic in lics], 'loan_covered': loan_ok,
            'verified': verified}

def out_of_bounds(c, der_id, day, cms_version=None):
    """Out-of-bounds source pixel area of a derivative on a day, per layer."""
    row = _derivative(c, der_id, cms_version)
    if row is None:
        return None
    a = _assess(c, row, day)
    return {'derivative': row['id'], 'cms_version': row['cms_version'], 'day': day,
            'layers': a['layers'], 'out_area': a['out_area'], 'licenses': a['licenses']}

def assess_derivative(c, der_id, day, cms_version=None):
    """Disposition of a registered but not necessarily published derivative."""
    row = _derivative(c, der_id, cms_version)
    if row is None:
        return None
    a = _assess(c, row, day)
    return {'derivative': row['id'], 'cms_version': row['cms_version'], 'day': day,
            'status': 'publishable' if a['ok'] else 'pending-license',
            'reasons': a['reasons'], 'out_of_bounds_area': a['out_area'],
            'layers': a['layers'], 'licenses': a['licenses'], 'loan_covered': a['loan_covered']}

# ---------------------------------------------------------------- publications

def _live(pub, day):
    """Whether the CDN serves the file on a day (cache keeps serving until purge)."""
    if day < pub['published_day']:
        return False
    if pub['takedown_day'] is not None and day >= pub['cdn_purge_day']:
        return False
    return True

def _exposure(c, pub, der_row, until_day):
    """Exposed days and violating days of one publication version up to a day."""
    end = until_day
    if pub['takedown_day'] is not None:
        end = min(end, pub['cdn_purge_day'] - 1)
    exposed = []; violations = []
    day = pub['published_day']
    while day <= end:
        exposed.append(day)
        if not _assess(c, der_row, day)['ok']:
            violations.append(day)
        day += 1
    return exposed, violations

def assess_publications(c, day):
    """For every public file on a day: publishable / pending-license / must-takedown.

    Each entry carries the out-of-bounds source pixel area under the license
    versions active that day, the per-layer source attribution, and the
    historical exposure of the file (old URLs are never silently overwritten).
    """
    report = {'day': day, 'publishable': [], 'pending_license': [], 'must_takedown': [],
              'publications': {}, 'candidates': {}}
    pubs = c.execute('SELECT * FROM media_publications ORDER BY id, version').fetchall()
    for pub in pubs:
        if pub['published_day'] > day:
            continue
        der = _derivative(c, pub['derivative_id'], pub['derivative_cms_version'])
        a = _assess(c, der, day)
        live = _live(pub, day)
        if a['ok']:
            status = 'publishable'
        elif live:
            status = 'must-takedown'
        else:
            status = 'pending-license'
        exposed, violations = _exposure(c, pub, der, day)
        key = f"{pub['id']}#v{pub['version']}"
        report['publications'][key] = {
            'publication': pub['id'], 'version': pub['version'], 'url': pub['url'],
            'derivative': pub['derivative_id'], 'cms_version': pub['derivative_cms_version'],
            'cdn_sha256': pub['cdn_sha256'], 'status': status, 'reasons': a['reasons'],
            'out_of_bounds_area': a['out_area'], 'layers': a['layers'],
            'licenses': a['licenses'], 'loan_covered': a['loan_covered'],
            'verified': a['verified'], 'live': live,
            'exposed_days': exposed, 'violation_days': violations,
            'violations': len(violations),
            'published_day': pub['published_day'], 'takedown_day': pub['takedown_day'],
            'cdn_purge_day': pub['cdn_purge_day'], 'superseded_day': pub['superseded_day']}
        report[{'publishable': 'publishable', 'pending-license': 'pending_license',
                'must-takedown': 'must_takedown'}[status]].append(key)
    published = {(p['derivative_id'], p['derivative_cms_version']) for p in pubs}
    for row in c.execute('SELECT * FROM media_derivatives ORDER BY id, cms_version').fetchall():
        if (row['id'], row['cms_version']) in published:
            continue
        a = _assess(c, row, day)
        report['candidates'][row['id']] = {
            'cms_version': row['cms_version'],
            'status': 'publishable' if a['ok'] else 'pending-license',
            'reasons': a['reasons'], 'out_of_bounds_area': a['out_area'],
            'layers': a['layers'], 'licenses': a['licenses']}
    report['license_fees'] = license_fees(c)
    return report

def url_history(c, url):
    """Every file version ever served at a URL; old occupants stay on record."""
    rows = c.execute('SELECT * FROM media_publications WHERE url=? ORDER BY version',
                     (url,)).fetchall()
    return [{'publication': r['id'], 'version': r['version'], 'derivative': r['derivative_id'],
             'cms_version': r['derivative_cms_version'], 'cdn_sha256': r['cdn_sha256'],
             'published_day': r['published_day'], 'takedown_day': r['takedown_day'],
             'cdn_purge_day': r['cdn_purge_day'], 'superseded_day': r['superseded_day']}
            for r in rows]

def license_fees(c, artifact=None):
    """Total agreed authorization fees paid for signed region licenses."""
    if artifact is None:
        row = c.execute("SELECT COALESCE(SUM(amount),0) s FROM receipts WHERE kind='license-fee'").fetchone()
    else:
        row = c.execute("SELECT COALESCE(SUM(amount),0) s FROM receipts WHERE kind='license-fee' AND subject=?",
                        (artifact,)).fetchone()
    return row['s']
