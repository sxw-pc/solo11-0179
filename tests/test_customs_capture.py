"""海关双栏放行单取证：逐件归属、错关联撤销与审计。

固定合成数据：一箱 artifact-1/artifact-2，车次 QR=TR-8；左栏 X-31 允许目的
地 B，右栏 X-32 只允许本地转运；票面不写件号。车次二维码不能推出件号归属。
"""
import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from exhibitflow import cli, customs, storage

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'customs-columns.json'


def load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def sha_b64(text):
    return hashlib.sha256(base64.b64decode(text)).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = storage.connect(Path(self.tmp.name) / 'db.sqlite')
        self.n = 0
        self.fx = load()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def ev(self, sub, action, subject='doc', effective_at=None, received_at=None, **data):
        self.n += 1
        return cli.run_event(self.con, sub, {
            'id': f'e{self.n}', 'subject': subject, 'action': action, 'data': data,
            'effective_at': self.n if effective_at is None else effective_at,
            'received_at': self.n if received_at is None else received_at,
        })

    def manifest(self):
        return self.ev('customs', 'register', **self.fx['manifest'])

    def capture(self, page=1, with_tokens=True, media='ZGVtby1pbWFnZQ==', **over):
        columns = over.pop('columns', None)
        tokens = over.pop('tokens', None)
        if columns is None:
            columns = [{k: v for k, v in c.items() if k != 'ocr_tokens'} for c in self.fx['columns']]
        kwargs = dict(document=self.fx['document'], page=page, trip=self.fx['trip'],
                      box=self.fx['box'], seal=self.fx['seal'], columns=columns,
                      permitted_destinations=['B', 'LOCAL'],
                      media={'mime': 'image/png', 'bytes_b64': media},
                      candidates=[self.fx['qr']])
        if tokens is None and with_tokens:
            tokens = [t for c in self.fx['columns'] for t in c['ocr_tokens']]
        if tokens is not None:
            kwargs['tokens'] = tokens
        kwargs.update(over)
        return self.ev('customs', 'capture', **kwargs)

    def attribute(self, kind, rows, observed_at, media=None):
        kwargs = dict(document=self.fx['document'], trip=self.fx['trip'],
                      box=self.fx['box'], seal=self.fx['seal'], kind=kind,
                      observed_at=observed_at, assignments=rows)
        if media is not None:
            kwargs['media'] = media
        return self.ev('customs', 'attribute', **kwargs)

    def status(self, artifact):
        res = customs.resolution(self.con, self.fx['trip'], self.fx['document'])
        return res['items'][artifact]['status']

    def gate(self, artifact):
        return customs.release_check(self.con, artifact, trip=self.fx['trip'])

    def boxes(self):
        return storage.report(self.con)


class CaptureAndColumns(Base):
    def test_capture_stores_original_media_and_duplicate_digest(self):
        self.manifest()
        r = self.capture()
        self.assertEqual(r['outcome'], 'captured')
        media = self.con.execute('SELECT bytes,digest FROM customs_media').fetchall()
        self.assertEqual(len(media), 1)
        self.assertEqual(bytes(media[0]['bytes']), base64.b64decode('ZGVtby1pbWFnZQ=='))
        self.assertEqual(media[0]['digest'], hashlib.sha256(base64.b64decode('ZGVtby1pbWFnZQ==')).hexdigest())

    def test_seal_must_cover_the_piece_number_region(self):
        self.manifest()
        columns = [{k: v for k, v in c.items() if k != 'ocr_tokens'} for c in self.fx['columns']]
        columns[0]['signature'] = copy.deepcopy(columns[0]['signature'])
        columns[0]['signature'].update({'x': 200, 'y': 200, 'w': 10, 'h': 10})
        r = self.capture(columns=columns)
        self.assertEqual(r['outcome'], 'blocked')
        self.assertEqual(r['reason'], 'signature-does-not-cover-column')

    def test_tampered_signature_bytes_rejected(self):
        self.manifest()
        columns = [{k: v for k, v in c.items() if k != 'ocr_tokens'} for c in self.fx['columns']]
        columns[1]['signature'] = copy.deepcopy(columns[1]['signature'])
        columns[1]['signature']['bytes_b64'] = base64.b64encode(b'tampered').decode()
        r = self.capture(columns=columns)
        self.assertEqual(r['outcome'], 'blocked')
        self.assertEqual(r['reason'], 'signature-digest-mismatch')

    def test_ocr_outside_column_region_flagged(self):
        self.manifest()
        tokens = []
        for i, t in enumerate(self.fx['columns'][0]['ocr_tokens']):
            t = dict(t)
            t['y'] = 500  # far below X-31's region
            tokens.append(t)
        tokens += self.fx['columns'][1]['ocr_tokens']
        r = self.capture(tokens=tokens)
        self.assertEqual(r['outcome'], 'captured-with-ocr-mismatch')
        self.assertFalse(r['data']['ocr']['X-31']['ok'])

    def test_duplicate_column_number_rejected(self):
        self.manifest()
        columns = [{k: v for k, v in c.items() if k != 'ocr_tokens'} for c in self.fx['columns']]
        dup = copy.deepcopy(columns[0])
        dup['signature'] = copy.deepcopy(dup['signature'])
        r = self.capture(columns=columns + [dup])
        self.assertEqual(r['outcome'], 'blocked')
        self.assertEqual(r['reason'], 'duplicate-column-number')


