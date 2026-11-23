"""Museum catalog labels, source ingestion and joint chronology reconciliation.

The baseline :func:`select_label` only picks the newest editorial record for one
object/language.  The chronology layer below adds an ingestion pipeline for
scanned pages, catalog cards, bilingual editorial records and relative
chronology edges, then solves every object's date *jointly* so locally
plausible labels cannot be stitched into a contradictory label set.

Years are mapped to a continuous internal sequence where ``1 BCE = 0``,
``1 CE = 1`` and so on.  There is no internal year that renders as a
"year 0": internal ``0`` is always rendered as ``1 BCE``.
"""
import json
import hashlib

NAME = '编年目录'

# --- Baseline (unchanged) -------------------------------------------------

def select_label(records, object_id, language):
    """Return the latest matching editorial label, without historical reconciliation."""
    matching = [
        row for row in records
        if row["object_id"] == object_id and row["language"] == language
    ]
    if not matching:
        return None
    selected = max(matching, key=lambda row: (row["revision"], row["received_at"]))
    return {
        "object_id": object_id,
        "language": language,
        "title": selected["title"],
        "date_label": selected["date_label"],
        "source_id": selected["source_id"],
    }

# --- Era / internal year sequence -----------------------------------------

ERA_CE = "CE"
ERA_BCE = "BCE"


def era_to_internal(era, year):
    """Map an explicit BCE/CE year onto the continuous internal sequence.

    ``1 BCE`` maps to ``0`` and ``1 CE`` maps to ``1``; the astronomical
    year zero is never accepted as an input year.
    """
    year = int(year)
    if year < 1:
        raise ValueError("no-year-zero")
    if era.upper() == ERA_CE:
        return year
    if era.upper() == ERA_BCE:
        return 1 - year
    raise ValueError("unknown-era-system")


def format_internal_year(value, language):
    """Render an internal year. Internal 0 is always ``1 BCE``, never year 0."""
    value = int(value)
    if value <= 0:
        bce_year = 1 - value
        return f"公元前{bce_year}年" if language == "zh" else f"{bce_year} BCE"
    return f"公元{value}年" if language == "zh" else f"{value} CE"


def format_range(low, high, language):
    """Render an internal-year range with explicit BCE/CE markers."""
    if low == high:
        return format_internal_year(low, language)
    return f"{format_internal_year(low, language)}–{format_internal_year(high, language)}"


def disputed_suffix(language):
    return "（争议范围）" if language == "zh" else " (disputed range)"


# --- Snapshot construction from stored evidence ---------------------------

def active_domains(readings):
    """Intersect per-object ranges by union? No: take the narrowest common span.

    Each surviving reading contributes an absolute candidate range.  An object's
    admissible domain is the intersection of its active reading ranges (a reading
    outside every other reading is an unresolved conflict, surfaced separately).
    """
    per_object = {}
    for row in readings:
        if row["state"] != "active":
            continue
        per_object.setdefault(row["object_id"], []).append(
            (int(row["low_year"]), int(row["high_year"]), row["source_id"], row["candidate"])
        )
    domains = {}
    for object_id, spans in per_object.items():
        low = max(span[0] for span in spans)
        high = min(span[1] for span in spans)
        domains[object_id] = {
            "low": low,
            "high": high,
            "feasible": low <= high,
            "sources": sorted({span[2] for span in spans}),
            "candidates": [{"source_id": span[2], "candidate": span[3]} for span in spans],
        }
    return domains


# --- Joint constraint solver ----------------------------------------------

def solve_joint(domains, edges):
    """Enumerate every joint assignment satisfying domains and relative edges.

    ``edges`` are dicts with ``earlier``, ``later`` and ``delta`` meaning
    ``later = earlier + delta``.  Only ``verified`` edges constrain the
    solution; ``pending`` edges are listed for review and withdrawn edges are
    retained for provenance only.
    """
    active = [e for e in edges if e["status"] == "verified"]
    objects = sorted(domains)
    options = {
        oid: list(range(domains[oid]["low"], domains[oid]["high"] + 1))
        for oid in objects
        if domains[oid]["feasible"]
    }
    solutions = []
    assignment = {}

    def consistent(oid, year):
        for edge in active:
            a, b, d = edge["earlier"], edge["later"], int(edge["delta"])
            if a == b:
                # Self-edge pins the object to an absolute internal year.
                if oid == a and year != d:
                    return False
                continue
            if oid == a and b in assignment and assignment[b] != year + d:
                return False
            if oid == b and a in assignment and assignment[a] != year - d:
                return False
        return True

    def backtrack(index):
        if index == len(objects):
            solutions.append(dict(assignment))
            return
        oid = objects[index]
        if oid not in options:
            return
        for year in options[oid]:
            if consistent(oid, year):
                assignment[oid] = year
                backtrack(index + 1)
                del assignment[oid]

    backtrack(0)
    return solutions


