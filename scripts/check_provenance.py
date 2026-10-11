#!/usr/bin/env python3
"""Trace a published report's numbers back to the briefing that produced them.

This is the entry point for the provenance index — without it
carrac/grounding/provenance.py would be library code with no caller, which is
this codebase's most common defect.

    # every unsourced number in the latest general report
    python scripts/check_provenance.py --latest general

    # a specific report
    python scripts/check_provenance.py --id 373

    # "where did this number come from?" — the grounding question
    python scripts/check_provenance.py --id 373 --value 165,694

Run it inside the app container, which has the DB credentials:

    docker compose exec carrac python scripts/check_provenance.py --latest general

Read-only. Reports whose source_briefing has aged out of the 90-day retention
window (prune_old_source_briefings) cannot be checked — that is expected, and
the tool says so rather than reporting every number as unsourced.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

def _fetch(report_id=None, latest=None):
    from carrac.db_pool import get_reports_conn
    with get_reports_conn() as conn:
        cur = conn.cursor()
        if report_id:
            cur.execute("SELECT id, report_type, content, source_briefing "
                        "FROM reports WHERE id = %s", (report_id,))
        else:
            cur.execute("SELECT id, report_type, content, source_briefing "
                        "FROM reports WHERE report_type = %s "
                        "ORDER BY created_at DESC LIMIT 1", (latest,))
        return cur.fetchone()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, help="report id")
    g.add_argument("--latest", help="report_type, e.g. general / afternoon / weekly")
    ap.add_argument("--value", help="trace a single number instead of the whole report")
    ap.add_argument("--all-sections", action="store_true",
                    help="include code-rendered sections (normally suppressed)")
    args = ap.parse_args()

    # Imported after parse_args: importing carrac creates ./data as a side
    # effect, which `--help` must not do.
    from carrac.grounding.provenance import (
        DETERMINISTIC_SECTIONS,
        check_provenance,
        source_of,
        split_retained,
    )

    row = _fetch(args.id, args.latest)
    if not row:
        print("no such report", file=sys.stderr)
        return 2
    rid, rtype, content, briefing = row[0], row[1], row[2], row[3]
    if not briefing:
        print(f"report {rid} ({rtype}) has no stored briefing — it predates stored briefings "
              f"or has aged past the 90-day retention window. Nothing to check against.",
              file=sys.stderr)
        return 3

    if args.value:
        blocks = source_of(args.value, briefing)
        print(f"report {rid} ({rtype}) — {args.value!r}")
        if blocks:
            for b in blocks:
                print(f"  sourced from: {b}")
        else:
            print("  NO SOURCE — this number is in no block of the briefing.")
        return 0

    skip = () if args.all_sections else DETERMINISTIC_SECTIONS
    out = check_provenance(content, briefing, skip_sections=skip)
    pct = out["grounded"] / out["checked"] * 100 if out["checked"] else 0.0
    prompt, retained = split_retained(briefing)
    print(f"report {rid} ({rtype})")
    print(f"  briefing {len(prompt):,} chars prompt + {len(retained):,} chars retained")
    print(f"  numbers checked : {out['checked']}")
    print(f"  with provenance : {out['grounded']} ({pct:.1f}%)")
    print(f"  unsourced       : {len(out['orphans'])}")
    for o in out["orphans"]:
        print(f"\n  {o['value']}   [{o['section']}]")
        print(f"      ...{o['context'][:150]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
