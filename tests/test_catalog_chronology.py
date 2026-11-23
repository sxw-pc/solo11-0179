import json
import tempfile
import unittest
from pathlib import Path

from exhibitflow import catalog, cli, storage
from exhibitflow.catalog import (
    select_label, era_to_internal, format_internal_year, format_range,
    active_domains, solve_joint, narrowest_sets, build_publish_version,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "catalog-chronology.json"

# Fixed counterexample domains (internal sequence 1 BCE=0 ... 3 CE=3).
DOMAINS = {
    "A": {"low": 0, "high": 2, "feasible": True, "sources": ["card-1"], "candidates": []},
    "B": {"low": 2, "high": 3, "feasible": True, "sources": ["card-2"], "candidates": []},
    "C": {"low": 1, "high": 3, "feasible": True, "sources": ["card-3"], "candidates": []},
}
EDGES = [
    {"edge_id": "edge-ab", "earlier": "A", "later": "B", "delta": 2, "status": "verified"},
    {"edge_id": "edge-ac", "earlier": "A", "later": "C", "delta": 1, "status": "verified"},
]


def readings_for(domains):
    return [
        {"object_id": oid, "low_year": d["low"], "high_year": d["high"],
         "state": "active", "source_id": d["sources"][0], "candidate": "x"}
        for oid, d in domains.items()
    ]


class EraMapping(unittest.TestCase):
    def test_continuous_sequence_and_no_year_zero(self):
        self.assertEqual(era_to_internal("BCE", 1), 0)
        self.assertEqual(era_to_internal("CE", 1), 1)
        self.assertEqual(era_to_internal("CE", 2), 2)
        self.assertEqual(era_to_internal("BCE", 2), -1)
        with self.assertRaises(ValueError):
            era_to_internal("CE", 0)
        with self.assertRaises(ValueError):
            era_to_internal("BCE", 0)

    def test_rendering_never_shows_year_zero(self):
        self.assertEqual(format_internal_year(0, "zh"), "公元前1年")
        self.assertEqual(format_internal_year(0, "en"), "1 BCE")
        self.assertEqual(format_internal_year(1, "zh"), "公元1年")
        self.assertEqual(format_internal_year(1, "en"), "1 CE")
        self.assertEqual(format_internal_year(-1, "en"), "2 BCE")
        for language in ("zh", "en"):
            for value in range(-5, 6):
                self.assertNotIn("0 ", format_internal_year(value, language))
                self.assertNotIn("0年", format_internal_year(value, language))
        self.assertEqual(format_range(0, 1, "en"), "1 BCE–1 CE")
        self.assertEqual(format_range(0, 1, "zh"), "公元前1年–公元1年")


class JointChronology(unittest.TestCase):
    def test_fixed_counterexample_has_exactly_two_joint_solutions(self):
        solutions = solve_joint(DOMAINS, EDGES)
        self.assertEqual(
            sorted(solutions, key=lambda s: (s["A"], s["B"], s["C"])),
            [{"A": 0, "B": 2, "C": 1}, {"A": 1, "B": 3, "C": 2}],
        )
        self.assertEqual(len(solutions), 2)

    def test_reject_locally_stitched_b2_c2_combination(self):
        # B=2, C=2 each lie inside their own ranges, but violate the edges.
        solutions = solve_joint(DOMAINS, EDGES)
        self.assertNotIn({"A": 0, "B": 2, "C": 2}, solutions)
        self.assertNotIn({"A": 1, "B": 2, "C": 2}, solutions)
        for sol in solutions:
            self.assertEqual(sol["B"], sol["A"] + 2)
            self.assertEqual(sol["C"], sol["A"] + 1)

    def test_without_edges_more_local_assignments_exist(self):
        # Demonstrates the edge constraints do the work, not the ranges alone.
        loose = solve_joint(DOMAINS, [])
        self.assertIn({"A": 0, "B": 2, "C": 2}, loose)
        self.assertEqual(len(loose), 2 * 2 * 3)

    def test_narrowest_sets_cover_both_joint_solutions(self):
        narrow = narrowest_sets(solve_joint(DOMAINS, EDGES), ["A", "B", "C"])
        self.assertEqual(narrow, {"A": [0, 1], "B": [2, 3], "C": [1, 2]})

    def test_infeasible_domain_is_surfaced_not_silently_dropped(self):
        bad = dict(DOMAINS)
        bad["A"] = {"low": 5, "high": 6, "feasible": True, "sources": ["x"], "candidates": []}
        self.assertEqual(solve_joint(bad, EDGES), [])
        version = build_publish_version(readings_for(bad), [], EDGES, 1, 1)
        self.assertEqual(version["status"], "conflict")
        self.assertIn("A", version["infeasible_objects"])

    def test_conflicting_edges_publish_conflict_version(self):
        edges = EDGES + [
            {"edge_id": "edge-bad", "earlier": "A", "later": "B", "delta": 5,
             "status": "verified"},
        ]
        self.assertEqual(solve_joint(DOMAINS, edges), [])
        version = build_publish_version(readings_for(DOMAINS), [], edges, 1, 1)
        self.assertEqual(version["status"], "conflict")
        self.assertIn("edge-bad", version["conflict_edges"])
        self.assertEqual(version["solution_count"], 0)


class PurePublishVersion(unittest.TestCase):
    def _editorials(self):
        return [
            {"object_id": "A", "language": "zh", "revision": 1, "title": "铜灯",
             "date_label": "x", "source_id": "card-1", "state": "active", "withdrawn_at": 0},
            {"object_id": "A", "language": "en", "revision": 1, "title": "Bronze Lamp",
             "date_label": "x", "source_id": "card-1", "state": "active", "withdrawn_at": 0},
        ]

    def test_disputed_version_keeps_ranges_and_marks_dispute(self):
        version = build_publish_version(readings_for(DOMAINS), self._editorials(), EDGES, 1, 20)
        self.assertEqual(version["status"], "disputed")
        self.assertEqual(version["solution_count"], 2)
        zh = version["objects"]["A"]["labels"]["zh"]
        en = version["objects"]["A"]["labels"]["en"]
        self.assertTrue(zh["text"].endswith("（争议范围）"))
        self.assertTrue(en["text"].endswith("(disputed range)"))
        self.assertEqual(zh["text"], "公元前1年–公元1年（争议范围）")
        self.assertEqual(en["text"], "1 BCE–1 CE (disputed range)")

    def test_branch_b2_pins_single_resolved_solution(self):
        edges = EDGES + [{"edge_id": "edge-b2", "earlier": "B", "later": "B",
                          "delta": 2, "status": "verified"}]
        version = build_publish_version(readings_for(DOMAINS), self._editorials(), edges, 1, 30)
        self.assertEqual(version["status"], "resolved")
        self.assertEqual(version["solutions"], [{"A": 0, "B": 2, "C": 1}])
        labels = version["objects"]
        self.assertEqual(labels["A"]["labels"]["zh"]["text"], "公元前1年")
        self.assertEqual(labels["A"]["labels"]["en"]["text"], "1 BCE")
        self.assertEqual(labels["B"]["labels"]["zh"]["text"], "公元2年")
        self.assertEqual(labels["B"]["labels"]["en"]["text"], "2 CE")
        self.assertEqual(labels["C"]["labels"]["zh"]["text"], "公元1年")
        self.assertEqual(labels["C"]["labels"]["en"]["text"], "1 CE")

    def test_branch_b3_pins_other_resolved_solution(self):
        edges = EDGES + [{"edge_id": "edge-b3", "earlier": "B", "later": "B",
                          "delta": 3, "status": "verified"}]
        version = build_publish_version(readings_for(DOMAINS), self._editorials(), edges, 1, 30)
        self.assertEqual(version["solutions"], [{"A": 1, "B": 3, "C": 2}])
        labels = version["objects"]
        self.assertEqual(labels["A"]["labels"]["en"]["text"], "1 CE")
        self.assertEqual(labels["B"]["labels"]["en"]["text"], "3 CE")
        self.assertEqual(labels["C"]["labels"]["en"]["text"], "2 CE")

    def test_zh_and_en_cite_same_version_id(self):
        version = build_publish_version(readings_for(DOMAINS), self._editorials(), EDGES, 7, 20)
        zh_id = en_id = version["version_id"]
        self.assertTrue(zh_id.startswith("cat-v7-"))
        self.assertEqual(zh_id, en_id)
        for obj in version["objects"].values():
            self.assertEqual(set(obj["labels"]), {"zh", "en"})

    def test_pending_edges_are_listed_but_do_not_constrain(self):
        edges = EDGES + [{"edge_id": "edge-b2", "earlier": "B", "later": "B",
                          "delta": 2, "status": "pending", "source_id": "rephoto-page-2",
                          "reviewer": None}]
        version = build_publish_version(readings_for(DOMAINS), self._editorials(), edges, 1, 11)
        self.assertEqual(version["solution_count"], 2)
        self.assertEqual([e["edge_id"] for e in version["pending_edges"]], ["edge-b2"])


class BaselineSelectLabel(unittest.TestCase):
    def test_baseline_still_picks_largest_revision(self):
        records = [
            {"object_id": "A", "language": "zh", "revision": 1, "received_at": 1,
             "title": "铜灯", "date_label": "1 BCE–2 CE", "source_id": "card-1"},
            {"object_id": "A", "language": "zh", "revision": 2, "received_at": 9,
             "title": "铜灯", "date_label": "2 CE", "source_id": "ocr-page-7"},
        ]
        picked = select_label(records, "A", "zh")
        self.assertEqual(picked["source_id"], "ocr-page-7")
        self.assertEqual(picked["date_label"], "2 CE")
        # And that raw label is exactly what the joint solver refuses.
        self.assertNotIn({"A": 2}, solve_joint(DOMAINS, EDGES))


class EventSourcedFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = storage.connect(Path(self.tmp.name) / "catalog.db")
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _run(self, rows):
        return [cli.run_event(self.con, r["subsystem"], r["event"]) for r in rows]

    def _base(self):
        return self._run(self.fixture["base_events"])

    def _published_versions(self):
        return [
            dict(r) for r in self.con.execute(
                "SELECT * FROM catalog_versions ORDER BY sequence")
        ]

    def test_ingestion_records_every_required_source_field(self):
        self._base()
        source = storage.latest(self.con, "chrono_sources", "ocr-page-7")
        self.assertEqual(source["kind"], "scanned-page")
        self.assertTrue(source["summary"])
        self.assertEqual(source["page"], "7")
        self.assertEqual(source["era_system"], "BCE-CE")
        self.assertEqual(source["translation_version"], "zh-v1")
        self.assertEqual(source["reviewer"], "intern-sun")
        self.assertEqual(source["revised_at"], 9)
        reading = self.con.execute(
            "SELECT * FROM chrono_readings WHERE source_id='ocr-page-7'").fetchone()
        self.assertEqual(reading["confidence"], "misread")
        editorial = self.con.execute(
            "SELECT * FROM chrono_editorials WHERE object_id='A' AND language='zh' ORDER BY revision DESC"
        ).fetchone()
        self.assertEqual(editorial["date_label"], "2 CE")
        edge = storage.latest(self.con, "chrono_edges", "edge-ab")
        self.assertEqual((edge["earlier"], edge["later"], edge["delta"]), ("A", "B", 2))
        self.assertEqual(edge["status"], "verified")

    def test_disputed_publish_has_two_solutions_and_disputed_labels(self):
        self._base()
        results = self._run(self.fixture["scenarios"]["disputed_publish"])
        self.assertEqual(results[-1]["outcome"], "disputed")
        version = results[-1]["catalog"]
        self.assertEqual(version["solution_count"], 2)
        self.assertEqual(
            sorted(version["solutions"], key=lambda s: s["A"]),
            [{"A": 0, "B": 2, "C": 1}, {"A": 1, "B": 3, "C": 2}],
        )
        zh = version["objects"]["B"]["labels"]["zh"]
        en = version["objects"]["B"]["labels"]["en"]
        self.assertEqual(zh["text"], "公元2年–公元3年（争议范围）")
        self.assertEqual(en["text"], "2 CE–3 CE (disputed range)")
        self.assertNotRegex(en["text"], r"(?<!\d)0 (CE|BCE)")

    def test_branch_b2_and_b3_each_leave_one_solution(self):
        for scenario, expected in (
            ("branch_b2", {"A": 0, "B": 2, "C": 1}),
            ("branch_b3", {"A": 1, "B": 3, "C": 2}),
        ):
            self._base()
            results = self._run(self.fixture["scenarios"][scenario])
            publish = [r for r in results if r["outcome"] == "resolved"][0]
            self.assertEqual(publish["catalog"]["solutions"], [expected])
            labels = publish["catalog"]["objects"]
            for obj in labels.values():
                zh, en = obj["labels"]["zh"], obj["labels"]["en"]
                self.assertNotIn("0年", zh["text"])
                self.assertNotRegex(en["text"], r"(?<!\d)0 (CE|BCE)")
            self.tearDown()
            self.setUp()

    def test_bilingual_labels_share_version_id_in_persistence(self):
        self._base()
        self._run(self.fixture["scenarios"]["branch_b2"])
        row = self._published_versions()[0]
        payload = json.loads(
            self.con.execute(
                "SELECT payload FROM catalog_versions WHERE version_id=?",
                (row["version_id"],)).fetchone()["payload"])
        for obj in payload["objects"].values():
            self.assertEqual(set(obj["labels"]), {"zh", "en"})
        self.assertEqual(row["version_id"], payload["version_id"])

    def test_withdrawn_relation_recomputes_version_and_replays_identically(self):
        self._base()
        results = self._run(self.fixture["scenarios"]["withdraw_relation"])
        edge = storage.latest(self.con, "chrono_edges", "edge-ac")
        self.assertEqual(edge["status"], "withdrawn")
        self.assertGreater(edge["withdrawn_at"], 0)
        version = [r for r in results if "catalog" in r][-1]["catalog"]
        # With edge-ac gone, C is free inside [1,3]; A/B still locked to two pairs.
        self.assertEqual(version["solution_count"], 6)
        self.assertIn("edge-ac", [e["edge_id"] for e in version["withdrawn_edges"]])
        # Provenance of the new version no longer treats edge-ac as active.
        self.assertNotIn("edge-ac", version["objects"]["C"]["dependent_edges"])
        # Deterministic replay from raw events yields the identical report.
        report_before = storage.report(self.con)
        replay = storage.connect(":memory:")
        for row in self.con.execute("SELECT subsystem,payload FROM events ORDER BY rowid"):
            cli.run_event(replay, row["subsystem"], json.loads(row["payload"]))
        self.assertEqual(storage.report(replay), report_before)
        self.assertTrue(cli.audit(self.con)["ok"])
        replay.close()

    def test_conflict_scenario_publishes_no_point_labels(self):
        self._base()
        results = self._run(self.fixture["scenarios"]["conflict"])
        publish = [r for r in results if r["outcome"] == "conflict"][0]
        self.assertEqual(publish["catalog"]["solution_count"], 0)
        for obj in publish["catalog"]["objects"].values():
            self.assertEqual(obj["labels"], {})

    def test_erratum_retains_old_print_and_lists_affected_labels(self):
        self._base()
        scenario = self.fixture["scenarios"]["erratum"]
        v1_result = self._run(scenario[:1])[0]
        old_version_id = v1_result["version_id"]
        middle = [dict(r) for r in scenario[1:-1]]
        self._run(middle)
        erratum_event = json.loads(json.dumps(scenario[-1]))
        erratum_event["event"]["data"]["old_version_id"] = old_version_id
        erratum = self._run([erratum_event])[0]
        self.assertEqual(erratum["outcome"], "corrected")
        # Old version row is retained untouched with its print run and links.
        old_row = self.con.execute(
            "SELECT * FROM catalog_versions WHERE version_id=?", (old_version_id,)).fetchone()
        self.assertEqual(old_row["print_run"], "run-2026-v1")
        erratum_row = self.con.execute("SELECT * FROM catalog_errata").fetchone()
        self.assertEqual(erratum_row["old_version_id"], old_version_id)
        affected = json.loads(erratum_row["affected_labels"])
        affected_pairs = {(a["object_id"], a["language"]) for a in affected}
        self.assertIn(("A", "zh"), affected_pairs)
        self.assertIn(("A", "en"), affected_pairs)
        # The old, contradicted point label is preserved in the correction record.
        old_a = [a for a in affected if a["object_id"] == "A" and a["language"] == "zh"][0]
        self.assertEqual(old_a["old_text"], "公元2年")
        self.assertIn("公元前1年", old_a["new_text"])
        # New version exists and old one is still independently queryable.
        self.assertNotEqual(erratum_row["new_version_id"], old_version_id)
        self.assertEqual(len(self._published_versions()), 2)

    def test_duplicate_publish_is_idempotent_not_a_new_version(self):
        self._base()
        self._run(self.fixture["scenarios"]["disputed_publish"])
        again = self._run([
            {"subsystem": "catalog", "event": {
                "id": "pub-again", "subject": "catalog", "action": "publish",
                "data": {"languages": ["zh", "en"]},
                "effective_at": 25, "received_at": 25}}
        ])[0]
        self.assertEqual(again["outcome"], "unchanged")
        self.assertEqual(len(self._published_versions()), 1)

    def test_event_idempotency_and_conflict(self):
        rows = self.fixture["base_events"]
        first = cli.run_event(self.con, rows[0]["subsystem"], rows[0]["event"])
        self.assertEqual(first["outcome"], "ingested")
        again = cli.run_event(self.con, rows[0]["subsystem"], rows[0]["event"])
        self.assertEqual(again["outcome"], "idempotent")
        mutated = json.loads(json.dumps(rows[0]["event"]))
        mutated["data"]["summary"] = "tampered"
        with self.assertRaises(ValueError):
            cli.run_event(self.con, "catalog", mutated)

    def test_loan_scope_does_not_endorse_chronology(self):
        # Authorize A on the loan subsystem, then publish catalog without it
        # changing any date; loan state appears only as scope metadata.
        cli.run_event(self.con, "loan", {
            "id": "loan-a", "subject": "A", "action": "authorize",
            "data": {"start": 1, "end": 30, "version": 1},
            "effective_at": 1, "received_at": 1})
        self._base()
        results = self._run(self.fixture["scenarios"]["disputed_publish"])
        version = results[-1]["catalog"]
        self.assertEqual(version["loan_scope"], ["A"])
        self.assertEqual(version["solution_count"], 2)
        # No loan authorization at all still publishes the same chronology.
        fresh = storage.connect(":memory:")
        for row in self.fixture["base_events"] + self.fixture["scenarios"]["disputed_publish"]:
            cli.run_event(fresh, row["subsystem"], row["event"])
        no_loan = json.loads(
            fresh.execute("SELECT payload FROM catalog_versions").fetchone()["payload"])
        self.assertEqual(no_loan["loan_scope"], [])
        self.assertEqual(no_loan["solutions"], version["solutions"])
        fresh.close()

    def test_cli_scenario_command_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cli.db"
            exit_code = 0
            try:
                cli.main(["catalog", "--db", str(db), "--fixture", str(FIXTURE),
                          "--scenario", "branch_b2"])
            except SystemExit as exc:
                exit_code = exc.code
            self.assertEqual(exit_code, 0)
            con = storage.connect(db)
            self.assertTrue(cli.audit(con)["ok"])
            versions = con.execute("SELECT status FROM catalog_versions").fetchall()
            self.assertEqual([r["status"] for r in versions], ["resolved"])
            con.close()


if __name__ == "__main__":
    unittest.main()
