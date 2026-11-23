from . import authority
from . import storage as s

NAME = '借展契约'
LEGACY_PROTOCOL = 'protocol:{0}'


def _protocol(p, sid):
    return str(p.get('protocol') or p.get('agreement') or LEGACY_PROTOCOL.format(sid))


def _signing_at(p, e):
    value = p.get('signing_at', p.get('signed_at'))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _assess(c, e, p, sid):
    """Return (evaluation, protocol, signing_at) or (None, protocol, None)."""
    protocol = _protocol(p, sid)
    signing_at = _signing_at(p, e)
    # Without an explicit signing time, the latest scanned signature at or
    # before this event defines the signing moment.
    probe_at = signing_at if signing_at is not None else e['effective_at']
    if not authority.has_evidence(c, protocol, sid, probe_at):
        return None, protocol, signing_at
    evaluation = authority.evaluate(c, protocol, sid, signing_at=signing_at,
                                    check_at=e['effective_at'],
                                    destination=p.get('destination'))
    return evaluation, protocol, evaluation['signing_at']


def record_addendum(c, e, p, sid):
    """Low-value addendum: a separate, lower-valued object travelling on the same path."""
    protocol = str(p.get('protocol') or p.get('agreement') or f'addendum:{sid}')
    artifact = str(p.get('artifact') or f'{sid}-low-value-addendum')
    signing_at = _signing_at(p, e)
    if signing_at is None:
        return s.decide(c, e, NAME, 'blocked', 'missing-signing-time', 'blocked')
    evaluation = authority.evaluate(c, protocol, artifact, signing_at=signing_at,
                                    check_at=e['effective_at'],
                                    destination=p.get('destination'))
    authority.save_evaluation(c, e['id'], evaluation)
    if evaluation['outcome'] != 'authorized':
        reason = evaluation['reasons'][0] if evaluation['reasons'] else 'insufficient-authority'
        return s.decide(c, e, NAME, 'blocked', reason, 'blocked')
    if p.get('artifact') and str(p['artifact']) == sid:
        return s.decide(c, e, NAME, 'blocked', 'addendum-must-not-reuse-original-artifact', 'blocked')
    version = int(p.get('version', 1))
    authority.save_claim(c, artifact, version, protocol, evaluation, 'low-value-addendum',
                         int(p.get('start', signing_at)), int(p.get('end', e['effective_at'])),
                         signing_at=signing_at)
    c.execute('INSERT OR REPLACE INTO subjects VALUES(?,?,?,?)',
              (artifact, 'artifact', 'authorized', version))
    c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',
              (e['id'], artifact, 'permit', version,
               f'{int(p.get("start", signing_at))}:{int(p.get("end", e["effective_at"]))}'))
    return s.decide(c, e, NAME, 'authorized', 'low-value-addendum-within-path-limit', 'authorized')


