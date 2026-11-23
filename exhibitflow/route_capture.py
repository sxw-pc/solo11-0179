"""Capture external convoy evidence, normalize it, and solve route assignment.

The archive (``sources``) keeps every captured artifact verbatim.  All derived
tables are rebuilt from that archive in a canonical order, so an interrupted
write, a late CSV, or a repeated capture yields identical trip identities, the
same conflict graph, and the same current dispatch table.

Captured kinds
  road          road capacity and clearance
  vehicle       per-vehicle escort qualification and allowed routes
  cargo         boxed artifact height and part number
  permit        time-boxed road permit (a consumable resource)
  announcement  ocr/screenshot or structured road notice from one issuer
  retraction    notice withdrawal, scoped to its own issuer
  ics           carrier calendar; RECURRENCE-ID is one occurrence, not the series
  csv           reroute/cancellation rows (may arrive late or out of order)
  seal-usage    seal-to-vehicle binding history (a seal may change vehicle)
"""
import csv, io, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from . import storage as s

NAME = '路线采录'
DEFAULT_TZ = 'America/New_York'
CLOSING = ('closure', 'closed', 'closed-full', 'shutdown')
TRANSFERABLE = 'transferable-endorsement'
LOCKED = 'locked-endorsement'


# ---------------------------------------------------------------- time helpers
def _zone(name):
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def to_epoch(value, tzname=DEFAULT_TZ, fold=0):
    """Epoch seconds from an int, numeric string, or ISO local/offset time."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.lstrip('-').isdigit():
        return int(text)
    dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tzname), fold=int(fold))
    return int(dt.timestamp())


def iso(epoch, tzname=DEFAULT_TZ):
    return datetime.fromtimestamp(int(epoch), _zone(tzname)).isoformat()


# ---------------------------------------------------------------- capture
def capture(con, event):
    """Archive one captured artifact; the rebuild is driven from this row."""
    p = event.get('data', {})
    kind = str(p.get('kind', '')).strip().lower()
    if kind not in ('road', 'vehicle', 'cargo', 'permit', 'announcement', 'retraction',
                    'ics', 'csv', 'seal-usage'):
        return s.decide(con, event, NAME, 'blocked', 'unknown-capture-kind', 'blocked')
    content = p.get('content', p.get('payload'))
    if content is None:
        return s.decide(con, event, NAME, 'blocked', 'empty-capture', 'blocked')
    if kind in ('ics', 'csv') and isinstance(content, str):
        pass
    elif not isinstance(content, (dict, list)):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            return s.decide(con, event, NAME, 'blocked', 'unparseable-capture', 'blocked')
    body = {'kind': kind, 'ref': p.get('ref', ''), 'content': content}
    con.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)',
                (event['id'], kind, s.digest(body), event['id'],
                 int(event.get('received_at', event.get('effective_at', 0)))))
    return s.decide(con, event, NAME, 'captured', f'{kind}-archived', 'captured')


# ---------------------------------------------------------------- normalization
def _sources(con, kind):
    rows = con.execute('SELECT * FROM sources WHERE kind=? ORDER BY digest, id', (kind,)).fetchall()
    out = []
    for row in rows:
        payload = con.execute('SELECT payload FROM events WHERE id=?', (row['event_id'],)).fetchone()
        body = json.loads(payload['payload'])['data']
        out.append((row, body.get('content')))
    return out


def _replace(con, table, rows):
    con.execute(f'DELETE FROM {table}')
    for row in rows:
        con.execute(f'INSERT OR REPLACE INTO {table} VALUES({",".join("?" * len(row))})', row)


def _parse_json_text(content):
    """OCR captures may arrive as one text blob or as a list of scanned lines."""
    if isinstance(content, list) and content and all(isinstance(x, str) for x in content):
        return '\n'.join(content)
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (TypeError, ValueError):
            return content
    return content


def _roads(con):
    rows = []
    for src, content in _sources(con, 'road'):
        for r in content if isinstance(content, list) else [content]:
            rows.append((r['id'], int(r.get('capacity', 1)), float(r.get('clear_height', 99.0)),
                         int(bool(r.get('permit_required', False))), src['id'], src['digest']))
    _replace(con, 'roads', sorted(rows))


def _vehicles(con):
    rows = []
    for src, content in _sources(con, 'vehicle'):
        for r in content if isinstance(content, list) else [content]:
            routes = ','.join(sorted(str(x) for x in r.get('routes', [])))
            rows.append((r['id'], float(r.get('max_height', 99.0)), routes,
                         int(bool(r.get('escort_ok', True))), src['id'], src['digest']))
    _replace(con, 'vehicles', sorted(rows))


def _cargo(con):
    rows = []
    for src, content in _sources(con, 'cargo'):
        for r in content if isinstance(content, list) else [content]:
            rows.append((r['id'], r['artifact'], r.get('box', r['id']), float(r.get('height', 0.0)),
                         str(r.get('part', '')), src['id'], src['digest']))
    _replace(con, 'cargo', sorted(rows))


def _permits(con):
    rows = {}
    for src, content in _sources(con, 'permit'):
        for r in content if isinstance(content, list) else [content]:
            key = (r['road_id'], to_epoch(r['window_from']), to_epoch(r['window_to']),
                   r.get('issuer', ''))
            rows[key] = (r.get('id') or s.digest(list(key))[:16], key[0], key[1], key[2],
                         key[3], r.get('status', 'granted'), src['id'], src['digest'])
    _replace(con, 'permits', sorted(rows.values()))


_ANNOUNCEMENT_KEYS = {'issuer', 'authority', 'road_id', 'road', 'notice_no', 'notice', 'status',
                      'valid', 'window', 'valid_from', 'valid_to', 'tz', 'fold', 'published_at',
                      'retracts'}


def _announcement_record(raw, default_issuer=''):
    """Structured record or a tiny OCR text block into a notice dict."""
    text = raw if isinstance(raw, str) else None
    if text is not None:
        raw = {}
        for line in text.splitlines():
            if ':' not in line:
                continue
            key, _, value = line.partition(':')
            raw[key.strip().lower()] = value.strip()
    if raw and not (_ANNOUNCEMENT_KEYS & set(raw)):
        return None
    issuer = str(raw.get('issuer') or raw.get('authority') or default_issuer).strip()
    notice = str(raw.get('notice_no') or raw.get('notice') or '').strip()
    status = str(raw.get('status', 'open')).strip().lower()
    window = raw.get('valid') or raw.get('window') or ''
    valid_from, valid_to = raw.get('valid_from'), raw.get('valid_to')
    if (valid_from is None or valid_to is None) and window:
        parts = str(window).replace('..', '/').split('/')
        if len(parts) == 2:
            valid_from, valid_to = parts
    tzname = raw.get('tz') or DEFAULT_TZ
    fold = int(raw.get('fold', 0))
    return {
        'issuer': issuer,
        'road_id': str(raw.get('road_id') or raw.get('road') or '').strip(),
        'notice_no': notice or s.digest([issuer, raw.get('road_id') or raw.get('road'), valid_from, valid_to])[:12],
        'status': status,
        'valid_from': to_epoch(valid_from, tzname, fold),
        'valid_to': to_epoch(valid_to, tzname, fold),
        'published_at': to_epoch(raw.get('published_at'), tzname, fold) or 0,
        'retracts': str(raw.get('retracts') or '').strip(),
    }


def _announcements(con):
    notices = {}
    for src, content in _sources(con, 'announcement'):
        content = _parse_json_text(content)
        for raw in content if isinstance(content, list) else [content]:
            rec = _announcement_record(raw, src['id'])
            if rec is None or not rec['issuer'] or rec['valid_from'] is None or rec['valid_to'] is None:
                continue
            notices[(rec['issuer'], rec['notice_no'])] = rec | {'source': src['id'], 'digest': src['digest']}
    for src, content in _sources(con, 'retraction'):
        content = _parse_json_text(content)
        for raw in content if isinstance(content, list) else [content]:
            rec = _announcement_record(raw, src['id'])
            if rec is None:
                continue
            key = (rec['issuer'], rec['retracts'] or rec['notice_no'])
            base = notices.get(key, rec)
            notices[key] = base | {'issuer': key[0], 'notice_no': key[1], 'status': 'retracted',
                                   'source': src['id'], 'digest': src['digest']}
    for rec in notices.values():
        # a retraction notice published before it could exist is still honored
        rec.setdefault('valid_from', rec.get('published_at') or 0)
        rec.setdefault('valid_to', rec.get('published_at') or 0)
    rows = []
    for (issuer, notice), rec in notices.items():
        ident = s.digest([issuer, rec['road_id'], notice])[:20]
        rows.append((ident, issuer, rec['road_id'], notice, int(rec['valid_from']), int(rec['valid_to']),
                     rec['status'], int(rec['published_at']), rec['retracts'], rec['source'], rec['digest']))
    _replace(con, 'announcements', sorted(rows))


def closure_windows(con):
    """Closed [from, to) intervals per road, merged across issuers.

    A retraction only removes its own issuer's notice.  Two issuers publishing
    the same road_id both remain visible, so the most restrictive view wins and
    neither erases the other's evidence.
    """
    by_road = {}
    for row in con.execute("SELECT * FROM announcements ORDER BY issuer,road_id,notice_no"):
        if row['status'] in CLOSING:
            by_road.setdefault(row['road_id'], []).append(
                (row['valid_from'], row['valid_to'], row['issuer'], row['notice_no']))
    windows = {}
    for road, spans in by_road.items():
        spans.sort()
        merged = []
        for start, end, issuer, notice in spans:
            if merged and start <= merged[-1][1]:
                last = merged[-1]
                merged[-1] = (last[0], max(last[1], end), last[2] + ',' + issuer, last[3] + ',' + notice)
            else:
                merged.append((start, end, issuer, notice))
        windows[road] = merged
    return windows


def _ics_events(content):
    """Parse the small ICS subset used by the carrier schedules."""
    if isinstance(content, dict):
        return content.get('events', [])
    if isinstance(content, list):
        return content
    events, current = [], None
    for raw in str(content).splitlines():
        line = raw.strip()
        if line == 'BEGIN:VEVENT':
            current = {}
            continue
        if line == 'END:VEVENT':
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ':' not in line:
            continue
        head, _, value = line.partition(':')
        name = head.split(';')[0].upper()
        params = {}
        for bit in head.split(';')[1:]:
            key, _, val = bit.partition('=')
            params[key.upper()] = val
        current[name] = {'value': value, 'params': params}
    return events


def _canonical_occurrence(depart, fold=0):
    """Occurrence identity is local wall time, so a DST fall-back never renames it.

    On the repeated wall clock the second instance carries a fold discriminator,
    keeping the two distinct without making identity depend on UTC offset.
    """
    dt = datetime.fromisoformat(str(depart))
    key = dt.strftime('%Y%m%dT%H%M%S')
    return key + ('#fold1' if int(fold) else '')


def _occurrence_epoch(depart, tzname, fold):
    """Fold-aware epoch; the second 01:30 in a fall-back keeps the later offset."""
    dt = datetime.fromisoformat(str(depart))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tzname), fold=int(fold))
    return int(dt.timestamp())


def _expand_master(master):
    """Expand a recurring master into concrete local occurrences.

    The recurrence window is the occurrence domain: the master is the first
    instance, and COUNT/RRULE-FREQ=DAILY extends it.  Each occurrence shares the
    master's duration and time of day.
    """
    first = datetime.fromisoformat(str(master['depart']).replace('Z', '+00:00'))
    count = int(master.get('count') or 1)
    if count <= 1:
        return [master]
    out = []
    for index in range(count):
        moment = first + timedelta(days=index)
        rec = dict(master)
        rec['recurrence'] = _canonical_occurrence(moment.isoformat(), 0)
        rec['depart'] = moment.isoformat()
        rec['arrive'] = (datetime.fromisoformat(str(master['arrive']).replace('Z', '+00:00'))
                         + timedelta(days=index)).isoformat()
        out.append(rec)
    return out


def _ics_trips(con):
    """One row per occurrence; RECURRENCE-ID replaces only that instance."""
    series = {}
    for src, content in _sources(con, 'ics'):
        for ev in _ics_events(content):
            uid = str(ev.get('UID', {}).get('value', '')).strip()
            if not uid:
                continue
            recurrence = ev.get('RECURRENCE-ID', {})
            recurrence_id = str(recurrence.get('value', '')).strip()
            recurrence_fold = int(recurrence.get('params', {}).get('FOLD', 0))
            rrule = str(ev.get('RRULE', {}).get('value', '')).upper()
            count = 1
            if 'COUNT=' in rrule:
                try:
                    count = int(rrule.split('COUNT=')[1].split(';')[0])
                except ValueError:
                    count = 1
            dtstart = ev.get('DTSTART', {})
            record = {
                'series_uid': uid,
                'recurrence': recurrence_id,
                'status': str(ev.get('STATUS', {}).get('value', 'CONFIRMED')).strip().upper(),
                'tz': dtstart.get('params', {}).get('TZID') or DEFAULT_TZ,
                'fold': dtstart.get('params', {}).get('FOLD', 0),
                'depart': dtstart.get('value'),
                'arrive': ev.get('DTEND', {}).get('value') or dtstart.get('value'),
                'vehicle': str(ev.get('VEHICLE', {}).get('value', '')).strip(),
                'route': str(ev.get('ROUTE', {}).get('value', '')).strip(),
                'artifact': str(ev.get('ARTIFACT', {}).get('value', '')).strip(),
                'count': count,
                'source': src['id'], 'digest': src['digest'],
            }
            bucket = series.setdefault(uid, {'master': None, 'overrides': {}})
            if recurrence_id:
                key = _canonical_occurrence(recurrence_id, recurrence_fold)
                bucket['overrides'][key] = record
            elif bucket['master'] is None or record['source'] < bucket['master']['source']:
                bucket['master'] = record

    rows = []
    for uid, bucket in series.items():
        occurrences = {}
        master = bucket['master']
        if master is not None:
            for rec in _expand_master(master):
                occurrences[rec['recurrence']] = rec
        # an exception replaces only its own instance; untouched instances survive
        occurrences.update(bucket['overrides'])
        for occurrence, rec in occurrences.items():
            cancelled = str(rec['status']).upper() == 'CANCELLED'
            depart = _occurrence_epoch(rec['depart'], rec['tz'], rec.get('fold', 0))
            arrive = _occurrence_epoch(rec['arrive'], rec['tz'], rec.get('fold', 0))
            trip_id = s.digest([uid, occurrence])[:20]
            rows.append((trip_id, uid, occurrence, occurrence, rec['vehicle'], rec['route'],
                         rec['artifact'], int(depart), int(arrive),
                         'cancelled' if cancelled else 'scheduled', rec['source'], rec['digest']))
    _replace(con, 'trips', sorted(rows))


def _csv_rows(content):
    if isinstance(content, list):
        return content
    return list(csv.DictReader(io.StringIO(str(content).strip())))


def _reroutes(con):
    """Latest received reroute row per trip wins; cancellation is sticky."""
    rows = {}
    for src, content in _sources(con, 'csv'):
        for raw in _csv_rows(content):
            row = {str(k).strip().lower(): v for k, v in raw.items()}
            trip = str(row.get('trip') or row.get('recurrence_id') or '').strip()
            if not trip:
                continue
            cancelled = str(row.get('cancelled', row.get('cancel', ''))).strip().lower() in ('1', 'true', 'yes', 'y')
            effective = to_epoch(row.get('effective_at', row.get('effective')), row.get('tz') or DEFAULT_TZ)
            received = to_epoch(row.get('received_at', row.get('received')), row.get('tz') or DEFAULT_TZ) or 0
            rec = {
                'trip': trip, 'from': str(row.get('from_route', row.get('from', ''))).strip(),
                'to': str(row.get('to_route', row.get('to', ''))).strip(),
                'cancelled': int(cancelled), 'effective_at': int(effective or 0),
                'received_at': int(received), 'source': src['id'], 'digest': src['digest'],
            }
            prev = rows.get(trip)
            if prev is None or (rec['received_at'], rec['source']) >= (prev['received_at'], prev['source']):
                rows[trip] = rec
            elif cancelled:
                rows[trip]['cancelled'] = 1
    _replace(con, 'reroutes', sorted(
        (trip, rec['trip'], rec['from'], rec['to'], rec['cancelled'], rec['received_at'],
         rec['effective_at'], 'adopted', rec['source'], rec['digest'])
        for trip, rec in rows.items()))


def _seals(con):
    rows = {}
    for src, content in _sources(con, 'seal-usage'):
        for raw in content if isinstance(content, list) else [content]:
            seal = str(raw.get('seal', '')).strip()
            if not seal:
                continue
            rows[(seal, raw.get('vehicle', ''), int(raw.get('effective_at', 0)))] = (
                s.digest([seal, raw.get('vehicle'), raw.get('effective_at')])[:20], seal,
                raw.get('vehicle', ''), raw.get('artifact', ''), int(raw.get('effective_at', 0)),
                src['id'], src['digest'])
    _replace(con, 'seal_history', sorted(rows.values()))


def rebuild(con):
    """Recompute every derived table from the source archive."""
    _roads(con)
    _vehicles(con)
    _cargo(con)
    _permits(con)
    _announcements(con)
    _ics_trips(con)
    _reroutes(con)
    _seals(con)
    return con



# ---------------------------------------------------------------- feasibility
def _overlaps(a_from, a_to, b_from, b_to):
    return a_from < b_to and b_from < a_to


def _closed_at(road, start, end, closures):
    """Does a closure hit any moment of the run?

    A road notice and a permit are independent instruments: a permit does not
    dissolve a closure.  Two notices that overlap are a single closure window.
    """
    spans = list(closures.get(road, []))
    spans.sort()
    merged = []
    for start_i, end_i, *_ in spans:
        lo, hi = max(start_i, start), min(end_i, end)
        if lo >= hi:
            continue
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return bool(merged)



def _road_info(con):
    return {r['id']: r for r in con.execute('SELECT * FROM roads')}


def _vehicle_info(con):
    return {r['id']: r for r in con.execute('SELECT * FROM vehicles')}


def _permit_key(permit):
    """One physical permit is one consumable, even if captured twice."""
    return f"{permit['issuer']}:{permit['road_id']}:{permit['window_from']}:{permit['window_to']}"


def _cargo_for(con, artifact):
    return con.execute('SELECT * FROM cargo WHERE artifact=? ORDER BY id', (artifact,)).fetchone()


def _usage(con):
    return {r['trip']: r for r in con.execute('SELECT * FROM reroutes')}


def route_options(con):
    """Per-trip route feasibility using every normalized source of truth."""
    roads, vehicles = _road_info(con), _vehicle_info(con)
    closures, usage = closure_windows(con), _usage(con)
    trips = [dict(r) for r in con.execute('SELECT * FROM trips ORDER BY depart_at,id')]
    out = {}
    for trip in trips:
        info = {'trip': trip['id'], 'vehicle': trip['vehicle'], 'artifact': trip['artifact'],
                'depart_at': trip['depart_at'], 'arrive_at': trip['arrive_at'],
                'status': trip['status'], 'options': [], 'blocked': []}
        if trip['status'] == 'cancelled':
            info['status'] = 'cancelled'
            out[trip['id']] = info
            continue
        vehicle = vehicles.get(trip['vehicle'])
        cargo = _cargo_for(con, trip['artifact'])
        if not vehicle:
            info['blocked'].append('vehicle-unknown')
            info['status'] = 'no-route'
            out[trip['id']] = info
            continue
        if not vehicle['escort_ok']:
            info['blocked'].append('escort-not-qualified')
        allowed = [r for r in vehicle['routes'].split(',') if r]
        if trip['id'] in usage and usage[trip['id']]['cancelled']:
            info['status'] = 'cancelled'
            info['blocked'].append('carrier-cancellation')
            out[trip['id']] = info
            continue
        reroute = usage.get(trip['id'])
        if reroute and reroute['to'] and reroute['to'] in allowed:
            allowed = [reroute['to']]
        if cargo and vehicle['max_height'] < cargo['height']:
            info['blocked'].append('cargo-height-exceeds-vehicle')
            allowed = []
        for road_id in allowed:
            road = roads.get(road_id)
            if not road:
                info['blocked'].append(f'road-unknown:{road_id}')
                continue
            if road['clear_height'] < (cargo['height'] if cargo else 0):
                info['blocked'].append(f'clearance:{road_id}')
                continue
            if _closed_at(road_id, trip['depart_at'], trip['arrive_at'], closures):
                info['blocked'].append(f'closed:{road_id}')
                continue
            permits = con.execute(
                "SELECT * FROM permits WHERE road_id=? AND status='granted' ORDER BY window_from,id",
                (road_id,)).fetchall()
            valid = [p for p in permits
                     if p['window_from'] <= trip['depart_at'] and trip['arrive_at'] <= p['window_to']]
            if road['permit_required'] and not valid:
                info['blocked'].append(f'no-permit:{road_id}')
                continue
            if not vehicle['escort_ok']:
                continue
            info['options'].append({'route': road_id, 'capacity': road['capacity'],
                                    'permit': _permit_key(valid[0]) if valid else None,
                                    'declared': trip['route']})
        out[trip['id']] = info
    return out


def conflict_graph(options):
    """Cross-vehicle edges grouped by plan label.

    Edges are written for a label rather than for a fixed assignment; a plan is
    legal only when none of its chosen routes is an endpoint of an edge for the
    reasons the plan triggers (lane capacity and permit consumption).
    """
    trips = sorted(options.values(), key=lambda t: (t['depart_at'], t['trip']))
    edges = []
    for i, left in enumerate(trips):
        for right in trips[i + 1:]:
            if left['vehicle'] == right['vehicle']:
                continue
            if not _overlaps(left['depart_at'], left['arrive_at'], right['depart_at'], right['arrive_at']):
                continue
            for a in left['options']:
                for b in right['options']:
                    if a['route'] != b['route']:
                        continue
                    detail = {'window': [max(left['depart_at'], right['depart_at']),
                                         min(left['arrive_at'], right['arrive_at'])],
                              'route': a['route']}
                    if a['capacity'] < 2 or b['capacity'] < 2:
                        edges.append({'kind': 'lane-capacity', 'a': left['trip'], 'b': right['trip'],
                                      'route': a['route'], **detail})
                    if a['permit'] and b['permit'] and a['permit'] == b['permit']:
                        edges.append({'kind': 'permit-shared', 'a': left['trip'], 'b': right['trip'],
                                      'route': a['route'], **detail})
    return edges


def violated_edges(chosen, edges):
    """Edges contradicted by a possibly-partial {trip: route} choice."""
    out = []
    for edge in edges:
        if chosen.get(edge['a']) == edge['route'] and chosen.get(edge['b']) == edge['route']:
            out.append(edge)
    return out


def greedy(options, edges, order=None):
    """Reproduce the legacy first-feasible-road dispatch, for comparison."""
    trips = sorted(options.values(), key=lambda t: (t['depart_at'], t['trip']))
    chosen = {}
    steps = []
    for trip in trips:
        if trip['status'] == 'cancelled':
            continue
        pick = None
        for option in sorted(trip['options'], key=lambda o: o['route']):
            if not violated_edges(chosen | {trip['trip']: option['route']}, edges):
                pick = option
                break
        if pick:
            chosen[trip['trip']] = pick['route']
        steps.append({'trip': trip['trip'], 'vehicle': trip['vehicle'], 'artifact': trip['artifact'],
                      'route': pick['route'] if pick else None,
                      'outcome': 'routable' if pick else 'no-route'})
    return {'assignment': chosen, 'steps': steps}


def _combinations(trips, idx=0, current=None):
    current = current or {}
    if idx == len(trips):
        yield dict(current)
        return
    trip = trips[idx]
    if not trip['options']:
        yield from _combinations(trips, idx + 1, current)
        return
    for option in sorted(trip['options'], key=lambda o: o['route']):
        current[trip['trip']] = option['route']
        yield from _combinations(trips, idx + 1, current)
    current.pop(trip['trip'], None)


def solve(options, edges, pinned=None):
    """Exhaustive global assignment; returns a traceable handoff order.

    ``pinned`` fixes signed-endorsement trips to their acknowledged route.
    Feasible plans rank first (fewest contradicted edges), then by how many
    artifacts arrive in-window, then by a stable route vector so replays of the
    same archive produce byte-identical dispatch tables.
    """
    pinned = pinned or {}
    trips = sorted((t for t in options.values() if t['status'] != 'cancelled'),
                   key=lambda t: (t['depart_at'], t['trip']))
    best = None
    for assignment in _combinations(trips):
        if any(pinned.get(t['trip']) and pinned[t['trip']] != assignment.get(t['trip']) for t in trips):
            continue
        violated = violated_edges(assignment, edges)
        arrived = sum(1 for t in trips if t['trip'] in assignment)
        vector = [assignment.get(t['trip'], '~') for t in trips]
        key = (len(violated), -arrived, vector)
        if best is None or key < best[0]:
            best = (key, assignment, violated)
    if best is None:
        return {'ok': False, 'plans': [], 'edges': edges, 'blocked': [t['trip'] for t in trips],
                'assignment': {}, 'reason': 'no-assignment'}
    _, assignment, violated = best
    ok = not violated
    blocked = sorted({e['a'] for e in violated} | {e['b'] for e in violated}) if violated else []
    plans = []
    for order, trip in enumerate(trips, 1):
        route = assignment.get(trip['trip'])
        chosen = next((o for o in trip['options'] if o['route'] == route), None)
        # an unsigned trip caught in a contradiction has no legal road; a pinned
        # signed trip keeps its acknowledged route so the blocked edge is visible
        stranded = not ok and trip['trip'] in blocked and not pinned.get(trip['trip'])
        plans.append({'trip': trip['trip'], 'vehicle': trip['vehicle'], 'artifact': trip['artifact'],
                      'route': None if stranded else route,
                      'permit': None if stranded else (chosen['permit'] if chosen else None),
                      'sequence': order, 'depart_at': trip['depart_at'], 'arrive_at': trip['arrive_at'],
                      'outcome': 'no-route' if stranded or not chosen else 'routable',
                      'reason': 'blocked-by-conflict' if stranded else 'globally-feasible' if ok else 'best-effort'})
    return {'ok': ok, 'plans': plans, 'edges': edges if ok else violated,
            'blocked': blocked, 'assignment': assignment,
            'reason': 'all-trips-routable' if ok else 'no-global-solution'}


def conflict_graph_rows(edges, label='current'):
    rows = []
    for edge in edges:
        ident = s.digest([label, edge['kind'], edge['a'], edge['b'], edge['route']])[:20]
        rows.append((ident, label, edge['a'], edge['b'], edge['kind'],
                     json.dumps({'route': edge['route'], 'window': edge['window']}, sort_keys=True),
                     s.digest(edge)))
    return sorted(rows)


def write_edges(con, edges, label='current'):
    for row in conflict_graph_rows(edges, label):
        con.execute('INSERT OR REPLACE INTO conflict_edges VALUES(?,?,?,?,?,?,?)', row)


def prepare(con):
    """Normalize the archive and derive options + the full conflict graph."""
    rebuild(con)
    options = route_options(con)
    return options, conflict_graph(options)


def dispatch(con, pinned=None):
    """Normalize, solve, and persist the current graph plus a replayable plan."""
    options, edges = prepare(con)
    plan = solve(options, edges, pinned=pinned)
    write_edges(con, edges, 'current')
    if not plan['ok']:
        write_edges(con, plan['edges'], 'blocked')
    return options, edges, plan
