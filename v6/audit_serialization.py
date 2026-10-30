#!/usr/bin/env python3
# Audit.txt serialization (step 12) - machine-readable audit per carved file.
# Keeps audit disjoint from data/validators/performance.

import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import run_db

def write_audit_txt(conn, run_id, out_path, limit=500):
    """Write audit.txt: offset size type fragments confidence [label]"""
    rows = run_db.ranked(conn, run_id, limit=limit)
    lines = ["# audit.txt - fixformat v6 - one line per candidate", "# fields: offset | length | ext | fragments | confidence | label | validated | sha256"]
    for row in rows:
        lines.append("%d | %d | %s | %d | %.4f | %s | %s | %s" % (
            row["offset"] if row["offset"] else 0,
            row["length"] if row["length"] else 0,
            row["ext"] or "",
            row["fragments"] if row["fragments"] else 1,
            float(row["confidence"] or 0.0),
            row["label"] or "",
            "yes" if row["validated"] else "no",
            (row["sha256"] or "")[:16]
        ))
    text = "\n".join(lines) + "\n"
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return out_path, len(rows)

if __name__ == "__main__":
    conn = run_db.open_run_db(":memory:")
    rid = run_db.new_run(conn, source="audit_demo.img", source_size=1024*1024)
    run_db.add_candidate(conn, rid, offset=1024, length=8192, ext=".jpg", label="photo",
                          validated=1, confidence=0.92, fragments=1)
    path, count = write_audit_txt(conn, rid, os.path.join(_HERE, "audit_demo.txt"))
    print("wrote %s (%d lines)" % (path, count))
    with open(path, "r", encoding="utf-8") as fh:
        print(fh.read())
