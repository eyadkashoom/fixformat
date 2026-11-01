# UI/UX Contracts — fixformat CLI (v6)

**Owner:** technical_writer_docs (study) + core_developer (impl) + qa_automated_tester (verification)
**Status:** Draft v1 — contracts for team review before coding.
**Scope:** Text/terminal interface only. No GUI (embedded Python 3.12 has no new libs). All changes in source files (`raw_scan.py`, `volume_gate.py`, `selftest.py`, `assembly.py`, `validators.py`); no device/image writes.

---

## 1. UX Audit (raw_scan.py)

Unify prompt/error/title formatting across the CLI:

- **Prompts:** single consistent question style — `? <question> [default]` with `[y/n]` for confirmations.
- **Errors:** `✗ <message>` (red when TTY) + exit code; never partial ANSI in files.
- **Titles/sections:** `=== <section> ===` (blue when TTY).
- **Paths:** always forward slashes in display; resolve to absolute internally.
- **Progress:** never interleave with prompts; use callback-driven updates (§4).

---

## 2. Terminal Formatting System (ANSI)

Contracts (from chat room, QA-approved):

| Rule | Requirement |
|---|---|
| Auto-disable | ANSI codes applied **only when stdout is a TTY** |
| `NO_COLOR` | Respect env var — disable coloring when set |
| No leak | `report.md`/`.csv`/provenance files are **plain text** (no ANSI bytes) |
| Coverage | `selftest.py` covers coloring on TTY + non-TTY + `NO_COLOR` + file output |
| Helpers | `green()`, `red()`, `yellow()`, `blue()` in `raw_scan.py`/`volume_gate.py` |
| Colors | green=success, red=error, yellow=warning, blue=info |

Implementation: small pure functions returning strings; no external deps (colorama not needed — raw ANSI via `sys.stdout.isatty()`).

---

## 3. Prefill & Memory

- Remember last used path (per-user, stored in `~/.fixformat/state.json` or env).
- Auto-detect devices/images (read-only probe; never write).
- Safe suggestions: default to read-only operations; destructive ops require explicit `--confirm`.
- Locale: respect `LANG`/`LC_ALL` for Arabic/English output.

---

## 4. Progress Callbacks (core_developer must define before coding)

Display layer ↔ scan logic separation requires a callback contract:

```python
# Contract (proposed)
class ProgressCallback:
    def on_start(self, total: int) -> None: ...
    def on_progress(self, done: int, total: int, rate: float) -> None: ...
    def on_file(self, path: str, size: int) -> None: ...
    def on_warning(self, code: str, message: str) -> None: ...
    def on_complete(self, summary: dict) -> None: ...
```

- Scan logic calls callbacks; display layer implements them (terminal or file).
- Callbacks must be **mockable** (QA tests inject fake callbacks).
- No blocking in callbacks (progress must not stall scan).
- Progress must not print when `non-TTY` (use callback no-op or file-only).

**Go/no-go:** core_developer must define exact callback signatures before any UI code is written. QA will test against mock callbacks.

---

## 5. Smart Reports (report.md / .csv)

- Schema: defined columns (path, size, type, offset, hash, timestamp, verdict).
- Strict validation: `selftest.py` validates schema on synthetic fixtures.
- Arabic/English encoding: UTF-8, BOM optional, CRLF/LF per platform.
- Printable/shareable: no ANSI, no control chars, deterministic ordering.

---

## 6. Onboarding

- README updated: workflow, shortcuts, language (Arabic/English per user pref).
- First-run: detect bundled Python, show version, verify read-only media access.
- Help: `--help` per subcommand; consistent flag names.

---

## 7. Future GUI

- **Out of scope** for embedded Python (no new libs, no GUI toolkit).
- If a real GUI is needed: separate project (web or desktop), not in `fixformat` core.
- Document decision in README: CLI-only for now.

---

## 8. Acceptance Criteria (QA)

1. Display layer separated from scan logic (testable).
2. Inputs mockable (fake callbacks, fake stdin).
3. Strict schema validation for report.md/.csv.
4. Arabic/English encoding tests on synthetic fixtures only.
5. Progress callbacks defined before implementation.
6. No ANSI leak to files; auto-disable on non-TTY; respect `NO_COLOR`.
7. All tests in `selftest.py` on synthetic fixtures (no device writes).

---

## 9. Status

- **Study:** done (this doc).
- **Contracts:** defined (§1–8).
- **Implementation:** gated on core_developer callback contract + @eyad approval of these contracts.
- **Impl tasks (held):** t_c9e59c05 (theme engine), t_20771cc0 (progress indicators), t_5cfd5a33 (report formatting — done), t_811012b1 (qa tests), t_6fef9851 (docs).

**Next:** core_developer defines callback signatures → @eyad approves → unblock impl tasks.
