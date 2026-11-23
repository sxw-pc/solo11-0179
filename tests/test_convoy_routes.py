import json, tempfile, unittest
from pathlib import Path
from exhibitflow import cli, storage, route_capture as rc

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'convoy-routes.json'


def capture_events():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class ConvoyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = storage.connect(Path(self.tmp.name) / 'convoy.db')
        self.n = 0

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def go(self, sub, action, subject='convoy', **data):
        self.n += 1
        return cli.run_event(self.con, sub, {
            'id': f'e{self.n}', 'subject': subject, 'action': action,
            'data': data, 'effective_at': self.n + 10, 'received_at': self.n + 10})

    def load(self, rows=None):
        for row in rows if rows is not None else capture_events():
            cli.run_event(self.con, row['subsystem'], row['event'])
        rc.rebuild(self.con)

    def dispatch(self, protocol=rc.TRANSFERABLE):
        return self.go('transit', 'dispatch', protocol=protocol)

    def plan(self):
        return {r['vehicle']: r['route'] for r in
                self.con.execute("SELECT vehicle,route FROM convoy_plans WHERE outcome='routable' ORDER BY vehicle")}

    def orders(self):
        return {r['subject']: r['route'] for r in
                self.con.execute("SELECT subject,route FROM handoff_orders WHERE superseded_by='' AND state='issued'")}


