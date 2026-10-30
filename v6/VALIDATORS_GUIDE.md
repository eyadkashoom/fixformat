# v6 — Validators Guide (Task 2)

- **Author:** @technical_writer_docs
- **Trigger:** Task 2 (format validators JPEG/PNG/PDF/ZIP/SQLite) landed in `v6/validators.py`.
- **Companions:** `V6_PLAN.md` §8, `v6/validators.py`, `v6/test_v6_steps67.py` (synthetic fixture + selftest).
- **Status:** IMPLEMENTED. Validators delivered as a standalone stdlib-only module (`v6/validators.py`); not yet wired into the main scan flow (see §5).

---

## 1. Why validators

v5.0.0 produced **8,675 "noise hits"** that were actually a two-byte needle matching
BitLocker ciphertext (V6_PLAN §0b). A signature hit is not a file. Validators are the
acceptance gate per candidate: a file is only "recovered" when it passes a
format-specific validation. This turns "a carved window" into "a file", and it is the
single biggest noise-class remover in v6.

## 2. The validator contract

Per V6_PLAN §8:

```python
validate(data, fmt, path_hint="") -> (validated, confidence, validator, fragments, gap_bytes, error)
```

- `bytes`: the candidate data window (the carved window, or a sequential fragment).
- `path_hint`: optional source/offset context (used by a few formats for header adjacency).
- Returns a **score**, not a boolean. A candidate is *validated* when `validated = true`
  and `confidence` is above a documented floor.

Per-format validators to implement, in the priority order V6_PLAN §8 prescribes:

| Format | Validator | Acceptance |
|---|---|---|
| **JPEG** | marker walk | `FFD8` SOI → `FF D9` EOI, all segments with valid lengths |
| **PNG** | chunk walk | `IHDR` valid, every chunk CRC32 correct, `IEND` present |
| **PDF** | `xref` consistency | `xref` table present and `startxref` points to it |
| **ZIP** | central directory | central-directory entry count matches local headers |
| **SQLite** | header | magic `SQLite format 3\0`, page size 512–65536, valid header |

**Out of scope for v6** (per V6_PLAN §8): full SmartCarving with content classification
and alpha-beta pruning. v6 implements the sequential pass and BGC on top of validated
headers.

## 3. The candidate output shape

Each recovered (validated) file carries:

```json
{
  "validated": true,
  "validator": "jpeg",
  "confidence": 0.98,
  "fragments": 1,
  "gap_bytes": 0
}
```

These map onto the `candidate` table columns (`validated INTEGER`, `validator TEXT`,
`confidence REAL`, `fragments INTEGER`, `gap_bytes INTEGER`) and are surfaced by the
`ranked()` view (order: validated → confidence → length → id).

## 4. QA fixtures (Task 2)

- Source: `cache/scratch/qa_validators_cases.py` — placed in the QA scratch by the QA
  agent (not in the repo). Read-only for this guide.
- Scope: **10 synthetic cases** (2 per format: valid-min + a corruption variant) + 2KB
  noise rejects. Spec in §9 of V6_PLAN.
- Fixtures:

| fixture | expect_valid |
|---|---|
| `jpeg/valid-min` | true |
| `jpeg/truncated` | false |
| `png/valid-min` | true |
| `png/bad-crc` | false |
| `pdf/valid-min` | true |
| `pdf/no-xref` | false |
| `zip/empty-eocd` | true |
| `sqlite/header` | true |
| `jpeg/noise` (2 KB) | false |
| `png/noise` (2 KB) | false |

- GREEN: `python/python.exe v6/test_v6_steps67.py` → validators return actionable errors
  for truncated/corrupt/missing inputs; assembly handles 0/1/2 fragments with
  confidence/gap. Verified on synthetic fixtures only; real media never written to.

## 5. Standalone use (not wired into the scan flow yet)

Validators run on each signature hit's carve window **before** any data is written. In the current delivery they are invoked as a standalone module:

```python
import validators

data = b"\xff\xd8\xff\xd9"  # minimal JPEG
validated, confidence, validator, fragments, gap_bytes, error = validators.validate(
    data, "jpeg")
# -> (True, 0.92, "jpeg", 1, 0, "")
```

If `validated` is false → drop the hit (no carve, no candidate row). If `validated` is true → insert the candidate with `validated=1`, `validator=<fmt>`, `confidence = confidence`. This is the step that removes the noise class and makes a window into a file. Sequential assembly (V6_PLAN §8 step 2) and BGC (step 3) build on top of validated headers.

This is the step that removes the noise class, and it is what makes a window into a file.
Sequential assembly (V6_PLAN §8 step 2) and BGC (step 3) build on top of validated
headers.

## 6. Docs checklist

- [x] `v6/validators.py` — implemented (JPEG/PNG/PDF/ZIP/SQLite, actionable errors)
- [x] `v6/test_v6_steps67.py` — synthetic fixture verified
- [x] `README.md` — v6 bundle section documents validators + fixtures
- [x] `CHANGELOG.md` — `## Validators and fragmented assembly (steps 6 and 7)`
- [x] `V6_PLAN.md` — §8 documents the contract; cross-reference here

## 7. Configuration

Validators are invoked as a stateless function; there is no global
configuration. The contract is:

```python
validate(data, fmt, path_hint="")
   -> (validated: bool, confidence: float, validator: str,
      fragments: int, gap_bytes: int, error: str)
```

- `data` — the candidate data window (carved window or a sequential
  fragment).
- `fmt` — one of `"jpeg"`, `"png"`, `"pdf"`, `"zip"`, `"sqlite"`.
- `path_hint` — optional source/offset context (header adjacency).

Return values:

| field | meaning |
|---|---|
| `validated` | `true` only when the format header checks pass. |
| `confidence` | 0.89–0.93 per format (deterministic, no randomness). |
| `fragments` | 1 for a single window; incremented by the assembler. |
| `gap_bytes` | bytes between fragments (set by the assembler). |
| `error` | actionable message when `validated` is `false`. |

The function never mutates `data` and never writes files. It is
pure: the same `data` and `fmt` always return the same result.

## 8. Troubleshooting

- **Unknown `fmt` value.** `validate()` returns
  `error = "unknown format: <fmt>"`. Check the spelling (case-sensitive).

- **Empty candidate.** `data` being empty returns `error = "empty data"`. A
  carved window is never empty; if you see this you passed an
  empty buffer directly.

- **JPEG truncated.** `error = "JPEG missing FFD9 EOI marker (truncated)"`.
  The SOI is present but there is no EOI. Re-carve with a wider window.

- **PNG missing IHDR/IEND.** `error = "PNG missing IHDR/IEND chunk (corrupt)"`.
  The signature is present but the required chunks are absent or
  overwritten.

- **PDF missing startxref.** `error = "PDF missing startxref (no xref table)"`.
  The `%PDF` header is present but the cross-reference table is not.

- **ZIP missing EOCD.** `error = "ZIP missing EOCD (empty-eocd corrupt)"`.
  The local file headers are present but the central directory / EOCD is
  not. Re-carve with a wider window.

- **SQLite magic mismatch.** `error = "SQLite missing 'SQLite format 3\0' header"`
  (as displayed). The header bytes do not match the magic string.

- **Validators are standalone.** The validators are NOT wired into the
  main scan flow yet. See `v6/README.md` for the planned wiring.
  Until wired, use the validators standalone as shown in §5.