class UnresolvedAttribution(Base):
    def test_no_per_item_release_from_trip_qr(self):
        self.manifest()
        self.assertEqual(self.capture()['outcome'], 'captured')
        self.assertEqual(self.status('artifact-1'), 'unresolved')
        self.assertEqual(self.status('artifact-2'), 'unresolved')
        for a in ('artifact-1', 'artifact-2'):
            gate = self.gate(a)
            self.assertTrue(gate['required'])
            self.assertFalse(gate['allowed'])

    def test_two_complete_matchings_retained_not_arbitrary(self):
        self.manifest()
        self.capture()
        res = customs.resolution(self.con, self.fx['trip'], self.fx['document'])
        self.assertEqual(len(res['complete_matchings']), 2)
        self.assertEqual(
            {tuple(sorted(m.items())) for m in res['complete_matchings']},
            {(('artifact-1', 'X-31'), ('artifact-2', 'X-32')),
             (('artifact-1', 'X-32'), ('artifact-2', 'X-31'))},
        )
        # neither item is given a concrete column in the unresolved view
        self.assertIsNone(res['items']['artifact-1']['column'])
        self.assertIsNone(res['items']['artifact-2']['column'])
        # the package names both acceptable evidence paths as still to collect
        self.assertIn('sealing-time-signed-per-item-scan-ledger', res['missing_originals'])
        self.assertIn('customs-independent-unboxing-recheck', res['missing_originals'])
        pkg = customs.package(self.con, 'artifact-1', self.fx['trip'], self.fx['document'])
        self.assertEqual(pkg['status'], 'unresolved')
        self.assertEqual(len(pkg['alternatives']), 2)
        self.assertIn('competing-complete-matchings-retained-no-column-favoured', pkg['limitations'])

    def test_evidence_for_wrong_box_or_seal_rejected(self):
        self.manifest()
        self.capture()
        wrong_box = self.ev('customs', 'attribute', document=self.fx['document'], trip=self.fx['trip'],
                            box='BOX-OTHER', seal=self.fx['seal'], kind='sealing_scan', observed_at=2,
                            assignments=[{'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-31'}])
        self.assertEqual(wrong_box['outcome'], 'blocked')
        self.assertEqual(wrong_box['reason'], 'box-does-not-match-release-sheet')
        wrong_seal = self.ev('customs', 'attribute', document=self.fx['document'], trip=self.fx['trip'],
                             box=self.fx['box'], seal='seal-OTHER', kind='sealing_scan', observed_at=2,
                             assignments=[{'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-31'}])
        self.assertEqual(wrong_seal['reason'], 'seal-does-not-match-release-sheet')

    def test_sealing_scan_requires_collection_code(self):
        self.manifest()
        self.capture()
        # register without a manifest mapping for artifact-9 forces a missing code
        r = self.ev('customs', 'attribute', document=self.fx['document'], trip=self.fx['trip'],
                    box=self.fx['box'], seal=self.fx['seal'], kind='sealing_scan', observed_at=2,
                    assignments=[{'artifact': 'artifact-9', 'col_no': 'X-31'}])
        self.assertEqual(r['outcome'], 'blocked')
        self.assertEqual(r['reason'], 'sealing-scan-requires-collection-code')

    def test_qr_trip_conflict_rejected(self):
        self.manifest()
        columns = [{k: v for k, v in c.items() if k != 'ocr_tokens'} for c in self.fx['columns']]
        r = self.ev('customs', 'capture', document=self.fx['document'], page=1, trip=self.fx['trip'],
                    qr='TR-9', box=self.fx['box'], seal=self.fx['seal'], columns=columns,
                    media={'mime': 'image/png', 'bytes_b64': 'ZGVtby1pbWFnZQ=='})
        self.assertEqual(r['outcome'], 'blocked')
        self.assertEqual(r['reason'], 'qr-trip-mismatch')

    def test_legacy_rpa_association_is_not_release_proof(self):
        self.manifest()
        self.capture()
        self.ev('customs', 'legacy_associate', document=self.fx['document'], trip=self.fx['trip'],
                artifact='artifact-1', col_no='X-31')
        # provisional only; the downstream gate must not treat it as clearance
        self.assertFalse(self.gate('artifact-1')['allowed'])
        self.assertEqual(self.gate('artifact-1')['status'], 'unresolved')

    def test_whole_trip_cleared_cannot_double_book_a_column(self):
        self.manifest()
        self.capture()
        # one sealing scan assigning both artifacts to the same column is rejected
        r = self.attribute('sealing_scan', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-31'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
        ], observed_at=2)
        self.assertEqual(r['outcome'], 'blocked')
        self.assertEqual(r['reason'], 'column-claimed-by-two-artifacts')
        self.assertEqual(self.con.execute('SELECT COUNT(*) c FROM customs_column_claims').fetchone()['c'], 0)