class ArchiveAndIdentity(ConvoyBase):
    def test_rebuild_is_identical_after_interrupted_write(self):
        """A crash mid-rebuild leaves no partial tables: rerun matches one-shot."""
        self.load()
        expected = (rc.closure_windows(self.con),
                    [tuple(r) for r in self.con.execute('SELECT id,vehicle,route,status FROM trips ORDER BY id')],
                    [tuple(r) for r in self.con.execute('SELECT trip,vehicle,route,outcome FROM convoy_plans')])
        # poison a derived table, then rebuild from the archive rather than the tables
        self.con.execute("DELETE FROM trips")
        self.con.execute("UPDATE roads SET capacity=99")
        self.con.commit()
        rc.rebuild(self.con)
        actual = (rc.closure_windows(self.con),
                  [tuple(r) for r in self.con.execute('SELECT id,vehicle,route,status FROM trips ORDER BY id')],
                  [tuple(r) for r in self.con.execute('SELECT trip,vehicle,route,outcome FROM convoy_plans')])
        self.assertEqual(expected[0], actual[0])
        self.assertEqual(expected[1], actual[1])

    def test_capture_idempotent_and_conflicting_capture_rejected(self):
        event = capture_events()[0]['event']
        self.assertEqual(cli.run_event(self.con, 'transit', event)['outcome'], 'captured')
        self.assertEqual(cli.run_event(self.con, 'transit', event)['outcome'], 'idempotent')
        event['data']['content'][0]['capacity'] = 9
        self.assertRaises(ValueError, cli.run_event, self.con, 'transit', event)

    def test_same_notice_different_issuer_not_overwritten(self):
        self.load([e for e in capture_events() if e['event']['id'] in ('rc-ann-1', 'rc-ann-2')])
        rows = [dict(r) for r in self.con.execute('SELECT issuer,road_id,notice_no,status FROM announcements ORDER BY issuer')]
        self.assertEqual({r['issuer'] for r in rows}, {'road-authority', 'bridge-authority'})
        self.assertTrue(all(r['road_id'] == 'P' for r in rows))

    def test_retraction_is_issuer_scoped(self):
        rows = capture_events()
        rows.append({'subsystem': 'transit', 'event': {
            'id': 'rc-retract-1', 'subject': 'convoy', 'action': 'capture',
            'data': {'kind': 'retraction', 'content': [
                {'issuer': 'road-authority', 'notice_no': 'RA-100', 'road_id': 'P',
                 'valid_from': '2026-06-01T08:00:00', 'valid_to': '2026-06-01T09:00:00',
                 'published_at': '2026-05-31T12:00:00'}]},
            'effective_at': 2, 'received_at': 2}})
        self.load(rows)
        status = {r['issuer']: r['status'] for r in self.con.execute('SELECT issuer,status FROM announcements')}
        self.assertEqual(status['road-authority'], 'retracted')
        self.assertEqual(status['bridge-authority'], 'open')
        self.assertNotIn('P', rc.closure_windows(self.con))

    def test_recurrence_id_replaces_only_one_occurrence(self):
        rows = [{'subsystem': 'transit', 'event': {
            'id': 'ics-series', 'subject': 'convoy', 'action': 'capture',
            'data': {'kind': 'ics', 'content': (
                'BEGIN:VCALENDAR\n'
                'BEGIN:VEVENT\nUID:cycle-1\nDTSTART;TZID=America/New_York:20260601T090000\n'
                'DTEND;TZID=America/New_York:20260601T100000\nRRULE:FREQ=DAILY;COUNT=3\n'
                'VEHICLE:V1\nROUTE:P\nARTIFACT:artifact-1\nEND:VEVENT\n'
                'BEGIN:VEVENT\nUID:cycle-1\nRECURRENCE-ID;TZID=America/New_York:20260602T090000\n'
                'DTSTART;TZID=America/New_York:20260602T110000\nDTEND;TZID=America/New_York:20260602T120000\n'
                'VEHICLE:V1\nROUTE:Q\nARTIFACT:artifact-1\nEND:VEVENT\n'
                'BEGIN:VEVENT\nUID:cycle-1\nRECURRENCE-ID;TZID=America/New_York:20260603T090000\n'
                'DTSTART;TZID=America/New_York:20260603T090000\nDTEND;TZID=America/New_York:20260603T100000\n'
                'VEHICLE:V1\nROUTE:P\nARTIFACT:artifact-1\nSTATUS:CANCELLED\nEND:VEVENT\n'
                'END:VCALENDAR\n')},
            'effective_at': 1, 'received_at': 1}}]
        self.load(rows)
        trips = {r['recurrence_id']: (r['route'], r['status'])
                 for r in self.con.execute('SELECT recurrence_id,route,status FROM trips')}
        self.assertEqual(len(trips), 3)
        self.assertEqual(trips['20260601T090000'], ('P', 'scheduled'))
        self.assertEqual(trips['20260602T090000'], ('Q', 'scheduled'))
        self.assertEqual(trips['20260603T090000'], ('P', 'cancelled'))

    def test_dst_fall_back_keeps_distinct_trip_identity(self):
        """01:30 occurs twice on 2026-11-01; fold keeps both identities stable."""
        rows = [{'subsystem': 'transit', 'event': {
            'id': 'ics-dst', 'subject': 'convoy', 'action': 'capture',
            'data': {'kind': 'ics', 'content': (
                'BEGIN:VCALENDAR\n'
                'BEGIN:VEVENT\nUID:dst-1\nDTSTART;TZID=America/New_York;FOLD=0:20261101T013000\n'
                'DTEND;TZID=America/New_York;FOLD=0:20261101T023000\nVEHICLE:V1\nROUTE:P\nARTIFACT:artifact-1\nEND:VEVENT\n'
                'BEGIN:VEVENT\nUID:dst-2\nDTSTART;TZID=America/New_York;FOLD=1:20261101T013000\n'
                'DTEND;TZID=America/New_York;FOLD=1:20261101T023000\nVEHICLE:V2\nROUTE:P\nARTIFACT:artifact-2\nEND:VEVENT\n'
                'END:VCALENDAR\n')},
            'effective_at': 1, 'received_at': 1}}]
        self.load(rows)
        before = sorted((r['id'], r['depart_at']) for r in self.con.execute('SELECT id,depart_at FROM trips'))
        self.assertEqual(len(before), 2)
        self.assertEqual(len({t[0] for t in before}), 2)        # distinct trip identity
        self.assertEqual(len({t[1] for t in before}), 2)        # distinct UTC instant
        rc.rebuild(self.con)
        after = sorted((r['id'], r['depart_at']) for r in self.con.execute('SELECT id,depart_at FROM trips'))
        self.assertEqual(before, after)                         # rerun keeps identity


    def test_seal_reused_on_another_vehicle_keeps_history(self):
        rows = capture_events()
        rows.append({'subsystem': 'transit', 'event': {
            'id': 'seal-use-1', 'subject': 'convoy', 'action': 'capture',
            'data': {'kind': 'seal-usage', 'content': [
                {'seal': 'seal-A', 'vehicle': 'V1', 'artifact': 'artifact-1', 'effective_at': 10},
                {'seal': 'seal-A', 'vehicle': 'V2', 'artifact': 'artifact-1', 'effective_at': 20}]},
            'effective_at': 1, 'received_at': 1}})
        self.load(rows)
        binds = [dict(r) for r in self.con.execute('SELECT seal,vehicle,effective_at FROM seal_history ORDER BY effective_at')]
        self.assertEqual([(b['vehicle'], b['effective_at']) for b in binds], [('V1', 10), ('V2', 20)])

    def test_late_csv_cancellation_still_adopted(self):
        self.load()
        trips = {r['vehicle']: r['id'] for r in self.con.execute('SELECT id,vehicle FROM trips')}
        late = {'subsystem': 'transit', 'event': {
            'id': 'csv-late', 'subject': 'convoy', 'action': 'capture',
            'data': {'kind': 'csv', 'content': 'trip,from_route,to_route,cancelled,received_at\n'
                                               f"{trips['V2']},P,P,1,9000\n"},
            'effective_at': 50, 'received_at': 9000}}
        cli.run_event(self.con, 'transit', late['event'])
        rc.rebuild(self.con)
        options = rc.route_options(self.con)
        self.assertEqual(options[trips['V2']]['status'], 'cancelled')
        # the original appointment instant is not retroactively rewritten
        row = self.con.execute('SELECT depart_at FROM trips WHERE id=?', (trips['V2'],)).fetchone()
        self.assertEqual(row['depart_at'], 1780318800)



