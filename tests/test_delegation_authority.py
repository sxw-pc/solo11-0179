import json
import tempfile
import unittest
from pathlib import Path

from exhibitflow import authority, cli, storage

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'

BOARD = {'name': 'Board', 'employee_id': 'board', 'title': 'board-of-directors'}
DIRECTOR = {'name': 'Iris', 'employee_id': 'DIR-1', 'title': 'director'}
CURATOR = {'name': 'Curator C', 'employee_id': 'CUR-2', 'title': 'curator'}
ASSISTANT = {'name': 'Assistant D', 'employee_id': 'AST-3', 'title': 'assistant'}


class DelegationAuthority(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = storage.connect(Path(self.tmp.name) / 'authority.db')
        self.n = 0

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def event(self, sub, action, subject='artifact-1', t=None, **data):
        self.n += 1
        t = self.n if t is None else t
        return cli.run_event(self.con, sub, {
            'id': f'e{self.n}', 'subject': subject, 'action': action,
            'data': data, 'effective_at': t, 'received_at': t})

    def delegation(self, doc, delegator, delegate, start, end, limit, destinations,
                   redelegable=True, root=False, t=None, subject='artifact-1'):
        return self.event('authority', 'delegation', subject, t or self.n,
                          source_doc=doc, delegator=delegator, delegate=delegate,
                          valid_from=start, valid_to=end, max_value=limit,
                          destinations=destinations, redelegable=redelegable,
                          signature={'seal': f'{doc}-sig'}, root_authority=root)

    def valuation(self, value, t, artifact='artifact-1', doc=None):
        return self.event('authority', 'valuation', artifact, t,
                          artifact=artifact, value=value, destination='B',
                          source_doc=doc or f'valuation-{artifact}-{value}',
                          effective_at=t)

    def signature(self, protocol, person, at, destination='B', subject='artifact-1'):
        return self.event('authority', 'signature', subject, at,
                          protocol=protocol, document=protocol, signer=person,
                          at=at, destination=destination,
                          signature={'seal': protocol}, seal_coords={'page': 1, 'x': 1, 'y': 2})

    def authorize(self, t, start=1, end=30, protocol='loan-agreement-1',
                  destination='B', signing_at=6, subject='artifact-1'):
        return self.event('loan', 'authorize', subject, t, start=start, end=end,
                          protocol=protocol, destination=destination,
                          signing_at=signing_at)

    def seal(self, t, protocol='loan-agreement-1', destination='B', signing_at=6,
             subject='artifact-1', seal='S1'):
        return self.event('transit', 'seal', subject, t, protocol=protocol,
                          destination=destination, signing_at=signing_at,
                          seal=seal, items=1)

    def handoff(self, box, t, frm='museum-A', to='carrier', subject='artifact-1', protocol='loan-agreement-1'):
        return self.event('transit', 'handoff', subject, t, box=box,
                          **{'from': frm, 'to': to}, protocol=protocol,
                          destination='B', signing_at=6)

    def chain(self):
        self.delegation('board-resolution-BR-200', BOARD, DIRECTOR, 1, 10, 200, ['B'], True, True)
        self.delegation('director-letter-DIR-100', DIRECTOR, CURATOR, 1, 8, 100, ['B'], True)
        self.delegation('curator-subdelegation-CUR-150', CURATOR, ASSISTANT, 1, 10, 150, ['B'], True)
        self.valuation(120, 2)
        self.signature('loan-agreement-1', ASSISTANT, 6)

    # -- core scenario -----------------------------------------------------
    def test_fixture_chain_bottleneck_is_curator_limit(self):
        rows = json.loads((FIXTURES / 'delegation-chain.json').read_text(encoding='utf-8'))
        for row in rows:
            self.assertEqual(cli.run_event(self.con, row['subsystem'], row['event'])['outcome'], 'recorded')
        result = authority.evaluate(self.con, 'loan-agreement-1', 'artifact-1', check_at=6)
        self.assertEqual(result['narrowest_limit'], 100)
        self.assertEqual(result['valuation'], 120)
        self.assertEqual(result['outcome'], 'insufficient-authority')
        self.assertEqual(result['reasons'], ['value-exceeds-delegated-limit'])
        self.assertIn('board-direct-authorization-for:AST-3', result['missing_approvals'])
        path = result['paths'][0]
        self.assertEqual((path['valid_from'], path['valid_to']), (1, 8))
        self.assertEqual(path['limit'], 100)
        self.assertEqual(result['invalid_edges'], [])

    def test_120_artifact_blocked_on_original_path(self):
        self.chain()
        signed = self.authorize(6)
        self.assertEqual(signed['outcome'], 'blocked')
        self.assertEqual(signed['reason'], 'value-exceeds-delegated-limit')
        self.assertIsNone(storage.latest(self.con, 'subjects', 'artifact-1'))
        self.assertEqual(self.seal(7)['outcome'], 'blocked')
        # no amount of re-running sums the edges to 450; whole path stays at 100
        self.assertEqual(authority.evaluate(self.con, 'loan-agreement-1', 'artifact-1', check_at=6)['narrowest_limit'], 100)

    def test_parallel_paths_do_not_add_up(self):
        self.delegation('board-direct-100', BOARD, ASSISTANT, 1, 10, 100, ['B'], True, True)
        self.delegation('board-via-helper-80a', BOARD, DIRECTOR, 1, 10, 80, ['B'], True, True)
        self.delegation('helper-to-assistant-80', DIRECTOR, ASSISTANT, 1, 10, 80, ['B'], True)
        self.valuation(120, 2)
        self.signature('loan-agreement-1', ASSISTANT, 6)
        result = authority.evaluate(self.con, 'loan-agreement-1', 'artifact-1', check_at=6)
        self.assertEqual(sorted(result['limits_by_path']), [80, 100])
        self.assertEqual(result['narrowest_limit'], 100)
        self.assertNotEqual(result['narrowest_limit'], 180)
        self.assertEqual(result['outcome'], 'insufficient-authority')
        self.assertEqual(self.authorize(6)['reason'], 'value-exceeds-delegated-limit')

    def test_fixed_chain_allows_80(self):
        self.chain()
        self.valuation(80, 4, doc='valuation-revised-80')
        self.assertEqual(authority.find_valuation(self.con, 'artifact-1', 6)['value'], 80)
        self.assertEqual(self.authorize(6)['outcome'], 'authorized')

    def test_omitted_signing_time_uses_scanned_moment(self):
        self.delegation('board-to-assistant', BOARD, ASSISTANT, 1, 10, 100, ['B'], False, True)
        self.valuation(80, 2)
        self.signature('loan-agreement-1', ASSISTANT, 6)
        # event luckily recorded later than the seal: still uses the scan time
        auth = self.event('loan', 'authorize', 'artifact-1', 8,
                          start=1, end=30, protocol='loan-agreement-1', destination='B')
        self.assertEqual(auth['outcome'], 'authorized')
        sealed = self.event('transit', 'seal', 'artifact-1', 9, seal='S1', items=1)
        self.assertEqual(sealed['outcome'], 'sealed')
        # but a deliberately wrong signing time is refused
        wrong = self.event('loan', 'authorize', 'artifact-1', 10, start=40, end=60,
                           protocol='loan-agreement-1', destination='B', signing_at=5)
        self.assertEqual(wrong['outcome'], 'blocked')

    def test_mixed_redelegable_upstream_keeps_valid_branch(self):
        # D's own power from the board cannot be passed on, but a second grant
        # through C can; the valid branch must still be found.
        self.delegation('board-direct-D-nopass', BOARD, DIRECTOR, 1, 10, 50, ['B'], False, True)
        self.delegation('board-to-curator-pass', BOARD, CURATOR, 1, 10, 200, ['B'], True, True)
        self.delegation('curator-to-director-pass', CURATOR, DIRECTOR, 1, 10, 200, ['B'], True)
        self.delegation('director-to-assistant', DIRECTOR, ASSISTANT, 1, 10, 200, ['B'], True)
        self.valuation(120, 2)
        self.signature('loan-agreement-1', ASSISTANT, 6)
        result = authority.evaluate(self.con, 'loan-agreement-1', 'artifact-1', check_at=6)
        self.assertEqual(result['outcome'], 'authorized')
        self.assertEqual(result['narrowest_limit'], 200)
        self.assertEqual([p['limit'] for p in result['paths']], [200])

    # -- evidence versioning ----------------------------------------------
    def test_reprint_and_distinct_source_versions(self):
        first = self.delegation('director-letter-DIR-100', DIRECTOR, CURATOR, 1, 8, 100, ['B'], True)
        reprint = self.delegation('director-letter-DIR-100', DIRECTOR, CURATOR, 1, 8, 100, ['B'], True)
        self.assertIn('reprint-retained', reprint['reason'])
        self.assertEqual(storage.latest(self.con, 'events', 'e1')['id'], 'e1')
        revised = self.delegation('director-letter-DIR-100-rev2', DIRECTOR, CURATOR, 1, 8, 70, ['B'], True)
        self.assertIn('delegation-edge-retained', revised['reason'])
        docs = self.con.execute("SELECT id FROM documents WHERE kind='delegation' ORDER BY id").fetchall()
        self.assertEqual({d['id'] for d in docs},
                         {'delegation:director-letter-DIR-100:v1',
                          'delegation:director-letter-DIR-100-rev2:v1'})

    def test_same_name_different_employee_id(self):
        self.delegation('board-resolution-a', BOARD, DIRECTOR, 1, 10, 200, ['B'], True, True)
        other = dict(DIRECTOR, employee_id='DIR-9', title='deputy-director')
        self.delegation('board-resolution-b', BOARD, other, 1, 10, 200, ['B'], True, True)
        self.delegation('letter-to-real-director', DIRECTOR, ASSISTANT, 1, 10, 100, ['B'], True)
        self.valuation(80, 2)
        self.signature('loan-agreement-1', ASSISTANT, 6)
        result = authority.evaluate(self.con, 'loan-agreement-1', 'artifact-1', check_at=6)
        self.assertEqual(result['outcome'], 'authorized')
        self.assertEqual(result['narrowest_limit'], 100)

    def test_valuation_revisions_keep_history(self):
        self.chain()
        self.valuation(80, 8, doc='valuation-late-80')
        self.assertEqual(authority.find_valuation(self.con, 'artifact-1', 6)['value'], 120)
        self.assertEqual(authority.find_valuation(self.con, 'artifact-1', 8)['value'], 80)
        # the 80 revision cannot backdate the 120 signature at time 6
        self.assertEqual(self.authorize(8, signing_at=6)['outcome'], 'blocked')
        self.signature('loan-agreement-2', ASSISTANT, 8)
        self.assertEqual(self.authorize(8, protocol='loan-agreement-2', signing_at=8)['outcome'],
                         'authorized')

    def test_non_redelegable_edge_blocks_path(self):
        self.delegation('board-resolution-BR-200', BOARD, DIRECTOR, 1, 10, 200, ['B'], False, True)
        self.delegation('director-letter-DIR-100', DIRECTOR, CURATOR, 1, 8, 100, ['B'], True)
        self.delegation('curator-subdelegation-CUR-150', CURATOR, ASSISTANT, 1, 10, 150, ['B'], True)
        self.valuation(80, 2)
        self.signature('loan-agreement-1', ASSISTANT, 6)
        result = authority.evaluate(self.con, 'loan-agreement-1', 'artifact-1', check_at=6)
        self.assertEqual(result['outcome'], 'insufficient-authority')
        self.assertIn('redelegation-not-permitted', result['reasons'])
        self.assertTrue(any(i['reason'] == 'redelegation-not-permitted' for i in result['invalid_edges']))
        self.assertEqual(self.authorize(6)['outcome'], 'blocked')

    def test_destination_scope_intersection(self):
        self.delegation('board-resolution-BR-200', BOARD, DIRECTOR, 1, 10, 200, ['B', 'C'], True, True)
        self.delegation('director-letter-DIR-100', DIRECTOR, CURATOR, 1, 8, 100, ['B'], True)
        self.delegation('curator-subdelegation-CUR-150', CURATOR, ASSISTANT, 1, 10, 150, ['B', 'C'], True)
        self.valuation(80, 2)
        self.signature('loan-agreement-1', ASSISTANT, 6, destination='B')
        self.assertEqual(self.authorize(6)['outcome'], 'authorized')
        out = self.event('authority', 'signature', 'artifact-1', 6,
                         protocol='loan-agreement-C', document='loan-agreement-C',
                         signer=ASSISTANT, at=6, destination='C',
                         signature={'seal': 'x'}, seal_coords={'page': 1})
        self.assertEqual(out['outcome'], 'recorded')
        blocked = self.authorize(6, start=40, end=50, protocol='loan-agreement-C', destination='C')
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertEqual(blocked['reason'], 'destination-out-of-scope')

    # -- revocation --------------------------------------------------------
    def test_late_revocation_stops_next_transfer_but_keeps_history(self):
        self.chain()
        self.valuation(80, 4, doc='valuation-revised-80')
        self.assertEqual(self.authorize(6)['outcome'], 'authorized')
        box = self.seal(7)
        self.assertEqual(box['outcome'], 'sealed')
        self.assertEqual(self.handoff(box['event'], 8)['outcome'], 'in-transit')
        rev = self.event('authority', 'revocation', 'artifact-1', 9,
                         source_doc='hr-revocation-CUR-2', target='director-letter-DIR-100',
                         effective_at=9, person=CURATOR, signature={'seal': 'hr'})
        self.assertIn('revocation-effective-at:9', rev['reason'])
        self.assertEqual(storage.latest(self.con, 'allocations', box['event'])['state'], 'in-transit')
        after = self.handoff(box['event'], 10, frm='carrier', to='museum-B')
        self.assertEqual(after['outcome'], 'blocked')
        self.assertIn(after['reason'], ('revoked-before-movement', 'compensation-stops-transfer'))
        compensations = self.con.execute('SELECT * FROM compensations').fetchall()
        self.assertTrue(compensations)
        self.assertEqual(compensations[-1]['kind'], 'stop-next-transfer')
        receipt = self.con.execute("SELECT COUNT(*) c FROM receipts WHERE kind='handoff'").fetchone()['c']
        self.assertEqual(receipt, 1)  # only the completed leg is physical history
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_seal_resolves_governing_claim_without_repeated_protocol(self):
        self.chain()
        self.valuation(80, 4, doc='valuation-revised-80')
        self.assertEqual(self.authorize(6)['outcome'], 'authorized')
        # no protocol/signing_at passed: transit must reuse the authorized claim
        sealed = self.event('transit', 'seal', 'artifact-1', 7, seal='S1', items=1)
        self.assertEqual(sealed['outcome'], 'sealed')

    def test_movement_after_signing_window_still_uses_historical_grant(self):
        self.chain()
        self.valuation(80, 4, doc='valuation-revised-80')
        self.assertEqual(self.authorize(6)['outcome'], 'authorized')
        # signing at 6 was inside 1-8; a seal at 9 is governed by the permit,
        # not retroactively invalidated by the delegation edge's valid_to
        self.assertEqual(self.seal(9)['outcome'], 'sealed')
        # but a revocation effective before the seal does block it
        self.event('authority', 'revocation', 'artifact-1', 10,
                   source_doc='hr-revocation-CUR-2', target='director-letter-DIR-100',
                   effective_at=9, person=CURATOR, signature={'seal': 'hr'})
        self.assertEqual(self.seal(10, seal='S2')['outcome'], 'blocked')

    def test_future_revocation_only_stops_from_its_effective_moment(self):
        self.delegation('board-to-assistant', BOARD, ASSISTANT, 1, 100, 100, ['B'], False, True)
        self.valuation(80, 2)
        self.signature('loan-agreement-1', ASSISTANT, 6)
        self.assertEqual(self.authorize(6, end=100)['outcome'], 'authorized')
        box = self.seal(7)
        self.event('authority', 'revocation', 'artifact-1', 13,
                   source_doc='hr-future', target='board-to-assistant',
                   effective_at=40, person=BOARD, signature={'seal': 'hr'})
        stop = self.con.execute("SELECT effective_at FROM compensations WHERE box=?",
                                (box['event'],)).fetchone()
        self.assertEqual(stop['effective_at'], 40)
        self.assertEqual(self.handoff(box['event'], 14)['outcome'], 'in-transit')
        self.assertEqual(self.handoff(box['event'], 41, frm='carrier', to='museum-B')['outcome'], 'blocked')
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_revocation_before_signing_blocks_authorization(self):
        self.chain()
        self.valuation(80, 4, doc='valuation-revised-80')
        self.event('authority', 'revocation', 'artifact-1', 5,
                   source_doc='hr-revocation-CUR-2', target='director-letter-DIR-100',
                   effective_at=3, person=CURATOR, signature={'seal': 'hr'})
        result = authority.evaluate(self.con, 'loan-agreement-1', 'artifact-1', check_at=6)
        self.assertEqual(result['outcome'], 'revoked')
        self.assertIn('revoked-before-movement', result['reasons'])
        self.assertTrue(any(i['reason'] == 'revoked-at:3' for i in result['invalid_edges']))
        self.assertEqual(self.authorize(6)['reason'], 'revoked-before-movement')

    # -- remedies ----------------------------------------------------------
    def test_remedy_a_board_direct_new_resolution(self):
        self.chain()
        self.assertEqual(self.authorize(6)['outcome'], 'blocked')
        self.delegation('board-resolution-BR-direct-D', BOARD, ASSISTANT, 7, 10, 200, ['B'],
                        False, True, t=7)
        self.signature('loan-agreement-2', ASSISTANT, 8)
        self.assertEqual(self.authorize(8, start=7, end=10, protocol='loan-agreement-2', signing_at=8)['outcome'],
                         'authorized')
        self.assertEqual(self.seal(9, protocol='loan-agreement-2', signing_at=8)['outcome'], 'sealed')
        old = authority.latest_evaluation(self.con, 'loan-agreement-1')
        self.assertEqual(old['outcome'], 'insufficient-authority')
        self.assertEqual(old['signing_at'], 6)
        self.assertEqual(old['narrowest_limit'], 100)
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_remedy_b_low_value_addendum(self):
        self.chain()
        self.assertEqual(self.authorize(6)['outcome'], 'blocked')
        addendum_artifact = 'artifact-1-low-value-addendum'
        self.signature('addendum-1', ASSISTANT, 6, subject=addendum_artifact)
        self.valuation(80, 6, artifact=addendum_artifact, doc='valuation-addendum-80')
        added = self.event('loan', 'addendum', 'artifact-1', 7,
                           protocol='addendum-1', artifact=addendum_artifact,
                           start=1, end=10, signing_at=6, destination='B')
        self.assertEqual(added['outcome'], 'authorized')
        # original 120 artifact still cannot leave
        self.assertEqual(self.seal(7)['outcome'], 'blocked')
        # addendum can be sealed under the same C->D path
        sealed = self.seal(7, subject=addendum_artifact, protocol='addendum-1', seal='S2')
        self.assertEqual(sealed['outcome'], 'sealed')
        # the 120 original never received an authorized version or a permit
        self.assertIsNone(storage.latest(self.con, 'subjects', 'artifact-1'))
        self.assertEqual(storage.latest(self.con, 'subjects', addendum_artifact)['state'], 'authorized')
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) c FROM documents WHERE subject='artifact-1' AND kind='permit'").fetchone()['c'], 0)
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_addendum_cannot_masquerade_as_original(self):
        self.chain()
        self.valuation(80, 6, artifact='artifact-1-low-value-addendum', doc='valuation-addendum-80')
        self.signature('addendum-1', ASSISTANT, 6)
        reuse = self.event('loan', 'addendum', 'artifact-1', 7,
                           protocol='addendum-1', artifact='artifact-1',
                           start=1, end=10, signing_at=6, destination='B')
        self.assertEqual(reuse['outcome'], 'blocked')
        self.assertEqual(self.seal(7)['outcome'], 'blocked')

    def test_late_evidence_stops_legacy_sealed_box(self):
        # A box sealed before any authority scans exists must not slip through
        # once the chain is imported afterwards.
        self.assertEqual(self.event('loan', 'authorize', 'artifact-1', 1,
                                    start=1, end=30)['outcome'], 'authorized')
        box = self.event('transit', 'seal', 'artifact-1', 2, seal='S1', items=1)
        self.assertEqual(box['outcome'], 'sealed')
        self.delegation('board-resolution-BR-200', BOARD, DIRECTOR, 1, 10, 200, ['B'], True, True, t=3)
        self.delegation('director-letter-DIR-100', DIRECTOR, CURATOR, 1, 10, 100, ['B'], True, t=4)
        stops = self.con.execute("SELECT * FROM compensations WHERE box=?", (box['event'],)).fetchall()
        self.assertEqual(len(stops), 1)
        self.assertEqual(self.handoff(box['event'], 5)['outcome'], 'blocked')
        self.assertEqual(storage.latest(self.con, 'allocations', box['event'])['state'], 'sealed')
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_wrongly_sealed_box_cannot_be_handed_off(self):
        self.chain()
        self.valuation(80, 4, doc='valuation-revised-80')
        self.assertEqual(self.authorize(6)['outcome'], 'authorized')
        box = self.seal(7)
        self.assertEqual(box['outcome'], 'sealed')
        self.assertEqual(self.handoff(box['event'], 8)['outcome'], 'in-transit')
        self.event('authority', 'revocation', 'artifact-1', 9,
                   source_doc='hr-revocation-CUR-2', target='director-letter-DIR-100',
                   effective_at=9, person=CURATOR, signature={'seal': 'hr'})
        # physical chain is intact: box still in transit and recorded once
        self.assertEqual(storage.latest(self.con, 'allocations', box['event'])['state'], 'in-transit')
        self.assertEqual(self.handoff(box['event'], 10, frm='carrier', to='museum-B')['outcome'], 'blocked')
        stops = self.con.execute(
            "SELECT * FROM compensations WHERE box=? AND kind='stop-next-transfer'", (box['event'],)).fetchall()
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]['subject'], 'artifact-1')
        detail = json.loads(stops[0]['detail'])
        self.assertTrue(any(i['reason'].startswith('revoked-at:') for i in detail['invalid_edges']))
        self.assertEqual(detail['signing_at'], 6)
        # the old authorized evaluation at the signing moment is still
        # retrievable for responsibility replay
        history = authority.evaluations(self.con, 'loan-agreement-1')
        self.assertTrue(any(e['check_at'] == 6 and e['outcome'] == 'authorized' for e in history))
        # the revocation-driven stop is itself a replayable responsibility version
        self.assertTrue(any(e['check_at'] == 9 and e['outcome'] == 'revoked' for e in history))
        self.assertTrue(cli.audit(self.con)['ok'])


