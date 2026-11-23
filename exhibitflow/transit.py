from . import authority
from . import storage as s

NAME = '包装护运'


def _evaluate_seal(c, e, p, sid):
    # The governing claim/signature supplies protocol, signing time and
    # destination when the transit event does not repeat them.
    resolved = authority.governing(c, sid, e['effective_at'])
    protocol = str(p.get('protocol') or resolved['protocol'])
    signing_at = p.get('signing_at', p.get('signed_at'))
    try:
        signing_at = int(signing_at) if signing_at is not None else None
    except (TypeError, ValueError):
        signing_at = None
    if signing_at is None and resolved.get('signing_at') is not None:
        signing_at = int(resolved['signing_at'])
    destination = p.get('destination') or resolved.get('destination')
    if not authority.has_evidence(c, protocol, sid, signing_at if signing_at is not None else e['effective_at']):
        return None
    return authority.evaluate(c, protocol, sid,
                              signing_at=signing_at,
                              check_at=e['effective_at'],
                              destination=destination)


def apply(c, e):
    a = e['action']; p = e.get('data', {}); sid = e['subject']; loan = s.latest(c, 'subjects', sid)
    if a == 'seal':
        if not loan or loan['state'] != 'authorized':
            return s.decide(c, e, NAME, 'blocked', 'contract-not-authorized', 'blocked')
        permits = c.execute("SELECT state FROM documents WHERE subject=? AND kind='permit'", (sid,)).fetchall()
        if not any(int(x['state'].split(':')[0]) <= e['effective_at'] <= int(x['state'].split(':')[1]) for x in permits):
            return s.decide(c, e, NAME, 'blocked', 'outside-loan-period', 'blocked')
        evaluation = _evaluate_seal(c, e, p, sid)
        if evaluation is not None:
            authority.save_evaluation(c, e['id'], evaluation)
            if evaluation['outcome'] != 'authorized':
                reason = evaluation['reasons'][0] if evaluation['reasons'] else 'insufficient-authority'
                authority.compensate(c, e, sid, '', 'stop-pre-seal-release', evaluation)
                return s.decide(c, e, NAME, 'blocked', reason, 'blocked')
        if not p.get('seal') or int(p.get('items', 0)) < 1:
            return s.decide(c, e, NAME, 'blocked', 'box-list-or-seal-missing', 'blocked')
        c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)', (e['id'], sid, p['seal'], 'sealed', loan['version']))
        return s.decide(c, e, NAME, 'sealed', 'box-ready-for-release', 'sealed')
    if a == 'handoff':
        box = s.latest(c, 'allocations', p.get('box', ''))
        if not box or box['subject'] != sid or box['state'] not in ('sealed', 'in-transit', 'broken'):
            return s.decide(c, e, NAME, 'blocked', 'no-valid-box', 'blocked')
        stoppers = c.execute("SELECT id,detail FROM compensations WHERE subject=? AND box=? AND kind='stop-next-transfer' AND effective_at<=? ORDER BY effective_at,id",
                             (sid, box['id'], e['effective_at'])).fetchall()
        evaluation = _evaluate_seal(c, e, p, sid)
        if evaluation is not None and evaluation['outcome'] != 'authorized':
            reason = evaluation['reasons'][0] if evaluation['reasons'] else 'insufficient-authority'
            if not stoppers:
                authority.compensate(c, e, sid, box['id'], 'stop-next-transfer', evaluation)
            return s.decide(c, e, NAME, 'blocked', reason, 'blocked')
        if stoppers and evaluation is None:
            return s.decide(c, e, NAME, 'blocked', 'compensation-stops-transfer', 'blocked')
        if not p.get('from') or not p.get('to') or p['from'] == p['to']:
            return s.decide(c, e, NAME, 'blocked', 'custody-gap', 'blocked')
        previous = c.execute("SELECT state FROM receipts WHERE subject=? AND kind='handoff' ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
        parties = c.execute("SELECT state FROM documents WHERE subject=? AND kind='parties' ORDER BY version DESC LIMIT 1", (sid,)).fetchone()
        lender, borrower = parties['state'].split(':') if parties else ('museum-A', 'museum-B')
        if not previous and p['from'] != lender:
            return s.decide(c, e, NAME, 'blocked', 'initial-custodian-mismatch', 'blocked')
        if previous and previous['state'] != p['from']:
            return s.decide(c, e, NAME, 'blocked', 'previous-custodian-mismatch', 'blocked')
        state = 'broken-arrived' if p['to'] == borrower and box['state'] == 'broken' else 'arrived' if p['to'] == borrower else 'broken' if box['state'] == 'broken' else 'in-transit'
        c.execute('UPDATE allocations SET state=? WHERE id=?', (state, box['id']))
        c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)', (e['id'], sid, 'handoff', p['to'], 0))
        return s.decide(c, e, NAME, state, 'custody-transferred', state)
    if a == 'break':
        box = s.latest(c, 'allocations', p.get('box', ''))
        if not box or box['subject'] != sid or box['state'] != 'in-transit':
            return s.decide(c, e, NAME, 'blocked', 'no-in-transit-box', 'blocked')
        c.execute("UPDATE allocations SET state='broken' WHERE id=?", (box['id'],))
        return s.decide(c, e, NAME, 'quarantine', 'seal-broken-inspection-required', 'broken')
    if a == 'reseal':
        box = s.latest(c, 'allocations', p.get('box', ''))
        approval = c.execute("SELECT 1 FROM documents WHERE subject=? AND kind='reseal-approval' AND state=?", (sid, p.get('box', ''))).fetchone()
        if not box or box['state'] != 'broken' or not approval or not p.get('new_seal'):
            return s.decide(c, e, NAME, 'blocked', 'reseal-not-approved', 'blocked')
        c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)', (e['id'], sid, p['new_seal'], 'sealed', box['version'] + 1))
        return s.decide(c, e, NAME, 'resealed', 'old-broken-box-retained', 'resealed')
    if a == 'return_plan':
        if not loan or loan['state'] not in ('authorized', 'paused', 'revoked'):
            return s.decide(c, e, NAME, 'blocked', 'no-return-contract', 'blocked')
        state = 'urgent-return' if loan['state'] in ('paused', 'revoked') else 'scheduled-return'
        c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)', (e['id'], sid, p.get('route', 'return'), state, loan['version']))
        return s.decide(c, e, NAME, state, 'contract-status-drives-return', state)
    return s.decide(c, e, NAME, 'blocked', 'unknown-action', 'blocked')
