#!/usr/bin/env python3
# Plan screen (step 4) - renders recovery plan from run DB using public interfaces.
# Keeps UI/reporting/audit disjoint from data/validators/performance.

import sys, os, json
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import run_db

def render_plan_screen(conn, run_id, out_path=None):
    info = run_db.summary(conn, run_id)
    result = run_db.plan_extraction(conn, run_id, run_db.RULE_EVERYTHING)
    lines = []
    lines.append("=" * 70)
    lines.append("RECOVERY PLAN - what is on this source, and what will fit")
    lines.append("=" * 70)
    lines.append("  candidates        : %d found, %d validated" % (info["candidates"], info["validated"]))
    lines.append("  total carved size : %s if everything is extracted" % run_db.fmt_bytes(info["bytes"]))
    by_ext = info.get("by_extension", [])
    ext_lines = ""
    for item in by_ext[:5]:
        ext_lines += "   %s %d (%.2f %s)" % (item.get("ext", "other"), item.get("files", 0), float(item.get("bytes", 0)) / (1024**3) if item.get("bytes", 0) > 0 else 0, "GB")
    if ext_lines:
        lines.append("  by type           :" + ext_lines)
    lines.append("  planned bytes      : %s (%d files)" % (run_db.fmt_bytes(result.get("planned_bytes", 0)), result.get("planned_files", 0)))
    lines.append("  fits               : %s" % ("yes" if result.get("fits") else "no"))
    lines.append("  Extract which of them?")
    lines.append("    [1] everything that fits          -> %s" % run_db.fmt_bytes(result.get("planned_bytes", 0)))
    lines.append("    [2] validated only                -> %s" % run_db.fmt_bytes(result.get("planned_bytes", 0)))
    lines.append("    [3] documents only (.pdf/.docx/.md/.txt) -> %s" % run_db.fmt_bytes(result.get("planned_bytes", 0)))
    lines.append("    [4] my own filter (type / size / date / confidence)")
    output = "\n".join(lines) + "\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(output)
    return output, result

if __name__ == "__main__":
    # Example with synthetic fixture from v6 db_selftest
    import tempfile, sqlite3
    # Create temporary synthetic DB for demo
    conn = run_db.open_run_db(":memory:")
    rid = run_db.new_run(conn, tool_version="6.0", source="test.img", source_size=1024*1024*10,
                         mode=run_db.MODE_REPORT, workers=1)
    # Add some synthetic candidates
    run_db.add_candidate(conn, rid, offset=1024, length=4096, label="test.jpg",
                          ext=".jpg", validated=1, confidence=0.95, fragments=1)
    run_db.add_candidate(conn, rid, offset=8192, length=2048, label="test.pdf",
                          ext=".pdf", validated=0, confidence=0.50)
    text, res = render_plan_screen(conn, rid)
    print(text)
    print("Plan result keys:", sorted(res.keys()))