def narrowest_sets(solutions, objects):
    """Per-object set of years that occur in at least one joint solution."""
    sets = {}
    for oid in objects:
        sets[oid] = sorted({sol[oid] for sol in solutions})
    return sets


def basis_fingerprint(domains, verified_edges):
    """Deterministic fingerprint of exactly what the solver consumed."""
    payload = {
        "domains": {
            oid: [domains[oid]["low"], domains[oid]["high"]]
            for oid in sorted(domains)
            if domains[oid]["feasible"]
        },
        "edges": sorted(
            (e["edge_id"], e["earlier"], e["later"], int(e["delta"]))
            for e in verified_edges
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_labels(narrowest, language, disputed, title):
    low, high = narrowest[0], narrowest[-1]
    if disputed:
        text = format_range(low, high, language) + disputed_suffix(language)
        kind = "range-disputed"
    else:
        text = format_internal_year(low, language)
        kind = "point"
    return {
        "language": language,
        "title": title,
        "text": text,
        "kind": kind,
        "low_year": low,
        "high_year": high,
    }


def build_publish_version(readings, editorials, edges, sequence, published_at,
                          languages=("zh", "en"), loan_scope=None, print_run=None):
    """Pure projection of the current evidence ledger onto a catalog version.

    Chinese and English labels are produced from the *same* solution set and
    carry the same ``version_id``, so a bilingual publication can never cite
    two different reconciliations.
    """
    domains = active_domains(readings)
    verified = [e for e in edges if e["status"] == "verified"]
    pending = [e for e in edges if e["status"] == "pending"]
    withdrawn = [e for e in edges if e["status"] == "withdrawn"]
    # solve_joint filters verified edges internally; the full list keeps ids.
    solutions = solve_joint(domains, edges)
    objects = sorted(domains)
    infeasible = [oid for oid in objects if not domains[oid]["feasible"]]
    conflict = bool(infeasible) or (bool(verified) and not solutions)
    basis = basis_fingerprint(domains, verified)
    version_id = f"cat-v{sequence}-{basis[:10]}"

    if conflict:
        status = "conflict"
        narrowest = {oid: [] for oid in objects}
    else:
        status = "resolved" if len(solutions) == 1 else "disputed"
        narrowest = narrowest_sets(solutions, objects)

    active_editorials = [r for r in editorials if r["state"] == "active"]
    version_objects = {}
    for oid in objects:
        titles = {
            r["language"]: r["title"]
            for r in active_editorials if r["object_id"] == oid
        }
        labels = {}
        for language in languages:
            if not conflict and narrowest[oid]:
                labels[language] = build_labels(
                    narrowest[oid], language, status == "disputed",
                    titles.get(language, oid),
                )
        provenance_sources = set(domains[oid]["sources"])
        provenance_editorials = sorted(
            {
                f"{r['object_id']}:{r['language']}:r{r['revision']}"
                for r in active_editorials if r["object_id"] == oid
            }
        )
        provenance_edges = sorted(
            e["edge_id"] for e in edges
            if e["status"] in ("verified", "pending")
            and oid in (e["earlier"], e["later"])
        )
        version_objects[oid] = {
            "domain": [domains[oid]["low"], domains[oid]["high"]],
            "domain_feasible": domains[oid]["feasible"],
            "narrowest_years": narrowest.get(oid, []),
            "labels": labels,
            "dependent_sources": sorted(provenance_sources),
            "dependent_editorials": provenance_editorials,
            "dependent_edges": provenance_edges,
        }

    return {
        "version_id": version_id,
        "sequence": sequence,
        "published_at": int(published_at),
        "status": status,
        "basis": basis,
        "solution_count": len(solutions),
        "solutions": solutions,
        "objects": version_objects,
        "conflict_edges": sorted(e["edge_id"] for e in verified) if conflict else [],
        "pending_edges": [
            {
                "edge_id": e["edge_id"], "earlier": e["earlier"],
                "later": e["later"], "delta": int(e["delta"]),
                "source_id": e["source_id"], "reviewer": e.get("reviewer"),
            }
            for e in pending
        ],
        "withdrawn_edges": [
            {"edge_id": e["edge_id"], "withdrawn_at": int(e["withdrawn_at"])}
            for e in withdrawn
        ],
        "infeasible_objects": sorted(infeasible),
        "loan_scope": sorted(loan_scope or []),
        "print_run": print_run,
    }


# --- Event-sourced persistence layer --------------------------------------

def _table_rows(con, sql):
    return [dict(r) for r in con.execute(sql)]


def snapshot(con):
    return {
        "readings": _table_rows(con, "SELECT * FROM chrono_readings"),
        "editorials": _table_rows(con, "SELECT * FROM chrono_editorials"),
        "edges": _table_rows(con, "SELECT * FROM chrono_edges"),
    }


def loaned_objects(con):
    """Currently authorized loan subjects. Loan state never endorses dates."""
    return [
        r["id"] for r in con.execute(
            "SELECT id FROM subjects WHERE state='authorized' ORDER BY id"
        )
    ]


def _next_sequence(con):
    row = con.execute("SELECT COALESCE(MAX(sequence),0)+1 AS n FROM catalog_versions").fetchone()
    return int(row["n"])


def _existing_version_for_basis(con, basis):
    row = con.execute(
        "SELECT version_id,payload FROM catalog_versions WHERE basis=? ORDER BY sequence LIMIT 1",
        (basis,),
    ).fetchone()
    if not row:
        return None
    return row["version_id"], json.loads(row["payload"])


def _apply_publish(con, event):
    p = event.get("data", {})
    snap = snapshot(con)
    if not snap["readings"]:
        return _block(con, event, "no-ingested-evidence")
    current = build_publish_version(
        snap["readings"], snap["editorials"], snap["edges"],
        sequence=_next_sequence(con),
        published_at=int(event.get("effective_at", 0)),
        languages=tuple(p.get("languages", ("zh", "en"))),
        loan_scope=loaned_objects(con),
        print_run=p.get("print_run"),
    )
    duplicate = _existing_version_for_basis(con, current["basis"])
    if duplicate is not None:
        version_id, payload = duplicate
        return _decide(con, event, "unchanged",
                       "identical-basis-already-published", version_id, payload)
    con.execute(
        "INSERT INTO catalog_versions VALUES(?,?,?,?,?,?,?)",
        (current["version_id"], current["sequence"], current["published_at"],
         current["status"], current["basis"], current["solution_count"],
         json.dumps(current, ensure_ascii=False, sort_keys=True),
         p.get("print_run")),
    )
    return _decide(con, event, current["status"], "joint-chronology-published",
                   current["version_id"], current)


def _decide(con, event, outcome, reason, version_id, payload):
    from . import storage as s
    result = s.decide(con, event, NAME, outcome, reason, version_id)
    result["version_id"] = version_id
    result["catalog"] = payload
    return result


def _block(con, event, reason):
    from . import storage as s
    return s.decide(con, event, NAME, "blocked", reason, "blocked")


def apply(con, event):
    """Catalog subsystem event handler, replayed deterministically by the CLI."""
    action = event["action"]
    p = event.get("data", {})
    subject = event["subject"]
    effective = int(event.get("effective_at", 0))

    if action == "ingest_source":
        source_id = p.get("source_id", subject)
        if con.execute("SELECT 1 FROM chrono_sources WHERE source_id=?", (source_id,)).fetchone():
            return _block(con, event, "source-already-ingested")
        con.execute(
            "INSERT INTO chrono_sources VALUES(?,?,?,?,?,?,?,?,?)",
            (source_id, p["kind"], p["summary"], p.get("page"),
             p["era_system"], p.get("translation_version"), p.get("reviewer"),
             int(p.get("revised_at", effective)), 0, "active"),
        )
        return _simple(con, event, "ingested", "source-recorded", source_id)

    if action == "withdraw_source":
        source_id = p.get("source_id", subject)
        row = con.execute("SELECT state FROM chrono_sources WHERE source_id=?", (source_id,)).fetchone()
        if not row:
            return _block(con, event, "unknown-source")
        con.execute(
            "UPDATE chrono_sources SET state='withdrawn', withdrawn_at=? WHERE source_id=?",
            (effective, source_id),
        )
        con.execute(
            "UPDATE chrono_readings SET state='withdrawn' WHERE source_id=? AND state='active'",
            (source_id,),
        )
        return _simple(con, event, "withdrawn", "source-and-readings-withdrawn", source_id)

    if action == "add_reading":
        source_id = p["source_id"]
        source = con.execute("SELECT state FROM chrono_sources WHERE source_id=?", (source_id,)).fetchone()
        if not source:
            return _block(con, event, "unknown-source")
        if source["state"] != "active":
            return _block(con, event, "source-withdrawn")
        low, high = int(p["low_year"]), int(p["high_year"])
        if high < low:
            return _block(con, event, "invalid-reading-range")
        con.execute(
            "INSERT INTO chrono_readings(source_id,object_id,candidate,low_year,high_year,confidence,state) VALUES(?,?,?,?,?,?,?)",
            (source_id, p["object_id"], p["candidate"], low, high,
             p.get("confidence", "candidate"), "active"),
        )
        return _simple(con, event, "recorded", "reading-candidate-stored",
                       f"{source_id}:{p['object_id']}")

    if action == "withdraw_reading":
        cur = con.execute(
            "UPDATE chrono_readings SET state='withdrawn' WHERE source_id=? AND object_id=? AND candidate=? AND state='active'",
            (p["source_id"], p["object_id"], p["candidate"]),
        )
        if cur.rowcount == 0:
            return _block(con, event, "unknown-active-reading")
        return _simple(con, event, "withdrawn", "misreading-withdrawn",
                       f"{p['source_id']}:{p['object_id']}:{p['candidate']}")

    if action == "editorial":
        exists = con.execute(
            "SELECT 1 FROM chrono_editorials WHERE object_id=? AND language=? AND revision=?",
            (p["object_id"], p["language"], int(p["revision"])),
        ).fetchone()
        if exists:
            return _block(con, event, "editorial-revision-conflict")
        con.execute(
            "INSERT INTO chrono_editorials(object_id,language,revision,received_at,title,date_label,source_id,translation_version,state,withdrawn_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p["object_id"], p["language"], int(p["revision"]), int(p["received_at"]),
             p["title"], p["date_label"], p["source_id"],
             p.get("translation_version"), "active", 0),
        )
        return _simple(con, event, "recorded", "bilingual-editorial-stored",
                       f"{p['object_id']}:{p['language']}:r{p['revision']}")

    if action == "withdraw_editorial":
        cur = con.execute(
            "UPDATE chrono_editorials SET state='withdrawn', withdrawn_at=? WHERE object_id=? AND language=? AND revision=? AND state='active'",
            (effective, p["object_id"], p["language"], int(p["revision"])),
        )
        if cur.rowcount == 0:
            return _block(con, event, "unknown-active-editorial")
        return _simple(con, event, "withdrawn", "editorial-withdrawn",
                       f"{p['object_id']}:{p['language']}:r{p['revision']}")

    if action == "add_edge":
        edge_id = p["edge_id"]
        if con.execute("SELECT 1 FROM chrono_edges WHERE edge_id=?", (edge_id,)).fetchone():
            return _block(con, event, "edge-already-recorded")
        con.execute(
            "INSERT INTO chrono_edges VALUES(?,?,?,?,?,?,?,?,?)",
            (edge_id, p["earlier"], p["later"], int(p["delta"]), p["source_id"],
             p.get("reviewer"), p.get("status", "verified"),
             int(p.get("revised_at", effective)), 0),
        )
        return _simple(con, event, "recorded", "relative-edge-stored", edge_id)

    if action == "verify_edge":
        edge_id = p["edge_id"]
        row = con.execute("SELECT status FROM chrono_edges WHERE edge_id=?", (edge_id,)).fetchone()
        if not row:
            return _block(con, event, "unknown-edge")
        new_status = p.get("status", "verified")
        if new_status not in ("verified", "pending", "rejected"):
            return _block(con, event, "invalid-edge-status")
        if new_status == "rejected":
            con.execute(
                "UPDATE chrono_edges SET status='withdrawn', withdrawn_at=?, reviewer=? WHERE edge_id=?",
                (effective, p.get("reviewer"), edge_id),
            )
            return _simple(con, event, "withdrawn", "edge-rejected-and-withdrawn", edge_id)
        con.execute(
            "UPDATE chrono_edges SET status=?, reviewer=?, revised_at=? WHERE edge_id=?",
            (new_status, p.get("reviewer"), effective, edge_id),
        )
        return _simple(con, event, new_status, "edge-verification-recorded", edge_id)

    if action == "withdraw_edge":
        edge_id = p["edge_id"]
        row = con.execute("SELECT status FROM chrono_edges WHERE edge_id=?", (edge_id,)).fetchone()
        if not row:
            return _block(con, event, "unknown-edge")
        con.execute(
            "UPDATE chrono_edges SET status='withdrawn', withdrawn_at=? WHERE edge_id=?",
            (effective, edge_id),
        )
        return _simple(con, event, "withdrawn", "relative-edge-withdrawn", edge_id)

    if action == "publish":
        return _apply_publish(con, event)

    if action == "erratum":
        return _apply_erratum(con, event)

    return _block(con, event, "unknown-action")


def _simple(con, event, outcome, reason, state):
    from . import storage as s
    return s.decide(con, event, NAME, outcome, reason, state)


def _apply_erratum(con, event):
    p = event.get("data", {})
    effective = int(event.get("effective_at", 0))
    old_row = con.execute(
        "SELECT version_id,payload FROM catalog_versions WHERE version_id=?",
        (p["old_version_id"],),
    ).fetchone()
    if not old_row:
        return _block(con, event, "unknown-old-version")
    old_payload = json.loads(old_row["payload"])
    snap = snapshot(con)
    new_version = build_publish_version(
        snap["readings"], snap["editorials"], snap["edges"],
        sequence=_next_sequence(con),
        published_at=effective,
        languages=tuple(p.get("languages", ("zh", "en"))),
        loan_scope=loaned_objects(con),
        print_run=p.get("print_run"),
    )
    # A correction must be based on changed evidence; identical basis is a no-op.
    if new_version["basis"] == old_payload["basis"]:
        return _block(con, event, "no-evidence-change-for-correction")
    duplicate = _existing_version_for_basis(con, new_version["basis"])
    if duplicate is None:
        con.execute(
            "INSERT INTO catalog_versions VALUES(?,?,?,?,?,?,?)",
            (new_version["version_id"], new_version["sequence"], new_version["published_at"],
             new_version["status"], new_version["basis"], new_version["solution_count"],
             json.dumps(new_version, ensure_ascii=False, sort_keys=True),
             p.get("print_run")),
        )
        new_version_id = new_version["version_id"]
        new_payload = new_version
    else:
        new_version_id, new_payload = duplicate

    affected = []
    for oid in sorted(old_payload.get("objects", {})):
        old_labels = old_payload["objects"][oid].get("labels", {})
        new_labels = new_payload.get("objects", {}).get(oid, {}).get("labels", {})
        for language in sorted(set(old_labels) | set(new_labels)):
            before = old_labels.get(language, {}).get("text")
            after = new_labels.get(language, {}).get("text")
            if before != after:
                affected.append({
                    "object_id": oid, "language": language,
                    "old_text": before, "new_text": after,
                })
    erratum_id = p.get("erratum_id", f"erratum-{event['id']}")
    if con.execute("SELECT 1 FROM catalog_errata WHERE erratum_id=?", (erratum_id,)).fetchone():
        return _block(con, event, "erratum-already-recorded")
    con.execute(
        "INSERT INTO catalog_errata VALUES(?,?,?,?,?,?)",
        (erratum_id, event["id"], p["old_version_id"], new_version_id,
         json.dumps(affected, ensure_ascii=False), p.get("justification", ""), effective),
    )
    payload = {
        "erratum_id": erratum_id,
        "old_version_id": p["old_version_id"],
        "old_print_run": old_payload.get("print_run"),
        "new_version_id": new_version_id,
        "affected_labels": affected,
        "new_catalog": new_payload,
    }
    return _decide(con, event, "corrected", "erratum-issued-old-version-retained",
                   new_version_id, payload)