def apply(c, e):
    a = e['action']; p = e.get('data', {}); sid = e['subject']; old = s.latest(c, 'subjects', sid)
    if a == 'authorize':
        begin, end = int(p['start']), int(p['end'])
        if end < begin:
            return s.decide(c, e, NAME, 'blocked', 'invalid-period', 'blocked')
        evaluation, protocol, signing_at = _assess(c, e, p, sid)
        revoked = c.execute("SELECT MAX(version) v FROM documents WHERE subject=? AND kind='permit-revocation'", (sid,)).fetchone()['v'] or 0
        periods = c.execute("SELECT state FROM documents WHERE subject=? AND kind='permit' AND version>?", (sid, revoked)).fetchall()
        if any(not (end < int(x['state'].split(':')[0]) or begin > int(x['state'].split(':')[1])) for x in periods):
            return s.decide(c, e, NAME, 'blocked', 'overlapping-loan', 'blocked')
        version = old['version'] + 1 if old else int(p.get('version', 1))
        if evaluation is not None:
            authority.save_evaluation(c, e['id'], evaluation)
            if evaluation['outcome'] != 'authorized':
                reason = evaluation['reasons'][0] if evaluation['reasons'] else 'insufficient-authority'
                return s.decide(c, e, NAME, 'blocked', reason, 'blocked')
            destination = evaluation.get('destination')
            if evaluation.get('valuation') is None:
                return s.decide(c, e, NAME, 'blocked', 'valuation-missing', 'blocked')
            if evaluation.get('signature_time') != evaluation.get('signing_at'):
                # The scanned signature must precede (or define) this agreement.
                return s.decide(c, e, NAME, 'blocked', 'signing-time-mismatch', 'blocked')
            if signing_at is not None and int(signing_at) != int(evaluation['signing_at']):
                return s.decide(c, e, NAME, 'blocked', 'signing-time-mismatch', 'blocked')
            if destination and destination not in evaluation['selected_path']['destinations']:
                return s.decide(c, e, NAME, 'blocked', 'destination-out-of-scope', 'blocked')
            if int(evaluation['valuation']) > int(evaluation['narrowest_limit']):
                return s.decide(c, e, NAME, 'blocked', 'value-exceeds-delegated-limit', 'blocked')
            authority.save_claim(c, sid, version, protocol, evaluation, 'exhibition',
                                 begin, end, signing_at=signing_at)
            c.execute('INSERT OR REPLACE INTO subjects VALUES(?,?,?,?)', (sid, 'artifact', 'authorized', version))
            c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',
                      (e['id'], sid, 'permit', version, f'{begin}:{end}'))
            c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',
                      (e['id'] + '-parties', sid, 'parties', version,
                       f'{p.get("lender", "museum-A")}:{p.get("borrower", "museum-B")}'))
            return s.decide(c, e, NAME, 'authorized', 'delegated-path-bottleneck-within-value', 'authorized')
        c.execute('INSERT OR REPLACE INTO subjects VALUES(?,?,?,?)', (sid, 'artifact', 'authorized', version))
        c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)', (e['id'], sid, 'permit', version, f'{begin}:{end}'))
        c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)', (e['id'] + '-parties', sid, 'parties', version, f'{p.get("lender", "museum-A")}:{p.get("borrower", "museum-B")}'))
        return s.decide(c, e, NAME, 'authorized', 'period-and-object-approved', 'authorized')
    if a == 'addendum':
        return record_addendum(c, e, p, sid)
    if a == 'revoke':
        if not old:
            return s.decide(c, e, NAME, 'blocked', 'no-loan', 'blocked')
        c.execute("UPDATE subjects SET state='revoked',version=version+1 WHERE id=?", (sid,))
        c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)', (e['id'], sid, 'permit-revocation', old['version'] + 1, 'all-prior-permits'))
        c.execute("UPDATE allocations SET state='intercept' WHERE subject=? AND state IN ('sealed','in-transit')", (sid,))
        return s.decide(c, e, NAME, 'revoked', 'in-transit-intercept-required', 'revoked')
    if a == 'extend':
        if not old or old['state'] not in ('authorized', 'paused') or int(p.get('end', 0)) <= e['effective_at']:
            return s.decide(c, e, NAME, 'blocked', 'invalid-extension', 'blocked')
        version = old['version'] + 1
        c.execute('UPDATE subjects SET state=?,version=? WHERE id=?', ('authorized', version, sid))
        c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)', (e['id'], sid, 'permit', version, f'{e["effective_at"]}:{p["end"]}'))
        return s.decide(c, e, NAME, 'extended', 'exhibition-term-renegotiated', 'authorized')
    if a == 'liability':
        damaged = c.execute("SELECT 1 FROM receipts WHERE subject=? AND kind='damage'", (sid,)).fetchone()
        broken = c.execute("SELECT 1 FROM events WHERE subject=? AND subsystem='transit' AND action='break'", (sid,)).fetchone()
        cover = c.execute("SELECT state FROM documents WHERE subject=? AND kind='insurance' ORDER BY version DESC LIMIT 1", (sid,)).fetchone()
        covered = bool(cover and int(cover['state'].split(':')[0]) <= e['effective_at'] <= int(cover['state'].split(':')[1]))
        result = 'insured-carrier-review' if damaged and broken and covered else 'carrier-review' if damaged and broken else 'lender-review' if damaged else 'clear'
        c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)', (e['id'], sid, 'liability', result, 0))
        return s.decide(c, e, NAME, result, 'seal-and-condition-evidence-compared', result)
    if a == 'insurance':
        if not old or old['state'] != 'authorized' or int(p.get('end', 0)) < int(p.get('start', 0)):
            return s.decide(c, e, NAME, 'blocked', 'invalid-coverage', 'blocked')
        c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)', (e['id'], sid, 'insurance', old['version'], f'{p["start"]}:{p["end"]}'))
        return s.decide(c, e, NAME, 'covered', 'insurance-window-bound-to-loan', 'covered')
    if a == 'approve_reseal':
        broken = s.latest(c, 'allocations', p.get('box', ''))
        if not broken or broken['subject'] != sid or broken['state'] != 'broken':
            return s.decide(c, e, NAME, 'blocked', 'no-broken-box', 'blocked')
        c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)', (e['id'], sid, 'reseal-approval', old['version'] if old else 0, broken['id']))
        return s.decide(c, e, NAME, 'approved', 'broken-seal-remains-in-history', 'approved')
    return s.decide(c, e, NAME, 'blocked', 'unknown-action', 'blocked')