class GlobalRouteSelection(ConvoyBase):
    def test_greedy_strands_v2_but_global_swap_arrives_both(self):
        self.load()
        options = rc.route_options(self.con)
        edges = rc.conflict_graph(options)
        trips = {t['vehicle']: t['trip'] for t in options.values()}
        # the new fixture is constructed so the declared/iterated order puts V1 first
        self.assertEqual(trips['V1'], min(trips.values(), key=lambda k: (options[k]['depart_at'], k)))
        local = rc.greedy(options, edges)
        self.assertEqual(local['assignment'].get(trips['V1']), 'P')
        # V2 has only P, and P is now taken: the greedy order strands it
        self.assertIsNone(local['assignment'].get(trips['V2']))
        plan = rc.solve(options, edges)
        self.assertTrue(plan['ok'], plan)
        routes = {p['vehicle']: p['route'] for p in plan['plans']}
        self.assertEqual(routes, {'V1': 'Q', 'V2': 'P'})
        self.assertEqual([p['sequence'] for p in plan['plans']], [1, 2])

    def test_dispatch_after_authorize_seal_handoff_inspect(self):
        # reuse the demo's artifact-2 authorization and seal, then stop before arrival
        demo = json.loads((FIXTURE.parent / 'demo.json').read_text(encoding='utf-8'))
        for row in demo:
            ev = row['event']
            if ev['subject'] == 'artifact-2' and ev['id'] in ('l3', 't4', 't5'):
                cli.run_event(self.con, row['subsystem'], ev)
        self.load()
        result = self.dispatch()
        self.assertEqual(result['outcome'], 'dispatched')
        self.assertEqual(self.plan(), {'V1': 'Q', 'V2': 'P'})
        trips = {r['vehicle']: r['id'] for r in self.con.execute('SELECT id,vehicle FROM trips')}

        # receiving handoff for artifact-2 binds to V2's issued order (P), not Q
        late = self.go('transit', 'handoff', subject='artifact-2', box='t4',
                       trip=trips['V2'], route='Q', **{'from': 'carrier', 'to': 'museum-B'})
        self.assertEqual((late['outcome'], late['reason']), ('blocked', 'route-order-mismatch'))
        receipt = self.go('transit', 'handoff', subject='artifact-2', box='t4',
                          trip=trips['V2'], route='P', **{'from': 'carrier', 'to': 'museum-B'})
        self.assertEqual(receipt['outcome'], 'arrived')
        inspected = self.go('condition', 'inspect', subject='artifact-2', box='t4', trip=trips['V2'], damage=True)
        self.assertEqual(inspected['outcome'], 'hold')

    def test_dispatch_is_replayable_and_audits_clean(self):
        self.load()
        self.dispatch(rc.TRANSFERABLE)
        reported = storage.report(self.con)
        self.assertTrue(cli.audit(self.con)['ok'])
        # rerun replay from the archive equals the live dispatch table
        replay = storage.connect(':memory:')
        for row in self.con.execute('SELECT subsystem,payload FROM events ORDER BY rowid'):
            cli.run_event(replay, row['subsystem'], json.loads(row['payload']))
        self.assertEqual(storage.report(replay)['convoy_plans'], reported['convoy_plans'])
        replay.close()


