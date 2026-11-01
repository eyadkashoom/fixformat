# Reports — CLI — fixformat v6

**Author:** @technical_writer_docs
**Trigger:** Task 3 (CLI theming, progress, and reports).
**Companions:** `v6/k5_report.py`, `v6/test_display_layer.py`.
**Status:** IMPLEMENTED — docs merged for v6.1.0.

---

## 1. Scope

This guide documents report generation and export in fixformat v6:

- The **`export()`** entry point and its three formats: `md`, `csv`, `json`.
- The **strict schema** (`SCHEMA_KEYS`, `validate_row`, `ReportSchemaError`).
- Each format's exact shape and column order.
- Export options and examples.
- Troubleshooting.

Scope is **text/terminal only**. There is no GUI in v6.

---

## 2. Entry point

`k5_report.export(data: list[dict], format_type: str = "md") -> str` renders a
report from a list of candidate rows and returns the text. It

1. validates every row against the schema (raises `ReportSchemaError` on an
   unknown key);
2. renders the requested format;
3. returns **plain text only** — no ANSI bytes under any condition.

`format_type` is case-sensitive and one of `"md"`, `"csv"`, `"json"`. Anything
else raises `ReportSchemaError` with `"unsupported format: ..."`.

---

## 3. Strict schema

### 3.1 Schema keys

A candidate row must contain **only** these keys (any other key raises):

```
offset, length, label, ext, sha256, validated, confidence,
fragments, gap_bytes, name_guess
```

`SCHEMA_KEYS` pins the ten-column contract (10 keys).

### 3.2 Validation

`validate_row(row)` iterates the row and raises `ReportSchemaError(f"unknown key: {k}")`
for any key not in `SCHEMA_KEYS`. Unknown report keys and unsupported formats
both raise `ReportSchemaError` — this is the machine-checked gate.

### 3.3 Notes

- Types: `offset`, `length`, `validated`, `fragments`, `gap_bytes`, `name_guess`
  come from the row as-is (`str()`-ed by the renderer); `sha256` is a 64-char
  hex string; `confidence` is a float.
- The renderer never writes files itself — it returns a string. Callers write
  the string to a file (explicitly `encoding="utf-8"`); ANSI is never present,
  so no stripping is needed.

---

## 4. Format reference

### 4.1 Markdown (`md`)

```
# K5 Report Summary
- Candidates: 2
- Validated: 1
- By extension: .jpg=1, .png=1

| offset | length | label | ext | validated | confidence |
|--------|--------|-------|-----|-----------|------------|
| 1024 | 8192 | JPEG | .jpg | 1 | 0.92 |
| 2048 | 4096 | PNG | .png | 0 | 0.55 |
```

- Summary header + counts + extension breakdown (sorted keys).
- A table with the **output summary columns** (`offset`, `length`, `label`,
  `ext`, `validated`, `confidence`) is emitted even when the data is empty,
  in which case the table renders `"No candidates."`.
- The summary header/section is emitted together with the table for all
  three formats (`md`, `csv`, `json`), with ten-source columns for
  `csv` and `json` and the six summary table columns for `md`.

### 4.2 CSV (`csv`)

Header (exact order):

```
offset, length, label, ext, sha256, validated, confidence, fragments,
gap_bytes, name_guess
```

Rows are written with Python's `csv.writer` using `lineterminator="\n"`; values
are quoted per RFC 4180 when they contain commas, quotes, or pipes, so a label
like `a,b "q"` round-trips exactly (`"a,b ""q"""`) and a name like `x|y`
round-trips through the quoted field. The file ends with a newline.

### 4.3 JSON (`json`)

```json
{
  "summary": {"total": 2},
  "rows": [
    {"offset": 1024, "length": 8192, "label": "JPEG", "ext": ".jpg",
     "sha256": "a...a", "validated": 1, "confidence": 0.92,
     "fragments": 1, "gap_bytes": 0, "name_guess": "img_01"},
    ...
  ]
}
```

- `summary.total` = number of rows.
- `rows` = the original dicts, in input order.
- Deterministic: same input → byte-identical output across runs.

### 4.4 Empty and error shapes

