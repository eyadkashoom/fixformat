"""K5 report rendering: structured summaries, tables, optional file export."""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Optional


class ReportSchemaError(Exception):
    pass


SCHEMA_KEYS = {"offset", "length", "label", "ext", "sha256", "validated", "confidence", "fragments", "gap_bytes", "name_guess"}


def validate_row(row: Dict[str, Any]) -> None:
    for k in row:
        if k not in SCHEMA_KEYS:
            raise ReportSchemaError(f"unknown key: {k}")


def render_summary(data: List[Dict[str, Any]]) -> str:
    total = len(data)
    validated = sum(1 for r in data if r.get("validated"))
    by_ext: Dict[str, int] = {}
    for r in data:
        ext = r.get("ext", "unknown")
        by_ext[ext] = by_ext.get(ext, 0) + 1
    lines = [
        "# K5 Report Summary",
        f"- Candidates: {total}",
        f"- Validated: {validated}",
        f"- By extension: {', '.join(f'{k}={v}' for k,v in sorted(by_ext.items()))}",
    ]
    return "\n".join(lines)


def render_table(data: List[Dict[str, Any]]) -> str:
    if not data:
        return "No candidates."
    headers = ["offset", "length", "label", "ext", "validated", "confidence"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in data:
        vals = [str(r.get(h, "")) for h in headers]
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def render_csv(data: List[Dict[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    headers = ["offset", "length", "label", "ext", "sha256", "validated", "confidence", "fragments", "gap_bytes", "name_guess"]
    writer.writerow(headers)
    for r in data:
        writer.writerow([r.get(h, "") for h in headers])
    return out.getvalue()


def export(data: List[Dict[str, Any]], format_type: str = "md") -> str:
    for r in data:
        validate_row(r)
    if format_type == "md":
        return render_summary(data) + "\n\n" + render_table(data)
    elif format_type == "csv":
        return render_csv(data)
    elif format_type == "json":
        return json.dumps({"summary": {"total": len(data)}, "rows": data}, indent=2)
    else:
        raise ReportSchemaError(f"unsupported format: {format_type}")
