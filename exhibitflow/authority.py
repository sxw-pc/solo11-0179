"""Delegated signing authority evidence.

The board grants signing power in graded steps (resolution -> director ->
curator -> assistant).  Every delegation edge carries its own upstream
signature, validity window, value ceiling, destination scope and a
redelegation flag.  A signing path is only as strong as its narrowest edge:
time windows intersect, value ceilings take the minimum and destinations
intersect.  Parallel paths are alternatives, never a sum.

All evidence is content addressed so that identical reprints of the same
document are recognised while differing source versions are retained.
"""
import hashlib
import json

from . import storage as s

NAME = '授权链'
BOARD = 'board'

DELEGATION = 'delegation'
REVOCATION = 'revocation'
VALUATION = 'valuation'
SIGNATURE = 'signature'
CLAIM = 'authority-claim'


# ---------------------------------------------------------------- document log

def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _content(payload):
    """Hash the domain content only; import bookkeeping must not split reprints."""
    body = {k: v for k, v in payload.items()
            if not k.startswith('_') and k not in ('received_at', 'event_id', 'imported_at')}
    return hashlib.sha256((json.dumps(body, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')) + '|').encode()).hexdigest()[:16]


def _load(rows):
    out = []
    for row in rows:
        state = json.loads(row['state'])
        state['_doc_id'] = row['id']
        state['_version'] = row['version']
        state['_rowid'] = row['rowid']
        out.append(state)
    return out


def _kind_docs(con, kind, subject=None):
    if subject is None:
        rows = con.execute('SELECT rowid,id,subject,version,state FROM documents WHERE kind=?', (kind,)).fetchall()
    else:
        rows = con.execute('SELECT rowid,id,subject,version,state FROM documents WHERE kind=? AND subject=?', (kind, subject)).fetchall()
    return _load(rows)


def _latest_by_key(docs):
    out = {}
    for doc in docs:
        key = doc.get('doc_key')
        if key is None:
            continue
        if key not in out or doc['_version'] > out[key]['_version']:
            out[key] = doc
    return out


def _store_doc(con, subject, kind, doc_key, payload):
    """Store one source version.  Same content re-imported is a reprint."""
    payload = dict(payload)
    payload['doc_key'] = doc_key
    content = _content(payload)
    for old in _kind_docs(con, kind):
        if old.get('doc_key') == doc_key and old.get('_content') == content:
            return dict(old, reprint=True)
    versions = [d['_version'] for d in _kind_docs(con, kind) if d.get('doc_key') == doc_key]
    version = (max(versions) if versions else 0) + 1
    payload['_content'] = content
    doc_id = f'{kind}:{doc_key}:v{version}'
    con.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',
                (doc_id, subject, kind, version, _canonical(payload)))
    payload['_doc_id'] = doc_id
    payload['_version'] = version
    return payload


# --------------------------------------------------------------- ingest entry

def apply(con, event):
    action = event['action']
    data = dict(event.get('data', {}))
    subject = event['subject']
    if action == 'delegation':
        return _ingest_delegation(con, event, subject, data)
    if action == 'revocation':
        return _ingest_revocation(con, event, subject, data)
    if action == 'valuation':
        return _ingest_valuation(con, event, subject, data)
    if action == 'signature':
        return _ingest_signature(con, event, subject, data)
    return s.decide(con, event, NAME, 'blocked', 'unknown-action', 'blocked')


def _int(value, fallback=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _person(value):
    if not isinstance(value, dict):
        return None
    employee_id = value.get('employee_id')
    if not employee_id:
        return None
    return {'name': value.get('name', ''), 'employee_id': str(employee_id),
            'title': value.get('title', ''), 'employer': value.get('employer', '')}


def _ingest_delegation(con, event, subject, data):
    delegator = _person(data.get('delegator'))
    delegate = _person(data.get('delegate'))
    if not delegator or not delegate:
        return s.decide(con, event, NAME, 'blocked', 'missing-employee-identity', 'blocked')
    if not data.get('source_doc'):
        return s.decide(con, event, NAME, 'blocked', 'missing-source-document', 'blocked')
    if not data.get('signature'):
        return s.decide(con, event, NAME, 'blocked', 'upstream-signature-missing', 'blocked')
    start = _int(data.get('valid_from'))
    end = _int(data.get('valid_to'))
    limit = _int(data.get('max_value'))
    if start is None or end is None or start > end:
        return s.decide(con, event, NAME, 'blocked', 'invalid-validity-window', 'blocked')
    if limit is None or limit < 0:
        return s.decide(con, event, NAME, 'blocked', 'invalid-value-ceiling', 'blocked')
    destinations = data.get('destinations')
    if not isinstance(destinations, list) or not [d for d in destinations if d]:
        return s.decide(con, event, NAME, 'blocked', 'missing-destination-scope', 'blocked')
    if 'redelegable' not in data:
        return s.decide(con, event, NAME, 'blocked', 'redelegation-flag-missing', 'blocked')
    root = bool(data.get('root_authority')) or delegator['employee_id'] == BOARD
    if data.get('root_authority') and delegator['employee_id'] != BOARD:
        return s.decide(con, event, NAME, 'blocked', 'root-authority-must-originate-at-board', 'blocked')
    doc = _store_doc(con, subject, DELEGATION, str(data['source_doc']), {
        'doc_key': str(data['source_doc']),
        'source_doc': str(data['source_doc']),
        'delegator': delegator,
        'delegate': delegate,
        'valid_from': start,
        'valid_to': end,
        'max_value': limit,
        'destinations': sorted({str(d) for d in destinations if d}),
        'redelegable': bool(data['redelegable']),
        'root_authority': root,
        'signature': data['signature'],
        'received_at': _int(event.get('received_at'), 0),
    })
    stopped = _sweep_evidence(con, event, subject)
    reason = 'reprint-retained' if doc.get('reprint') else 'delegation-edge-retained'
    if stopped:
        reason += f";stopped-transfers:{len(stopped)}"
    return s.decide(con, event, NAME, 'recorded',
                    f"{reason}:{doc['doc_key']}:v{doc['_version']}", 'recorded')


def _ingest_revocation(con, event, subject, data):
    target = data.get('target') or data.get('delegation')
    source_doc = data.get('source_doc')
    if not target:
        return s.decide(con, event, NAME, 'blocked', 'missing-revocation-target', 'blocked')
    if not source_doc:
        return s.decide(con, event, NAME, 'blocked', 'missing-source-document', 'blocked')
    if not data.get('signature'):
        return s.decide(con, event, NAME, 'blocked', 'revocation-signature-missing', 'blocked')
    effective = _int(data.get('effective_at'), _int(event.get('effective_at'), 0))
    if effective is None:
        return s.decide(con, event, NAME, 'blocked', 'invalid-effective-time', 'blocked')
    doc = _store_doc(con, subject, REVOCATION, str(source_doc), {
        'doc_key': str(source_doc),
        'source_doc': str(source_doc),
        'target': str(target),
        'effective_at': effective,
        'person': _person(data.get('person')) or {},
        'signature': data['signature'],
        'received_at': _int(event.get('received_at'), 0),
    })
    stopped = _compensate_revocation(con, event, doc)
    stopped += [x for x in _sweep_evidence(con, event, subject) if x not in stopped]
    reason = f"revocation-effective-at:{effective}"
    if stopped:
        reason += f";stopped-transfers:{len(stopped)}"
    return s.decide(con, event, NAME, 'recorded', reason, 'recorded')


def _ingest_valuation(con, event, subject, data):
    artifact = str(data.get('artifact') or subject)
    value = _int(data.get('value'))
    effective = _int(data.get('effective_at'), _int(event.get('effective_at'), 0))
    if value is None or value <= 0:
        return s.decide(con, event, NAME, 'blocked', 'invalid-valuation', 'blocked')
    doc_key = str(data.get('source_doc') or f'valuation:{artifact}')
    doc = _store_doc(con, artifact, VALUATION, doc_key, {
        'doc_key': doc_key,
        'source_doc': doc_key,
        'artifact': artifact,
        'value': value,
        'currency': data.get('currency', 'unit'),
        'destination': data.get('destination'),
        'purpose': data.get('purpose', 'exhibition'),
        'effective_at': effective,
        'received_at': _int(event.get('received_at'), 0),
    })
    stopped = _sweep_evidence(con, event, artifact)
    reason = 'reprint-retained' if doc.get('reprint') else 'valuation-retained'
    if stopped:
        reason += f";stopped-transfers:{len(stopped)}"
    return s.decide(con, event, NAME, 'recorded',
                    f"{reason}:{artifact}:{value}:at:{effective}", 'recorded')


def _ingest_signature(con, event, subject, data):
    signer = _person(data.get('signer'))
    if not signer:
        return s.decide(con, event, NAME, 'blocked', 'missing-signer-employee-id', 'blocked')
    protocol = str(data.get('protocol') or f'protocol:{subject}')
    at = _int(data.get('at'), _int(event.get('effective_at'), 0))
    if at is None:
        return s.decide(con, event, NAME, 'blocked', 'invalid-signing-time', 'blocked')
    if not data.get('signature') or not data.get('seal_coords'):
        return s.decide(con, event, NAME, 'blocked', 'signature-or-seal-coordinates-missing', 'blocked')
    doc = _store_doc(con, subject, SIGNATURE, protocol, {
        'doc_key': protocol,
        'protocol': protocol,
        'document': data.get('document'),
        'signer': signer,
        'at': at,
        'destination': data.get('destination'),
        'purpose': data.get('purpose', 'exhibition'),
        'signature': data['signature'],
        'seal_coords': data['seal_coords'],
        'received_at': _int(event.get('received_at'), 0),
    })
    stopped = _sweep_evidence(con, event, subject)
    reason = 'reprint-retained' if doc.get('reprint') else 'signature-retained'
    if stopped:
        reason += f";stopped-transfers:{len(stopped)}"
    return s.decide(con, event, NAME, 'recorded', f"{reason}:{protocol}:at:{at}", 'recorded')


# ------------------------------------------------------------------ lookups

def find_signature(con, protocol, signing_at=None):
    docs = [d for d in _kind_docs(con, SIGNATURE) if d.get('protocol') == protocol]
    if signing_at is not None:
        docs = [d for d in docs if int(d['at']) <= int(signing_at)]
    if not docs:
        return None
    return max(docs, key=lambda d: (int(d['at']), d['_version'], d['_rowid']))


def find_valuation(con, artifact, at):
    docs = [d for d in _kind_docs(con, VALUATION)
            if d.get('artifact') == artifact and int(d['effective_at']) <= int(at)]
    if not docs:
        return None
    return max(docs, key=lambda d: (int(d['effective_at']), d['_version'], d['_rowid']))


def _delegations(con):
    return list(_latest_by_key(_kind_docs(con, DELEGATION)).values())


def _revocations(con):
    out = {}
    for doc in _latest_by_key(_kind_docs(con, REVOCATION)).values():
        out.setdefault(str(doc['target']), []).append(doc)
    return out


def has_evidence(con, protocol, artifact, signing_at=None):
    """True when authority evidence exists for this protocol/artifact.

    Legacy contracts keep the historical behaviour until delegation,
    valuation or signature scans are ingested.  Once any such evidence is
    present the strict path rules take over, so an operator cannot record
    only part of the chain and fall back to the unchecked rule.
    """
    if find_signature(con, protocol, signing_at) is not None:
        return True
    if find_valuation(con, artifact, _int(signing_at, 10 ** 12)) is not None:
        return True
    if _kind_docs(con, DELEGATION, artifact):
        return True
    if _kind_docs(con, REVOCATION, artifact):
        return True
    return bool(_kind_docs(con, CLAIM, artifact))


def _person_id(person):
    return (person or {}).get('employee_id')


def _enumerate_paths(edges, signer_id):
    """Enumerate chains ending at the signer.

    Edges whose delegator is the board are roots.  An edge that ends at a
    person with no upstream grant is reported as incomplete instead of being
    silently dropped, so the missing approval is visible to the operator.
    An upstream edge that forbids redelegation stops the walk at that point.
    """
    grants = {}
    for edge in edges:
        grants.setdefault(_person_id(edge.get('delegate')), []).append(edge)
    complete, incomplete = [], []

    def walk(edge, downstream, seen):
        if edge['doc_key'] in seen:
            return
        chain = [edge] + downstream
        delegator = _person_id(edge.get('delegator'))
        if delegator == BOARD or edge.get('root_authority'):
            complete.append({'edges': chain})
            return
        upstream = grants.get(delegator) or []
        if not upstream:
            incomplete.append({'edges': chain, 'missing_upstream_for': delegator})
            return
        non_redelegable = [p for p in upstream if not p.get('redelegable')]
        if non_redelegable:
            # Those grants may be used by the holder but not passed on; report
            # them so the block is visible, then keep exploring any other
            # upstream grant that does allow redelegation.
            incomplete.append({'edges': [non_redelegable[0]] + chain,
                               'missing_upstream_for': delegator,
                               'blocking_doc_key': non_redelegable[0]['doc_key'],
                               'reason': 'redelegation-not-permitted'})
        for parent in upstream:
            if parent.get('redelegable'):
                walk(parent, chain, seen | {edge['doc_key']})

    for leaf in grants.get(signer_id) or []:
        walk(leaf, [], set())
    return complete, incomplete


def _edge_public(edge):
    return {
        'doc_key': edge['doc_key'],
        'source_doc': edge.get('source_doc'),
        'delegator': edge.get('delegator'),
        'delegate': edge.get('delegate'),
        'valid_from': edge['valid_from'],
        'valid_to': edge['valid_to'],
        'max_value': edge['max_value'],
        'destinations': edge['destinations'],
        'redelegable': edge['redelegable'],
        'signature_present': bool(edge.get('signature')),
    }


def _analyze_path(con, path, signing_at, check_at, destination, revocations):
    edges = path['edges']
    invalid = []
    for index, edge in enumerate(edges):
        if not edge.get('signature'):
            invalid.append({'doc_key': edge['doc_key'], 'delegate': edge.get('delegate'),
                            'reason': 'upstream-signature-missing'})
        if index < len(edges) - 1 and not edge.get('redelegable'):
            invalid.append({'doc_key': edge['doc_key'], 'delegate': edge.get('delegate'),
                            'reason': 'redelegation-not-permitted'})
        hits = [r for r in revocations.get(str(edge['doc_key']), [])
                if int(r['effective_at']) <= check_at]
        if hits:
            at = max(int(r['effective_at']) for r in hits)
            invalid.append({'doc_key': edge['doc_key'], 'delegate': edge.get('delegate'),
                            'reason': f'revoked-at:{at}'})
    valid_from = max(int(e['valid_from']) for e in edges)
    valid_to = min(int(e['valid_to']) for e in edges)
    limit = min(int(e['max_value']) for e in edges)
    destinations = set(edges[0]['destinations'])
    for edge in edges[1:]:
        destinations &= set(edge['destinations'])
    destinations = sorted(destinations)
    covers_signing = valid_from <= signing_at <= valid_to
    covers_check = valid_from <= check_at <= valid_to
    if not covers_signing:
        invalid.append({'doc_key': edges[-1]['doc_key'], 'delegate': edges[-1].get('delegate'),
                        'reason': f'outside-signing-window:{valid_from}-{valid_to}'})
    destination_ok = bool(destination) and destination in destinations
    return {
        'edges': edges,
        'valid_from': valid_from,
        'valid_to': valid_to,
        'limit': limit,
        'destinations': destinations,
        'covers_signing': covers_signing,
        'covers_check': covers_check,
        'destination_ok': destination_ok,
        'invalid': invalid,
    }


def _public_path(analysis):
    return {
        'edges': [_edge_public(e) for e in analysis['edges']],
        'valid_from': analysis['valid_from'],
        'valid_to': analysis['valid_to'],
        'limit': analysis['limit'],
        'destinations': analysis['destinations'],
        'covers_signing': analysis['covers_signing'],
        'covers_check': analysis['covers_check'],
        'destination_ok': analysis['destination_ok'],
        'invalid': analysis['invalid'],
    }


def evaluate(con, protocol, artifact, signing_at=None, check_at=None, destination=None):
    """Compute the verifiable signing path for one protocol at a moment in time."""
    check = _int(check_at)
    signature = find_signature(con, protocol, signing_at)
    if signature is None and signing_at is None and check is not None:
        signature = find_signature(con, protocol, check)
    if signature is not None:
        effective_signing = int(signature['at'])
        signer = signature.get('signer') or {}
    else:
        effective_signing = _int(signing_at, check if check is not None else 0)
        signer = {}
    if check is None:
        check = effective_signing
    requested_signing = _int(signing_at)
    valuation = find_valuation(con, artifact, effective_signing)
    destination = destination or (signature or {}).get('destination') or (valuation or {}).get('destination')

    result = {
        'protocol': protocol,
        'artifact': artifact,
        'signing_at': effective_signing,
        'check_at': check,
        'destination': destination,
        'valuation': int(valuation['value']) if valuation else None,
        'valuation_source': valuation.get('source_doc') if valuation else None,
        'valuation_event_at': int(valuation['effective_at']) if valuation else None,
        'signer': signer,
        'signature_time': int(signature['at']) if signature else None,
        'signature_document': signature.get('document') if signature else None,
        'paths': [],
        'valid_paths': [],
        'invalid_edges': [],
        'missing_approvals': [],
        'reasons': [],
        'authority_limit': None,
        'narrowest_limit': None,
        'outcome': 'insufficient-authority',
    }
    if signature is None:
        result['outcome'] = 'unsigned'
        result['reasons'] = ['signature-missing']
        result['missing_approvals'] = ['record-signature', 'seal-coordinates']
        return result
    if requested_signing is not None and int(signature['at']) != requested_signing:
        # The agreement claims one signing moment but the scanned seal carries
        # another: never let a grant or valuation backdate across that gap.
        result['outcome'] = 'unsigned'
        result['reasons'] = ['signing-time-mismatch']
        result['missing_approvals'] = ['rescan-signature-at-claimed-time']
        return result
    signer_id = _person_id(signer)
    if not signer_id:
        result['outcome'] = 'unsigned'
        result['reasons'] = ['signer-identity-missing']
        result['missing_approvals'] = ['record-signer-employee-id']
        return result
    if valuation is None:
        result['reasons'] = ['valuation-missing']
        result['missing_approvals'] = ['artifact-valuation']
        return result

    revocations = _revocations(con)
    complete, incomplete = _enumerate_paths(_delegations(con), signer_id)
    analyzed = [_analyze_path(con, path, effective_signing, check, destination, revocations)
                for path in complete]
    result['paths'] = [_public_path(a) for a in analyzed]

    invalid = []
    for analysis in analyzed:
        invalid.extend(analysis['invalid'])
    for path in incomplete:
        blocking = path.get('blocking_doc_key') or path['edges'][-1]['doc_key']
        invalid.append({'doc_key': blocking,
                        'delegate': path['edges'][-1].get('delegate'),
                        'reason': path.get('reason') or f"no-upstream-grant-for:{path['missing_upstream_for']}"})
    unique = {}
    for item in invalid:
        unique[(item['doc_key'], item['reason'])] = item
    result['invalid_edges'] = list(unique.values())
    result['incomplete_paths'] = [
        {'edges': [_edge_public(e) for e in p['edges']],
         'missing_upstream_for': p['missing_upstream_for'],
         'reason': p.get('reason', 'no-upstream-grant')} for p in incomplete]

    # The delegation window governs when the signature may be given; the
    # movement itself is governed by the permit and by revocations, which
    # are checked against the movement time above.
    usable = [a for a in analyzed if not a['invalid'] and a['covers_signing']
              and a['destination_ok']]
    result['valid_paths'] = [_public_path(a) for a in usable]
    result['limits_by_path'] = [a['limit'] for a in analyzed]
    if usable:
        # Alternatives, not a sum: the strongest single usable path defines
        # the ceiling; withdrawn or invalid paths never contribute to it.
        ceiling = max(a['limit'] for a in usable)
        result['authority_limit'] = ceiling
        result['narrowest_limit'] = ceiling
    value = result['valuation']
    reasons = []

    if usable:
        sufficient = [a for a in usable if a['limit'] >= value]
        if sufficient:
            result['outcome'] = 'authorized'
            result['reasons'] = ['path-bottleneck-within-value', 'destination-and-time-covered']
            result['selected_path'] = _public_path(
                max(sufficient, key=lambda a: (a['limit'], -len(a['edges']))))
            return result
        reasons.append('value-exceeds-delegated-limit')
        result['missing_approvals'] = [
            f'board-direct-authorization-for:{signer_id}',
            'low-value-addendum',
            f'value-reduction-below:{value}',
        ]
    else:
        revoked = any(str(i['reason']).startswith('revoked-at:') for i in result['invalid_edges'])
        if revoked:
            result['outcome'] = 'revoked'
            reasons.append('revoked-before-movement')
            result['missing_approvals'] = [f're-authorize-signer:{signer_id}',
                                           'board-direct-authorization',
                                           'low-value-addendum']
        elif not analyzed:
            redelegation = any(p.get('reason') == 'redelegation-not-permitted' for p in incomplete)
            if redelegation:
                reasons.append('redelegation-not-permitted')
                result['missing_approvals'] = ['redelegation-approval']
            else:
                reasons.append('no-delegation-path')
                result['missing_approvals'] = [f'delegation-path-for:{signer_id}',
                                               'board-direct-authorization']
        elif not destination:
            reasons.append('destination-missing')
            result['missing_approvals'] = ['record-destination']
        elif not any(a['destination_ok'] for a in analyzed):
            reasons.append('destination-out-of-scope')
            result['missing_approvals'] = [f'extend-destination-scope:{destination}']
        elif any(not e.get('redelegable') for a in analyzed
                 for e in a['edges'][:-1]):
            reasons.append('redelegation-not-permitted')
            result['missing_approvals'] = ['redelegation-approval']
        elif not any(a['covers_signing'] for a in analyzed):
            reasons.append('outside-delegation-window-at-signing')
            result['missing_approvals'] = ['board-direct-authorization']
        elif any(i['reason'] == 'upstream-signature-missing' for i in result['invalid_edges']):
            reasons.append('upstream-signature-missing')
            result['missing_approvals'] = ['board-resolution-signature']
        else:
            reasons.append('no-usable-path')
            result['missing_approvals'] = ['board-direct-authorization']
    result['reasons'] = reasons
    return result


# ------------------------------------------------------------- claim storage

def save_evaluation(con, event_id, evaluation):
    con.execute(
        'INSERT INTO authority_evaluations(event_id,protocol,signing_at,check_at,outcome,'
        'valuation,destination,narrowest_limit,detail) VALUES(?,?,?,?,?,?,?,?,?)',
        (event_id, evaluation['protocol'], int(evaluation['signing_at']),
         int(evaluation['check_at']), evaluation['outcome'],
         int(evaluation.get('valuation') or 0), evaluation.get('destination') or '',
         evaluation.get('narrowest_limit'), _canonical(evaluation)))


def save_claim(con, subject, version, protocol, evaluation, mode, start, end, signing_at=None):
    payload = {
        'mode': mode,
        'protocol': protocol,
        'artifact': subject,
        'start': int(start),
        'end': int(end),
        'signing_at': int(signing_at if signing_at is not None else evaluation.get('signing_at') or start),
        'destination': evaluation.get('destination'),
        'valuation': evaluation.get('valuation'),
        'outcome': evaluation.get('outcome'),
        'authority_limit': evaluation.get('authority_limit'),
        'narrowest_limit': evaluation.get('narrowest_limit'),
        'selected_path': evaluation.get('selected_path'),
        'invalid_edges': evaluation.get('invalid_edges', []),
        'missing_approvals': evaluation.get('missing_approvals', []),
        'signer': evaluation.get('signer') or {},
    }
    con.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',
                (f'{CLAIM}:{subject}:{protocol}:v{version}', subject, CLAIM, version,
                 _canonical(payload)))
    return payload


def claim_for(con, subject, at):
    claims = [c for c in _kind_docs(con, CLAIM, subject)
              if int(c['start']) <= int(at) <= int(c['end'])]
    if not claims:
        return None
    return max(claims, key=lambda c: (c['_version'], c.get('signing_at') or -1,
                                     c.get('protocol') or '', c['_rowid']))


def governing(con, subject, at, version=None):
    """Resolve which protocol/signature governs a subject at a movement time.

    Transit events may omit the protocol; the authorized claim (or the latest
    scanned signature) supplies it.  The legacy default is only used when no
    authority evidence names a protocol at all.
    """
    claim = None
    if version is not None:
        matched = [c for c in _kind_docs(con, CLAIM, subject) if int(c['_version']) == int(version)]
        claim = matched[-1] if matched else None
    claim = claim or claim_for(con, subject, at)
    if claim:
        return {'protocol': claim.get('protocol'),
                'signing_at': claim.get('signing_at'),
                'destination': claim.get('destination'),
                'claim': claim}
    candidates = [d for d in _kind_docs(con, SIGNATURE, subject) if int(d['at']) <= int(at)]
    signature = max(candidates, key=lambda d: (int(d['at']), d['_version'], d['_rowid'])) if candidates else None
    if signature:
        return {'protocol': signature.get('protocol'),
                'signing_at': int(signature['at']),
                'destination': signature.get('destination'),
                'claim': None}
    return {'protocol': f'protocol:{subject}', 'signing_at': None,
            'destination': None, 'claim': None}


def evaluations(con, protocol=None):
    if protocol is None:
        rows = con.execute('SELECT id,detail FROM authority_evaluations ORDER BY id').fetchall()
    else:
        rows = con.execute('SELECT id,detail FROM authority_evaluations WHERE protocol=? ORDER BY id',
                           (protocol,)).fetchall()
    out = []
    for row in rows:
        detail = json.loads(row['detail'])
        detail['_id'] = row['id']
        out.append(detail)
    return out


def latest_evaluation(con, protocol):
    rows = evaluations(con, protocol)
    return rows[-1] if rows else None


# -------------------------------------------------------------- compensation

def compensate(con, event, subject, box, kind, evaluation, note=None):
    """Record that the next physical transfer must stop; custody history stays.

    The key is the box and the governing protocol, so late evidence arriving
    through several events cannot pile up duplicate stops for one box.
    Returns (compensation_id, created).
    """
    box = box or ''
    protocol = evaluation.get('protocol') or 'unknown'
    comp_id = f"compensation:{subject}:{box or 'pre-seal'}:{kind}:{protocol}"
    if con.execute('SELECT 1 FROM compensations WHERE id=?', (comp_id,)).fetchone():
        return comp_id, False
    detail = _canonical({
        'kind': kind,
        'protocol': evaluation.get('protocol'),
        'signing_at': evaluation.get('signing_at'),
        'check_at': evaluation.get('check_at'),
        'outcome': evaluation.get('outcome'),
        'invalid_edges': evaluation.get('invalid_edges', []),
        'missing_approvals': evaluation.get('missing_approvals', []),
        'note': note,
    })
    stop_at = int(evaluation.get('check_at', event.get('effective_at', 0)))
    con.execute('INSERT INTO compensations VALUES(?,?,?,?,?,?,?)',
                (comp_id, subject, box, kind, event['id'],
                 stop_at, detail))
    save_evaluation(con, comp_id, evaluation)
    s.decide(con, {'id': comp_id, 'subject': subject, 'effective_at': stop_at},
             NAME, 'compensation', f'{kind}:custody-chain-retained', 'stop-next-transfer')
    return comp_id, True


def _sweep_evidence(con, event, subject):
    """Re-check boxes already on the move once late evidence lands.

    Arrivals stay in the physical custody chain.  A sealed box about to be
    handed off again gets a stop decision that keeps the history replayable.
    """
    stopped = []
    rows = con.execute("SELECT id,subject,state,version FROM allocations "
                       "WHERE state IN ('sealed','in-transit') ORDER BY id").fetchall()
    for allocation in rows:
        if subject is not None and allocation['subject'] != subject:
            continue
        resolved = governing(con, allocation['subject'], int(event.get('effective_at', 0)),
                             version=allocation['version'])
        protocol = resolved.get('protocol')
        if not protocol:
            continue
        if not has_evidence(con, protocol, allocation['subject']):
            continue
        evaluation = evaluate(con, protocol, allocation['subject'],
                              signing_at=resolved.get('signing_at'),
                              check_at=int(event.get('effective_at', 0)),
                              destination=resolved.get('destination'))
        if evaluation['outcome'] == 'authorized':
            continue
        comp_id, created = compensate(con, event, allocation['subject'], allocation['id'],
                                      'stop-next-transfer', evaluation,
                                      note='late-authority-evidence')
        if created and comp_id not in stopped:
            stopped.append(comp_id)
    return stopped


def _compensate_revocation(con, event, revocation):
    target = str(revocation['target'])
    effective = int(revocation['effective_at'])
    stopped = []
    rows = con.execute("SELECT id,subject,state FROM allocations "
                       "WHERE state IN ('sealed','in-transit') ORDER BY id").fetchall()
    for allocation in rows:
        claim = claim_for(con, allocation['subject'], effective)
        if not claim:
            continue
        protocol = claim.get('protocol')
        if not has_evidence(con, protocol, allocation['subject']):
            continue
        evaluation = evaluate(con, protocol=protocol, artifact=allocation['subject'],
                              signing_at=claim.get('signing_at'), check_at=effective,
                              destination=claim.get('destination'))
        affected = any(str(i.get('doc_key')) == target for i in evaluation.get('invalid_edges', []))
        if affected and evaluation['outcome'] != 'authorized':
            comp_id, created = compensate(con, event, allocation['subject'], allocation['id'],
                                          'stop-next-transfer', evaluation,
                                          note=f'revocation:{revocation["source_doc"]}')
            if created:
                stopped.append(comp_id)
    return stopped
