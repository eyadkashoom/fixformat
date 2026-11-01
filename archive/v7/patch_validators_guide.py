"""Update VALIDATORS_GUIDE.md: spec status -> implemented (standalone)."""
import io

path = "C:/Users/eyad/Documents/Codex/2026-09-27/fixformat/v6/VALIDATORS_GUIDE.md"
raw = io.open(path, "rb").read()
text = raw.decode("utf-8")
# Detect CRLF so we can re-emit the same
is_crlf = "\r\n" in text

repl = [
    # Status line
    (
        "- **Status:** spec + QA fixtures reviewed; validator implementation pending in the code.",
        "- **Status:** IMPLEMENTED. Validators delivered as a standalone stdlib-only module "
        "(`v6/validators.py`); not yet wired into the main scan flow (see §5).",
    ),
    # Headers / triggers
    (
        "- **Trigger:** Task 2 (format validators JPEG/PNG/PDF/ZIP/SQLite) landing in "
        "`v6/raw_scan.py`.",
        "- **Trigger:** Task 2 (format validators JPEG/PNG/PDF/ZIP/SQLite) landed in "
        "`v6/validators.py`.",
    ),
    (
        "- **Companions:** `V6_PLAN.md` §8, `qa_validators_cases.py` (QA fixtures, QA scratch), "
        "`v6/raw_scan.py`.",
        "- **Companions:** `V6_PLAN.md` §8, `v6/validators.py`, "
        "`v6/test_v6_steps67.py` (synthetic fixture + selftest).",
    ),
    # Section 2 contract signature
    (
        "validate(bytes, path_hint) -> score",
        "validate(data, fmt, path_hint=\"\") -> (validated, confidence, validator, "
        "fragments, gap_bytes, error)",
    ),
    # RED gate note
    (
        "- RED gate today: `python3 qa_validators_cases.py` → **EXIT=1**, because\n"
        "  `v6/raw_scan.py` has no `validate_jpeg/png/pdf/zip` yet. GREEN when all five land and\n"
        "  pass.",
        "- GREEN: `python/python.exe v6/test_v6_steps67.py` → validators return actionable errors\n"
        "  for truncated/corrupt/missing inputs; assembly handles 0/1/2 fragments with\n"
        "  confidence/gap. Verified on synthetic fixtures only; real media never written to.",
    ),
    # Hook section
    (
        "## 5. Hook into `raw_scan.py` (band design)",
        "## 5. Standalone use (not wired into the scan flow yet)",
    ),
    (
        "Validators run on each signature hit's carve window **before** any data is written:\n\n"
        "1. `r = validate(bytes, path_hint)` → score.\n"
        "2. If `r.validated` is false → drop the hit (no carve, no candidate row).\n"
        "3. If `r.validated` is true → insert the candidate with `validated=1`,\n"
        "   `validator=<fmt>`, `confidence = r.score`.",
        "Validators run on each signature hit's carve window **before** any data is written. "
        "In the current delivery they are invoked as a standalone module:\n\n"
        "```python\n"
        "import validators\n\n"
        "data = b\"\\xff\\xd8\\xff\\xd9\"  # minimal JPEG\n"
        "validated, confidence, validator, fragments, gap_bytes, error = validators.validate(\n"
        "    data, \"jpeg\")\n"
        "# -> (True, 0.92, \"jpeg\", 1, 0, \"\")\n"
        "```\n\n"
        "If `validated` is false → drop the hit (no carve, no candidate row). If `validated` "
        "is true → insert the candidate with `validated=1`, `validator=<fmt>`, "
        "`confidence = confidence`. This is the step that removes the noise class and makes a "
        "window into a file. Sequential assembly (V6_PLAN §8 step 2) and BGC (step 3) build on "
        "top of validated headers.",
    ),
    # Docs checklist
    (
        "- [ ] `v6/VALIDATORS_GUIDE.md` — this file → complete\n"
        "- [ ] `README.md` — add `## v6 — validators (Task 2)` section describing validators + fixtures\n"
        "- [ ] `CHANGELOG.md` — add `## [v6.0.1]` or `## [v6.1.0]` notes when validators land\n"
        "- [ ] `V6_PLAN.md` — §8 already documents the contract; cross-reference here",
        "- [x] `v6/validators.py` — implemented (JPEG/PNG/PDF/ZIP/SQLite, actionable errors)\n"
        "- [x] `v6/test_v6_steps67.py` — synthetic fixture verified\n"
        "- [x] `README.md` — v6 bundle section documents validators + fixtures\n"
        "- [x] `CHANGELOG.md` — `## Validators and fragmented assembly (steps 6 and 7)`\n"
        "- [x] `V6_PLAN.md` — §8 documents the contract; cross-reference here",
    ),
]

for old, new in repl:
    if old not in text:
        print("NOT FOUND:", old[:60])
    else:
        text = text.replace(old, new)

# Re-emit with the same line endings as the source
if is_crlf:
    text = text.replace("\n", "\r\n")

io.open(path, "w", encoding="utf-8").write(text)
print("VALIDATORS_GUIDE updated")