class EndorsementProtocols(ConvoyBase):
    def setUp(self):
        super().setUp()
        self.load()
        self.trips = {r['vehicle']: r['id'] for r in self.con.execute('SELECT id,vehicle FROM trips')}

    def sign(self, vehicle, route, protocol):
        return self.go('loan', 'sign_route', subject=self.trips[vehicle],
                       trip=self.trips[vehicle], route=route, protocol=protocol)

    def test_transferable_endorsement_allows_double_signed_reroute(self):
        # first instruction is signed P and issued; both sides may later re-sign
        self.assertEqual(self.sign('V1', 'P', rc.LOCKED)['outcome'], 'signed')
        self.assertEqual(self.dispatch(rc.LOCKED)['outcome'], 'infeasible')
        self.assertEqual(self.orders(), {'artifact-1': 'P'})
        self.assertEqual(self.sign('V1', 'P', rc.TRANSFERABLE)['outcome'], 'signed')
        self.assertEqual(self.dispatch(rc.TRANSFERABLE)['outcome'], 'dispatched')
        self.assertEqual(self.orders(), {'artifact-1': 'Q', 'artifact-2': 'P'})
        # the superseded P instruction survives as history with a compensation event
        history = [dict(r) for r in self.con.execute('SELECT route,state,superseded_by FROM handoff_orders WHERE subject=?', ('artifact-1',))]
        self.assertTrue(any(h['route'] == 'P' and h['superseded_by'] for h in history))
        comp = self.con.execute('SELECT COUNT(*) n FROM compensation_events').fetchone()['n']
        self.assertGreaterEqual(comp, 1)

    def test_locked_endorsement_keeps_signed_p_and_reschedules_v2(self):
        self.assertEqual(self.sign('V1', 'P', rc.LOCKED)['outcome'], 'signed')
        result = self.dispatch(rc.LOCKED)
        self.assertEqual(result['outcome'], 'infeasible')
        plans = {r['vehicle']: (r['route'], r['outcome']) for r in
                 self.con.execute('SELECT vehicle,route,outcome FROM convoy_plans')}
        self.assertEqual(plans['V1'], ('P', 'routable'))
        self.assertEqual(plans['V2'], ('', 'no-route'))
        self.assertEqual(self.orders(), {'artifact-1': 'P'})
        blocked = [json.loads(r['detail']) for r in
                   self.con.execute("SELECT detail FROM conflict_edges WHERE graph='blocked'")]
        self.assertTrue(blocked)
        self.assertTrue(all(e['route'] == 'P' for e in blocked))

    def test_signed_route_must_be_individually_feasible(self):
        self.assertEqual(self.sign('V2', 'Q', rc.LOCKED)['outcome'], 'blocked')

    def test_closing_q_degrades_to_no_double_window_solution(self):
        authority = {'subsystem': 'transit', 'event': {
            'id': 'close-q', 'subject': 'convoy', 'action': 'capture',
            'data': {'kind': 'announcement', 'content': [
                {'issuer': 'bridge-authority', 'road_id': 'Q', 'notice_no': 'BA-9', 'status': 'closure',
                 'valid_from': '2026-06-01T09:00:00', 'valid_to': '2026-06-01T11:00:00'}]},
            'effective_at': 60, 'received_at': 60}}
        cli.run_event(self.con, 'transit', authority['event'])
        rc.rebuild(self.con)
        result = self.dispatch(rc.TRANSFERABLE)
        self.assertEqual(result['outcome'], 'infeasible')
        current = [dict(r) for r in self.con.execute("SELECT * FROM conflict_edges WHERE graph='current'")]
        self.assertTrue(current)
        # never silently double-book P
        self.assertNotEqual(self.orders(), {'artifact-1': 'P', 'artifact-2': 'P'})