class LegacyRegression(unittest.TestCase):
    """Existing contracts without authority scans keep the historical path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = storage.connect(Path(self.tmp.name) / 'legacy.db')
        self.n = 0

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def run_event(self, sub, action, t, **data):
        self.n += 1
        return cli.run_event(self.con, sub, {'id': f'l{self.n}', 'subject': 'artifact-old',
                                             'action': action, 'data': data,
                                             'effective_at': t, 'received_at': t})

    def test_legacy_authorize_and_seal_still_work(self):
        self.assertEqual(self.run_event('loan', 'authorize', 1, start=1, end=30)['outcome'], 'authorized')
        sealed = self.run_event('transit', 'seal', 2, seal='S1', items=1)
        self.assertEqual(sealed['outcome'], 'sealed')
        self.assertEqual(self.run_event('transit', 'handoff', 3, box=sealed['event'],
                                        **{'from': 'museum-A', 'to': 'carrier'})['outcome'], 'in-transit')

    def test_legacy_revocation_intercepts(self):
        self.run_event('loan', 'authorize', 1, start=1, end=30)
        sealed = self.run_event('transit', 'seal', 2, seal='S1', items=1)
        self.run_event('loan', 'revoke', 3)
        self.assertEqual(storage.latest(self.con, 'allocations', sealed['event'])['state'], 'intercept')
        self.run_event('transit', 'handoff', 4, box=sealed['event'],
                       **{'from': 'museum-A', 'to': 'carrier'})
        self.assertTrue(cli.audit(self.con)['ok'])


if __name__ == '__main__':
    unittest.main()