| `data` | `format_type` | Result |
|---|---|---|
| `[]` | `md` | Summary with `Candidates: 0` + `"No candidates."` in the table |
| `[]` | `csv` | Header-only (single row) |
| `[]` | `json` | `{"summary": {"total": 0}, "rows": []}` |

---

## 5. Export options

`export()` returns a string; direct the output with the caller's choice of
redirect, file, or capture:

```bash
# stdout
python v6/raw_scan.py --output-report md > report.md

# file (explicit UTF-8)
python -c "open('report.md','w',encoding='utf-8').write(k5_report.export(rows,'md'))"

# programmatic format switch
md  = k5_report.export(rows, "md")
csv = k5_report.export(rows, "csv")
json= k5_report.export(rows, "json")
```

File writes are always plain (no ANSI), UTF-8-safe, and deterministic. Keep
ANSI out of files by never styling the row text before `export()`.

---

## 6. Examples

```python
import k5_report

FIXTURE = [
    {"offset": 1024, "length": 8192, "label": "JPEG", "ext": ".jpg",
     "sha256": "a"*64, "validated": 1, "confidence": 0.92,
     "fragments": 1, "gap_bytes": 0, "name_guess": "img_01"},
    {"offset": 2048, "length": 4096, "label": "PNG", "ext": ".png",
     "sha256": "b"*64, "validated": 0, "confidence": 0.55,
     "fragments": 2, "gap_bytes": 512, "name_guess": "unknown"},
]

# md
md = k5_report.export(FIXTURE, "md")
# -> "# K5 Report Summary\n- Candidates: 2\n..."

# csv (quoting round-trip)
tricky = [dict(FIXTURE[0], label='a,b "q"', name_guess="x|y")]
row = k5_report.export(tricky, "csv").splitlines()[1]
# -> ['a,b "q"', 'x|y'] in the CSV reader

# json
payload = json.loads(k5_report.export(FIXTURE, "json"))
# -> {"summary": {"total": 2}, "rows": [...]}

# determinism
assert k5_report.export(FIXTURE, "md") == k5_report.export(FIXTURE, "md")
```

### 6.1 Arabic/UTF-8 round-trip

Non-ASCII labels (e.g. Arabic) survive the UTF-8 round-trip in all three
formats; the file is written explicitly as UTF-8.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ReportSchemaError: unknown key: ...` | A row carries a key not in `SCHEMA_KEYS`. | Drop the key; the ten contract keys are `offset, length, label, ext, sha256, validated, confidence, fragments, gap_bytes, name_guess`. |
| `ReportSchemaError: unsupported format: xml` | Unknown `format_type`. | Use `"md"`, `"csv"`, or `"json"` (case-sensitive). |
| ANSI bytes in a report file | The input rows or table text were styled with ANSI. | Pass unstyled rows; `export()` never adds ANSI. |
| CSV row `"a,b ""q"""` reads back as one field | RFC 4180 quoting is working as designed. | Expected; a comma/quote/pipe label round-trips correctly. |
| Empty report has no rows | `export([], "md")` includes the summary and `"No candidates."` | Expected; CSV is header-only, JSON is `{"summary":{"total":0},"rows":[]}`. |
| `md` table shows only 6 columns | The summary table is the 6-column `render_table` output; the ten-column source table is used by `export`. | Use `export()` for the full ten-column CSV/JSON-round-trippable output. |
| Report not UTF-8 / mojibake on Arabic labels | Caller wrote with another encoding. | Open/write with `encoding="utf-8"` (already the `export` contract). |
| Output differs between two runs | Non-determinism in row order or timestamp fields. | `export()` sorts nothing; it emits input order. Pin your input ordering for byte-identical output. |

---

## 8. Acceptance

- [x] `md` / `csv` / `json` shapes and column orders pinned by test.
- [x] `ReportSchemaError` raised for unknown keys and unsupported formats.
- [x] Empty and error shapes match contract (md/csv/json).
- [x] CSV quoting round-trips commas, quotes, and pipes.
- [x] Arabic/UTF-8 labels survive round-trip in all formats.
- [x] Output is byte-identical across runs (determinism).
- [x] No ANSI bytes in any format (negative control passed).