class RerouteOrdering(ConvoyBase):
    def test_signed_reroute_cancellation_out_of_order(self):
        """Same trip: a later-received signed reroute beats an earlier cancellation-free row."""
        self.load()
        trip = self.con.execute("SELECT id FROM trips WHERE vehicle='V1'").fetchone()['id']
        early = {'id': 'csv-early', 'subject': 'convoy', 'action': 'capture',
                 'data': {'kind': 'csv', 'content': f'trip,from_route,to_route,cancelled,received_at\n{trip},P,Q,0,100\n'},
                 'effective_at': 20, 'received_at': 100}
        late = {'id': 'csv-late-cancel', 'subject': 'convoy', 'action': 'capture',
                'data': {'kind': 'csv', 'content': f'trip,from_route,to_route,cancelled,received_at\n{trip},Q,Q,1,900\n'},
                'effective_at': 25, 'received_at': 900}
        for e in (early, late):
            cli.run_event(self.con, 'transit', e)
        rc.rebuild(self.con)
        row = self.con.execute('SELECT cancelled,to_route FROM reroutes WHERE trip=?', (trip,)).fetchone()
        self.assertEqual((row['cancelled'], row['to_route']), (1, 'Q'))

    def test_receiving_handoff_needs_issued_order_and_keeps_custody_chain(self):
        self.go('loan', 'authorize', subject='artifact-1', start=1, end=100)
        box = self.go('transit', 'seal', subject='artifact-1', seal='seal-A', items=1)
        self.load()
        trips = {r['vehicle']: r['id'] for r in self.con.execute('SELECT id,vehicle FROM trips')}
        first = self.go('transit', 'handoff', subject='artifact-1', box=box['event'], trip=trips['V1'],
                        **{'from': 'museum-A', 'to': 'carrier'})
        self.assertEqual((first['outcome'], first['reason']), ('blocked', 'no-current-dispatch-order'))
        self.dispatch()
        ok = self.go('transit', 'handoff', subject='artifact-1', box=box['event'], trip=trips['V1'], route='Q',
                     **{'from': 'museum-A', 'to': 'carrier'})
        self.assertEqual(ok['outcome'], 'in-transit')
        gap = self.go('transit', 'handoff', subject='artifact-1', box=box['event'], trip=trips['V1'],
                      **{'from': 'museum-A', 'to': 'museum-B'})
        self.assertEqual((gap['outcome'], gap['reason']), ('blocked', 'previous-custodian-mismatch'))


class RetryAndSourceChain(ConvoyBase):
    def test_interrupted_rebuild_rerun_matches_source_chain(self):
        """Archive survives a half-written rebuild; retry yields identical evidence."""
        self.load()
        self.dispatch(rc.TRANSFERABLE)
        first = storage.report(self.con)
        self.con.execute('DELETE FROM trips')
        self.con.execute('DELETE FROM conflict_edges')
        self.con.commit()
        retry = self.go('transit', 'dispatch', protocol=rc.TRANSFERABLE)
        second = storage.report(self.con)
        self.assertEqual(retry['outcome'], 'dispatched')
        self.assertEqual(first['trips'], second['trips'])
        self.assertEqual(first['conflict_edges'], second['conflict_edges'])
        # source chain: each derived trip points back at the archived capture
        for trip in second['trips']:
            src = self.con.execute('SELECT * FROM sources WHERE id=?', (trip['source'],)).fetchone()
            self.assertIsNotNone(src)
            self.assertEqual(src['kind'], 'ics')
        self.assertTrue(cli.audit(self.con)['ok'])

    def test_cannot_copy_permit_to_double_book_p(self):
        """Two permits with identical terms are the same physical permit, not a second one."""
        self.load()
        extra = {'id': 'permit-copy', 'subject': 'convoy', 'action': 'capture',
                 'data': {'kind': 'permit', 'content': [
                     {'id': 'permit-P-copy', 'road_id': 'P', 'window_from': '2026-06-01T00:00:00',
                      'window_to': '2026-06-02T00:00:00', 'issuer': 'public-works', 'status': 'granted'}]},
                 'effective_at': 3, 'received_at': 3}
        cli.run_event(self.con, 'transit', extra)
        rc.rebuild(self.con)
        self.dispatch(rc.TRANSFERABLE)
        options = rc.route_options(self.con)
        edges = rc.conflict_graph(options)
        permits = {o['permit'] for info in options.values() for o in info['options'] if o['route'] == 'P'}
        self.assertEqual(len(permits), 1)  # one identity despite two captures
        self.assertTrue(any(e['kind'] == 'permit-shared' for e in edges))
        self.assertNotEqual(self.orders(), {'artifact-1': 'P', 'artifact-2': 'P'})