class TwoEvidencePaths(Base):
    def test_sealing_scan_confirms_artifact1_export(self):
        self.manifest()
        self.capture()
        r = self.attribute('sealing_scan', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-31'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-32'},
        ], observed_at=2)
        self.assertEqual(r['outcome'], 'attributed')
        self.assertEqual(self.status('artifact-1'), 'confirmed')
        self.assertEqual(self.status('artifact-2'), 'held')
        self.assertTrue(self.gate('artifact-1')['allowed'])
        self.assertFalse(self.gate('artifact-2')['allowed'])

    def test_unboxing_recheck_reverses_the_list(self):
        self.manifest()
        self.capture()
        # sealing scan says artifact-1=X-31
        self.attribute('sealing_scan', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-31'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-32'},
        ], observed_at=2)
        # independent recheck finds the opposite physical reality
        r = self.attribute('unboxing_recheck', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
        ], observed_at=6)
        self.assertEqual(r['outcome'], 'attributed')
        self.assertEqual(self.status('artifact-1'), 'held')
        self.assertEqual(self.status('artifact-2'), 'confirmed')
        self.assertFalse(self.gate('artifact-1')['allowed'])
        self.assertTrue(self.gate('artifact-2')['allowed'])

    def test_two_paths_deliver_opposite_lists_from_same_ticket(self):
        outcomes = []
        for kind, rows, at in [
            ('sealing_scan', [('artifact-1', 'X-31'), ('artifact-2', 'X-32')], 2),
            ('unboxing_recheck', [('artifact-1', 'X-32'), ('artifact-2', 'X-31')], 6),
        ]:
            with tempfile.TemporaryDirectory() as tmp:
                con = storage.connect(Path(tmp) / 'o.db')
                self.con = con
                self.n = 0
                self.manifest()
                self.capture()
                self.attribute(kind, [{'artifact': a, 'collection_code': f'COL-ART-000{i}',
                                       'col_no': c} for i, (a, c) in enumerate(rows, 1)], observed_at=at)
                outcomes.append((self.status('artifact-1'), self.status('artifact-2')))
                con.close()
        self.assertEqual(outcomes, [('confirmed', 'held'), ('held', 'confirmed')])

    def test_package_records_evidence_limitations(self):
        self.manifest()
        self.capture()
        self.attribute('sealing_scan', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-31'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-32'},
        ], observed_at=2)
        pkg = customs.package(self.con, 'artifact-1', self.fx['trip'], self.fx['document'])
        self.assertEqual(pkg['status'], 'confirmed')
        self.assertIn('sealing-scan-confirms-historical-boxing-not-current-box-contents', pkg['limitations'])
        self.assertTrue(pkg['originals']['media'])
        self.assertTrue(pkg['originals']['signatures'])

    def test_evidence_ledger_media_preserved(self):
        self.manifest()
        self.capture()
        media = self.fx['seal_scan']['media']
        self.attribute('sealing_scan', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-31'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-32'},
        ], observed_at=2, media=media)
        rows = self.con.execute("SELECT bytes,digest FROM customs_media WHERE kind='sealing_scan'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['digest'], sha_b64(media['bytes_b64']))
        self.assertEqual(bytes(rows[0]['bytes']), base64.b64decode(media['bytes_b64']))
        # the per-item evidence payload commits to the ledger media digest
        ev = self.con.execute("SELECT payload FROM customs_evidence WHERE artifact='artifact-1'").fetchone()
        self.assertIn(rows[0]['digest'], ev['payload'])
        pkg = customs.package(self.con, 'artifact-1', self.fx['trip'], self.fx['document'])
        media_digests = {m['digest'] for m in pkg['originals']['media']}
        self.assertIn(rows[0]['digest'], media_digests)


class LegacyDefectAndInterception(Base):
    def test_old_rpa_defect_voids_and_intercepts_in_transit(self):
        # reproduce the old authorize->seal->handoff chain for artifact-1
        self.assertEqual(self.ev('loan', 'authorize', subject='artifact-1', start=1, end=20)['outcome'], 'authorized')
        sealed = self.ev('transit', 'seal', subject='artifact-1', seal='seal-A', items=1)
        self.assertEqual(sealed['outcome'], 'sealed')
        self.assertEqual(self.ev('transit', 'handoff', subject='artifact-1', box=sealed['event'],
                                 **{'from': 'museum-A', 'to': 'carrier'})['outcome'], 'in-transit')
        allocation = sealed['event']

        self.manifest()
        self.capture()
        self.ev('customs', 'legacy_associate', document=self.fx['document'], trip=self.fx['trip'],
                artifact='artifact-1', col_no='X-31')
        self.assertEqual(storage.latest(self.con, 'allocations', allocation)['state'], 'in-transit')

        r = self.attribute('unboxing_recheck', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
        ], observed_at=6)
        self.assertEqual(r['outcome'], 'attributed-with-voids')
        self.assertEqual(self.status('artifact-1'), 'held')
        self.assertEqual(self.status('artifact-2'), 'confirmed')
        # reverse-void record appended, old attribution left intact
        voids = self.con.execute("SELECT * FROM customs_voids WHERE artifact='artifact-1'").fetchall()
        self.assertEqual(len(voids), 1)
        self.assertEqual(voids[0]['target_kind'], 'attribution')
        self.assertEqual(voids[0]['reason'], 'authoritative-evidence-assigns-different-column')
        legacy = self.con.execute("SELECT * FROM customs_attributions WHERE id=?",
                                  (voids[0]['target_id'],)).fetchone()
        self.assertEqual(legacy['basis'], 'legacy-rpa-page-order')
        self.assertEqual(legacy['col_no'], 'X-31')
        # in-transit artifact intercepted, old sealed decision untouched
        self.assertEqual(storage.latest(self.con, 'allocations', allocation)['state'], 'intercept')
        decision = self.con.execute("SELECT outcome FROM decisions WHERE event_id=?",
                                    (sealed['event'],)).fetchone()
        self.assertEqual(decision['outcome'], 'sealed')
        self.assertTrue(self.gate('artifact-1')['intercept'])
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_late_page_voids_bogus_legacy_without_new_evidence(self):
        self.ev('loan', 'authorize', subject='artifact-1', start=1, end=20)
        sealed = self.ev('transit', 'seal', subject='artifact-1', seal='seal-A', items=1)
        self.ev('transit', 'handoff', subject='artifact-1', box=sealed['event'],
                **{'from': 'museum-A', 'to': 'carrier'})
        self.manifest()
        self.capture()
        self.ev('customs', 'legacy_associate', document=self.fx['document'], trip=self.fx['trip'],
                artifact='artifact-1', col_no='X-31')
        # page 2 arrives late and corrects X-31 to local-only (no longer export)
        late = copy.deepcopy(self.fx['columns'][0])
        late.update({'scope': 'local', 'destination': 'B'})
        # re-sign the corrected cell so the page itself is valid
        r = self.capture(page=2, with_tokens=False,
                         columns=[{k: v for k, v in late.items() if k != 'ocr_tokens'}],
                         media='cGFnZS0y')
        self.assertEqual(r['outcome'], 'captured')
        voids = self.con.execute("SELECT * FROM customs_voids WHERE artifact='artifact-1'").fetchall()
        self.assertEqual(len(voids), 1)
        self.assertEqual(voids[0]['reason'], 'column-no-longer-export-eligible')
        self.assertEqual(storage.latest(self.con, 'allocations', sealed['event'])['state'], 'intercept')
        self.assertFalse(self.gate('artifact-1')['allowed'])
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_void_chain_is_append_only_on_repeat(self):
        self.manifest()
        self.capture()
        self.ev('customs', 'legacy_associate', document=self.fx['document'], trip=self.fx['trip'],
                artifact='artifact-1', col_no='X-31')
        rows = [{'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
                {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'}]
        self.attribute('unboxing_recheck', rows, observed_at=6)
        first = self.con.execute('SELECT COUNT(*) c FROM customs_voids').fetchone()['c']
        self.ev('customs', 'reconcile', document=self.fx['document'], trip=self.fx['trip'], trigger='late-page')
        second = self.con.execute('SELECT COUNT(*) c FROM customs_voids').fetchone()['c']
        self.assertEqual(first, second)
        self.assertEqual(first, 1)

    def test_intercept_blocked_from_further_custody(self):
        self.ev('loan', 'authorize', subject='artifact-1', start=1, end=20)
        sealed = self.ev('transit', 'seal', subject='artifact-1', seal='seal-A', items=1)
        self.ev('transit', 'handoff', subject='artifact-1', box=sealed['event'],
                **{'from': 'museum-A', 'to': 'carrier'})
        self.manifest()
        self.capture()
        self.ev('customs', 'legacy_associate', document=self.fx['document'], trip=self.fx['trip'],
                artifact='artifact-1', col_no='X-31')
        self.attribute('unboxing_recheck', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
        ], observed_at=6)
        # a further handoff must fail: allocation is intercepted and item is held
        r = self.ev('transit', 'handoff', subject='artifact-1', box=sealed['event'],
                    **{'from': 'carrier', 'to': 'museum-B'}, trip=self.fx['trip'])
        self.assertEqual(r['outcome'], 'blocked')


class DownstreamConsumption(Base):
    def test_loan_authorize_consumes_per_item_certificate(self):
        self.manifest()
        self.capture()
        self.attribute('unboxing_recheck', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
        ], observed_at=6)
        blocked = self.ev('loan', 'authorize', subject='artifact-1', start=1, end=20, trip=self.fx['trip'])
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertEqual(blocked['reason'], 'customs-item-held')
        ok = self.ev('loan', 'authorize', subject='artifact-2', start=1, end=20, trip=self.fx['trip'])
        self.assertEqual(ok['outcome'], 'authorized')
        clearance = self.con.execute("SELECT state FROM documents WHERE subject='artifact-2' AND kind='customs-clearance'").fetchone()
        self.assertIn('X-31', clearance['state'])

    def test_transit_seal_requires_per_item_confirmation(self):
        self.manifest()
        self.capture()
        self.attribute('unboxing_recheck', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
        ], observed_at=6)
        self.ev('loan', 'authorize', subject='artifact-1', start=1, end=20)
        self.ev('loan', 'authorize', subject='artifact-2', start=1, end=20, trip=self.fx['trip'])
        blocked = self.ev('transit', 'seal', subject='artifact-1', seal='s1', items=1, trip=self.fx['trip'])
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertEqual(blocked['reason'], 'customs-item-held')
        ok = self.ev('transit', 'seal', subject='artifact-2', seal='s2', items=1, trip=self.fx['trip'])
        self.assertEqual(ok['outcome'], 'sealed')

    def test_unrelated_artifact_not_contagious(self):
        self.manifest()
        self.capture()
        self.attribute('unboxing_recheck', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
        ], observed_at=6)
        # artifact-3 is not on this customs sheet; its loan is unaffected
        self.assertEqual(self.ev('loan', 'authorize', subject='artifact-3', start=1, end=20,
                                 trip=self.fx['trip'])['outcome'], 'authorized')

    def test_original_demo_still_replays(self):
        rows = json.loads((Path(__file__).resolve().parent.parent / 'fixtures' / 'demo.json').read_text(encoding='utf-8'))
        results = [cli.run_event(self.con, x['subsystem'], x['event']) for x in rows]
        self.assertEqual(results[0]['outcome'], 'authorized')
        self.assertEqual(results[1]['outcome'], 'sealed')
        self.assertEqual(results[2]['outcome'], 'in-transit')
        self.assertEqual(results[3]['outcome'], 'arrived')
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_replayed_demo_with_customs_sheet_does_not_release_unknown(self):
        rows = json.loads((Path(__file__).resolve().parent.parent / 'fixtures' / 'demo.json').read_text(encoding='utf-8'))
        [cli.run_event(self.con, x['subsystem'], x['event']) for x in rows]
        self.manifest()
        self.capture()
        self.assertEqual(self.status('artifact-1'), 'unresolved')
        # a fresh re-seal of artifact-1 must be blocked by customs, not by its old box
        r = self.ev('transit', 'seal', subject='artifact-1', seal='seal-new', items=1, trip=self.fx['trip'])
        self.assertEqual(r['outcome'], 'blocked')
        self.assertTrue(r['reason'].startswith('customs-item-'))


