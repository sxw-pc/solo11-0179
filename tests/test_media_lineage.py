import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

from exhibitflow import cli, loan, media, storage

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'media-derivatives.json'


class MediaLineage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = storage.connect(Path(self.tmp.name) / 'media.db')
        self.fx = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    # -- helpers ------------------------------------------------------
    def send(self, event):
        return media.ingest(self.con, event)

    def send_all(self, events):
        return [self.send(e) for e in events]

    def seed_loan(self, start=1, end=10):
        event = dict(self.fx['loan_events'][0])
        event['data'] = {**event['data'], 'start': start, 'end': end}
        return cli.run_event(self.con, 'loan', event)

    def seed_core(self):
        """Archive, base region license and asset licenses (no derivatives)."""
        self.send_all(self.fx['base_events'][:4])

    def seed_base(self):
        """Loan period plus the full day1-5 baseline: L and R published on day5."""
        self.seed_loan()
        self.send_all(self.fx['base_events'])

    def base_event(self, event_id):
        return next(e for e in self.fx['base_events'] if e['id'] == event_id)

    # -- fixed renders --------------------------------------------------
    def test_fixed_renders_zero_and_full_out_of_bounds(self):
        self.seed_base()
        self.assertEqual(media.out_of_bounds(self.con, 'der-L', 5)['out_area'], 0)
        self.assertEqual(media.out_of_bounds(self.con, 'der-R', 5)['out_area'], 1000 * 1000)
        report = media.assess_publications(self.con, 5)
        left = report['publications']['pub-L#v1']
        right = report['publications']['pub-R#v1']
        self.assertEqual(left['status'], 'publishable')
        self.assertEqual(left['out_of_bounds_area'], 0)
        self.assertEqual(right['status'], 'must-takedown')
        self.assertEqual(right['out_of_bounds_area'], 1000 * 1000)
        self.assertIn('out-of-bounds', right['reasons'])
        self.assertEqual(report['publishable'], ['pub-L#v1'])
        self.assertEqual(report['must_takedown'], ['pub-R#v1'])
        self.assertEqual(self.fx['expect']['out_of_bounds']['der-R'], 1000000)

    def test_montage_keeps_layer_attribution(self):
        self.seed_base()
        license_record = {'artifact': 'artifact-1', 'max_width': 800, 'start': 1, 'end': 10}
        # The final-width gate alone would wave the 800px montage through.
        self.assertTrue(media.permit_render(
            {'artifact': 'artifact-1', 'width': 800, 'day': 5}, license_record))
        oob = media.out_of_bounds(self.con, 'der-M', 5)
        self.assertEqual(oob['out_area'], 1000 * 1000)
        # A 400px tile still reports its full 1000x1000 source footprint.
        self.assertEqual(oob['layers']['der-M/der-L2']['area'], 1000 * 1000)
        self.assertEqual(oob['layers']['der-M/der-L2']['out_area'], 0)
        self.assertEqual(oob['layers']['der-M/der-R2']['area'], 1000 * 1000)
        self.assertEqual(oob['layers']['der-M/der-R2']['out_area'], 1000 * 1000)
        candidate = media.assess_publications(self.con, 5)['candidates']['der-M']
        self.assertEqual(candidate['status'], 'pending-license')
        self.assertEqual(candidate['out_of_bounds_area'], 1000 * 1000)

    def test_rotated_crop_uses_preimage_intersection(self):
        self.seed_base()
        oob = media.out_of_bounds(self.con, 'der-ROT', 5)
        expected = 10000 * (9 - 4 * math.sqrt(2))
        self.assertAlmostEqual(oob['out_area'], expected, places=6)
        quad = media.coverage(self.con, 'der-ROT')['layers']['der-ROT']['quad']
        # The top-left pre-image corner sits inside the licensed half, so a
        # corner check would pass; the rotated block still crosses x=1000.
        self.assertLess(quad[0][0], 1000)
        self.assertGreater(max(x for x, _ in quad), 1000)
        inside = media.out_of_bounds(self.con, 'der-ROT-IN', 5)
        self.assertAlmostEqual(inside['out_area'], 0.0, places=9)
        candidate = media.assess_publications(self.con, 5)['candidates']['der-ROT']
        self.assertEqual(candidate['status'], 'pending-license')

    def test_watermark_and_caption_have_independent_licenses(self):
        self.seed_base()
        day5 = media.assess_publications(self.con, 5)['publications']['pub-L#v1']
        self.assertEqual(day5['status'], 'publishable')
        day7 = media.assess_publications(self.con, 7)['publications']['pub-L#v1']
        self.assertEqual(day7['status'], 'must-takedown')
        self.assertTrue(any(r.startswith('asset-unlicensed') for r in day7['reasons']))
        self.assertEqual(day7['out_of_bounds_area'], 0)  # pixels stay fully licensed

    # -- corrections ----------------------------------------------------
    def test_correction_supplement_keeps_R_after_day6(self):
        self.seed_base()
        self.send_all(self.fx['supplement_events'])
        # The day6-effective license does not retroactively clear day5.
        day5 = media.assess_publications(self.con, 5)
        self.assertEqual(day5['publications']['pub-R#v1']['status'], 'must-takedown')
        day6 = media.assess_publications(self.con, 6)
        right = day6['publications']['pub-R#v1']
        self.assertEqual(right['status'], 'publishable')
        self.assertEqual(right['violation_days'], [5])
        self.assertIn('lic-right', right['licenses'])
        self.assertEqual(day6['publications']['pub-L#v1']['status'], 'publishable')
        self.assertEqual(day6['publishable'], ['pub-L#v1', 'pub-R#v1'])
        self.assertEqual(day6['license_fees'], 12000)
        self.assertEqual(media.license_fees(self.con, 'artifact-1'), 12000)

    def test_correction_replace_recrops_and_purges_cdn(self):
        self.seed_base()
        self.send_all(self.fx['replace_events'])
        day6 = media.assess_publications(self.con, 6)
        old = day6['publications']['pub-R#v1']
        new = day6['publications']['pub-R#v2']
        self.assertEqual(old['status'], 'must-takedown')  # CDN cache still serves it
        self.assertEqual(new['status'], 'publishable')
        self.assertEqual(new['cdn_sha256'], self.fx['digests']['der-LR'])
        self.assertNotEqual(old['cdn_sha256'], new['cdn_sha256'])
        self.assertEqual(day6['license_fees'], 0)  # no right-half fee on this path
        day8 = media.assess_publications(self.con, 8)
        old = day8['publications']['pub-R#v1']
        self.assertEqual(old['status'], 'pending-license')  # purged, still unlicensed
        self.assertEqual(old['exposed_days'], [5, 6, 7])
        self.assertEqual(old['violation_days'], [5, 6, 7])
        # The replacement never pretends the old URL occupant did not exist.
        history = media.url_history(self.con, '/img/R.jpg')
        self.assertEqual([h['cdn_sha256'] for h in history],
                         [self.fx['digests']['der-R'], self.fx['digests']['der-LR']])
        self.assertEqual(history[0]['superseded_day'], 6)
        self.assertEqual(media.assess_publications(self.con, 6)['publications']
                         ['pub-L#v1']['status'], 'publishable')

    # -- failure and ordering ------------------------------------------
    def test_db_write_failure_healed_by_recompute(self):
        self.seed_base()
        # Original bytes archived, but the database write was lost.
        self.con.execute('DELETE FROM media_originals')
        broken = media.assess_publications(self.con, 5)
        self.assertEqual(broken['publications']['pub-L#v1']['status'], 'must-takedown')
        self.assertIn('source-unverified', broken['publications']['pub-L#v1']['reasons'])
        media.recompute(self.con)
        healed = media.assess_publications(self.con, 5)
        self.assertEqual(healed['publications']['pub-L#v1']['status'], 'publishable')
        self.assertEqual(healed['publications']['pub-R#v1']['status'], 'must-takedown')

    def test_crop_revision_keeps_both_cms_versions(self):
        self.seed_loan()
        self.seed_core()
        sha_orig = self.fx['digests']['orig-1']
        sha_v1 = hashlib.sha256(b'render-X-v1-right-half').hexdigest()
        sha_v2 = hashlib.sha256(b'render-X-v2-left-half').hexdigest()

        def register(version, source_rect, sha, day, received, tag=None):
            return self.send({'id': tag or f'x-reg-v{version}', 'subject': 'artifact-1',
                              'action': 'register_derivative',
                              'data': {'derivative': 'der-X', 'cms_version': version,
                                       'artifact': 'artifact-1', 'original': 'orig-1',
                                       'original_sha256': sha_orig,
                                       'ops': [{'op': 'scale', 'from': source_rect,
                                                 'size': [800, 400]}],
                                       'width': 800, 'height': 400, 'sha256': sha},
                              'effective_at': day, 'received_at': received})

        def publish(version, sha, day, received):
            return self.send({'id': f'x-pub-v{version}', 'subject': 'artifact-1',
                              'action': 'publish',
                              'data': {'publication': 'pub-X', 'derivative': 'der-X',
                                       'cms_version': version, 'url': '/img/X.jpg',
                                       'cdn_sha256': sha},
                              'effective_at': day, 'received_at': received})

        register(1, [1000, 0, 1000, 1000], sha_v1, 4, 4)
        publish(1, sha_v1, 5, 5)
        day5 = media.assess_publications(self.con, 5)
        self.assertEqual(day5['publications']['pub-X#v1']['status'], 'must-takedown')
        # Crop parameters are revised under a new CMS version, never in place.
        register(2, [0, 0, 1000, 1000], sha_v2, 6, 6)
        conflict = register(1, [0, 0, 1000, 1000], sha_v2, 6, 7, tag='x-reg-v1-rewrite')
        self.assertEqual(conflict['outcome'], 'blocked')
        self.send({'id': 'x-down', 'subject': 'artifact-1', 'action': 'takedown',
                   'data': {'publication': 'pub-X', 'cdn_purge_day': 6},
                   'effective_at': 6, 'received_at': 8})
        publish(2, sha_v2, 6, 9)
        day6 = media.assess_publications(self.con, 6)
        self.assertEqual(day6['publications']['pub-X#v2']['status'], 'publishable')
        old = day6['publications']['pub-X#v1']
        self.assertEqual(old['status'], 'pending-license')
        self.assertEqual(old['violation_days'], [5])
        # The retired CMS version keeps its own source attribution.
        self.assertEqual(media.out_of_bounds(self.con, 'der-X', 5, cms_version=1)['out_area'],
                         1000 * 1000)
        self.assertEqual(media.out_of_bounds(self.con, 'der-X', 6, cms_version=2)['out_area'], 0)

    def test_revocation_arriving_out_of_order(self):
        self.seed_loan()
        self.seed_core()
        self.send(self.base_event('m07'))  # der-L2, no asset layers
        for event_id, url, day, received in (('p-a', '/img/a.jpg', 5, 5),
                                             ('p-b', '/img/b.jpg', 8, 8)):
            self.send({'id': event_id, 'subject': 'artifact-1', 'action': 'publish',
                       'data': {'publication': f'pub-{event_id}', 'derivative': 'der-L2',
                                'cms_version': 1, 'url': url,
                                'cdn_sha256': self.fx['digests']['der-L2']},
                       'effective_at': day, 'received_at': received})
        # Revocation page effective day7 arrives only after the day8 publish.
        self.send({'id': 'rev-1', 'subject': 'artifact-1', 'action': 'revoke_license',
                   'data': {'license': 'lic-left', 'page': 'revocation-page-7'},
                   'effective_at': 7, 'received_at': 30})
        day6 = media.assess_publications(self.con, 6)
        self.assertEqual(day6['publications']['pub-p-a#v1']['status'], 'publishable')
        day8 = media.assess_publications(self.con, 8)
        self.assertEqual(day8['publications']['pub-p-a#v1']['status'], 'must-takedown')
        self.assertEqual(day8['publications']['pub-p-a#v1']['violation_days'], [7, 8])
        self.assertEqual(day8['publications']['pub-p-b#v1']['status'], 'must-takedown')
        self.assertEqual(day8['publications']['pub-p-b#v1']['violation_days'], [8])
        # Recomputation places the late page at its effective day: same state.
        live_tables = media.snapshot(self.con)
        live_report = media.assess_publications(self.con, 8)
        media.recompute(self.con)
        self.assertEqual(media.snapshot(self.con), live_tables)
        self.assertEqual(media.assess_publications(self.con, 8), live_report)

    def test_loan_period_jointly_constrains(self):
        self.seed_loan(1, 6)  # loan period ends before the media license
        self.send_all(self.fx['base_events'])
        day5 = media.assess_publications(self.con, 5)
        self.assertEqual(day5['publications']['pub-L#v1']['status'], 'publishable')
        self.assertFalse(loan.covers(self.con, 'artifact-1', 8))
        day8 = media.assess_publications(self.con, 8)
        left = day8['publications']['pub-L#v1']
        self.assertEqual(left['status'], 'must-takedown')
        self.assertIn('loan-period-miss', left['reasons'])

    # -- replay ---------------------------------------------------------
    def test_replay_reproduces_both_corrections(self):
        scenarios = {'supplement_events': (['pub-L#v1', 'pub-R#v1'], 12000),
                     'replace_events': (['pub-L#v1', 'pub-R#v2'], 0)}
        for key, (day6_publishable, fees) in scenarios.items():
            with self.subTest(scenario=key):
                with tempfile.TemporaryDirectory() as tmp:
                    con = storage.connect(Path(tmp) / 'replay.db')
                    cli.run_event(con, 'loan', self.fx['loan_events'][0])
                    for event in self.fx['base_events'] + self.fx[key]:
                        media.ingest(con, event)
                    day5 = media.assess_publications(con, 5)
                    day6 = media.assess_publications(con, 6)
                    tables = media.snapshot(con)
                    self.assertEqual(day5['publishable'], ['pub-L#v1'])
                    self.assertEqual(day5['must_takedown'], ['pub-R#v1'])
                    self.assertEqual(day6['publishable'], day6_publishable)
                    self.assertEqual(day6['license_fees'], fees)
                    media.recompute(con)
                    self.assertEqual(media.snapshot(con), tables)
                    self.assertEqual(media.assess_publications(con, 5), day5)
                    self.assertEqual(media.assess_publications(con, 6), day6)
                    con.close()

    def test_event_idempotency_and_conflict(self):
        self.seed_loan()
        self.seed_core()
        event = self.base_event('m05')
        self.assertEqual(self.send(event)['outcome'], 'registered')
        self.assertEqual(self.send(event)['outcome'], 'idempotent')
        tampered = dict(event, data={**event['data'], 'width': 801})
        with self.assertRaises(ValueError):
            self.send(tampered)


if __name__ == '__main__':
    unittest.main()
