#!/usr/bin/env python3
# Reports integration (step 5) - generate report.md/.csv/.html + audit.txt
# using public interfaces only, disjoint from scan logic.

import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import run_db

REPORT_DIR = os.path.join(_HERE, "reports_demo")

def generate_all(conn, run_id):
    os.makedirs(REPORT_DIR, exist_ok=True)
    # Report files from DB
    written = run_db.write_report(conn, run_id, REPORT_DIR)
    # Audit.txt from ranked candidates
    audit_path = os.path.join(REPORT_DIR, "audit.txt")
    rows = run_db.ranked(conn, run_id)
    with open(audit_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("# audit.txt - fixformat v6\n# offset | length | ext | validated | confidence\n")
        for r in rows:
            fh.write("%d | %d | %s | %s | %.4f\n" % (
                r["offset"] or 0,
                r["length"] or 0,
                r["ext"] or "",
                "yes" if r["validated"] else "no",
                float(r["confidence"] or 0.0)
            ))
    return {"report_md": written.get("md"), "report_csv": written.get("csv"),
            "report_html": written.get("html"), "audit_txt": audit_path,
            "files_written": len(written) + 1}

if __name__ == "__main__":
    conn = run_db.open_run_db(":memory:")
    rid = run_db.new_run(conn, source="demo", source_size=1024*1024*5, mode=run_db.MODE_REPORT)
    run_db.add_candidate(conn, rid, offset=512, length=1024, ext=".jpg", validated=1, confidence=0.88)
    result = generate_all(conn, rid)
    print("Generated:", result)