class OcrAndAudit(Base):
    def test_ocr_rerun_versioned_and_correction_auditable(self):
        self.manifest()
        self.capture()
        toks = [dict(t) for t in self.fx['columns'][0]['ocr_tokens']]
        toks[3]['ch'] = 'L'
        r = self.ev('customs', 'ocr', document=self.fx['document'], page=1, tokens=toks)
        self.assertEqual(r['outcome'], 'ocr-mismatch')
        state = r['data']['columns']['X-31']
        self.assertFalse(state['ok'])
        c = self.ev('customs', 'correct', document=self.fx['document'], page=1,
                    col_no='X-31', idx=3, before='L', after='1', operator='clerk-1')
        self.assertEqual(c['outcome'], 'corrected')
        tokens = self.con.execute("SELECT rev,ch FROM customs_tokens WHERE idx=3 AND col_no='X-31' ORDER BY rev").fetchall()
        self.assertEqual({t['rev'] for t in tokens}, {1, 2})
        self.assertEqual([t['ch'] for t in tokens if t['rev'] == 1], ['1'])
        self.assertEqual([t['ch'] for t in tokens if t['rev'] == 2], ['L'])
        corrections = self.con.execute('SELECT * FROM customs_corrections').fetchall()
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]['operator'], 'clerk-1')
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_swapping_upload_order_keeps_original_per_page(self):
        # capture page 2 first, then page 1; both original digests remain
        self.manifest()
        self.capture(page=2, tokens=False)
        self.capture(page=1)
        pages = self.con.execute('SELECT page,version,digest,media_id FROM customs_pages ORDER BY page').fetchall()
        self.assertEqual([p['page'] for p in pages], [1, 2])
        for p in pages:
            self.assertTrue(p['digest'])
            self.assertTrue(p['media_id'])
        media = self.con.execute('SELECT page,COUNT(*) c FROM customs_media GROUP BY page').fetchall()
        self.assertEqual({m['page'] for m in media}, {1, 2})

    def test_identical_page_replay_is_idempotent(self):
        self.manifest()
        self.assertEqual(self.capture()['outcome'], 'captured')
        r = self.capture()
        self.assertEqual(r['outcome'], 'idempotent')
        self.assertEqual(self.con.execute('SELECT COUNT(*) c FROM customs_pages').fetchone()['c'], 1)

    def test_late_page_adds_evidence_without_overwriting(self):
        self.manifest()
        self.capture(page=1)
        r = self.capture(page=2, tokens=False, media='cGFnZS0y')
        self.assertEqual(r['outcome'], 'captured')
        self.assertEqual(self.con.execute('SELECT COUNT(*) c FROM customs_pages').fetchone()['c'], 2)
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_audit_detects_tampered_customs_decision(self):
        self.manifest()
        self.capture()
        self.attribute('unboxing_recheck', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
        ], observed_at=6)
        self.assertTrue(cli.audit(self.con)['ok'])
        self.con.execute("UPDATE customs_attributions SET status='confirmed' WHERE artifact='artifact-1'")
        self.con.commit()
        self.assertFalse(cli.audit(self.con)['ok'])

    def test_interrupted_retry_does_not_double_claim_column(self):
        self.manifest()
        self.capture()
        rows = [{'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-31'},
                {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-32'}]
        first = self.attribute('sealing_scan', rows, observed_at=2)
        self.assertEqual(first['outcome'], 'attributed')
        # simulate a retry of the same evidence after an interrupted write
        self.n += 1
        retry = cli.run_event(self.con, 'customs', {
            'id': f'e{self.n}', 'subject': 'doc', 'action': 'attribute',
            'data': {'document': self.fx['document'], 'trip': self.fx['trip'], 'box': self.fx['box'],
                     'seal': self.fx['seal'], 'kind': 'sealing_scan', 'observed_at': 2,
                     'assignments': [
                         {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
                         {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
                     ]},
            'effective_at': 99, 'received_at': 99})
        self.assertEqual(retry['outcome'], 'idempotent')
        claims = self.con.execute('SELECT * FROM customs_column_claims ORDER BY col_no').fetchall()
        self.assertEqual(len(claims), 2)
        self.assertEqual({c['artifact'] for c in claims}, {'artifact-1', 'artifact-2'})
        self.assertEqual(self.status('artifact-1'), 'confirmed')
        self.assertEqual(self.status('artifact-2'), 'held')

    def test_unknown_action_blocked(self):
        r = self.ev('customs', 'teleport', document=self.fx['document'])
        self.assertEqual(r['outcome'], 'blocked')


class CertificatePackage(Base):
    def test_package_is_traceable_and_digest_stable(self):
        self.manifest()
        self.capture()
        self.attribute('sealing_scan', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-31'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-32'},
        ], observed_at=2)
        pkg = customs.package(self.con, 'artifact-1', self.fx['trip'], self.fx['document'])
        self.assertEqual(pkg['status'], 'confirmed')
        self.assertEqual(pkg['column'], 'X-31')
        self.assertEqual(pkg['destination'], 'B')
        self.assertEqual(pkg['collection_code'], 'COL-ART-0001')
        self.assertTrue(pkg['evidence'])
        self.assertTrue(pkg['originals']['tokens'])
        self.assertTrue(pkg['originals']['corrections'] == [])
        # recomputation from stored originals yields the same digest
        again = customs.package(self.con, 'artifact-1', self.fx['trip'], self.fx['document'])
        self.assertEqual(pkg['digest'], again['digest'])

    def test_certificates_report_lists_each_artifact_once(self):
        self.manifest()
        self.capture()
        self.attribute('unboxing_recheck', [
            {'artifact': 'artifact-1', 'collection_code': 'COL-ART-0001', 'col_no': 'X-32'},
            {'artifact': 'artifact-2', 'collection_code': 'COL-ART-0002', 'col_no': 'X-31'},
        ], observed_at=6)
        report = customs.certificates(self.con, self.fx['trip'], self.fx['document'])
        self.assertEqual(set(report['certificates']), {'artifact-1', 'artifact-2'})
        self.assertEqual(report['certificates']['artifact-1']['status'], 'held')
        self.assertEqual(report['certificates']['artifact-2']['status'], 'confirmed')
        self.assertEqual(report['resolution']['unresolved'], [])


if __name__ == '__main__':
    unittest.main()
