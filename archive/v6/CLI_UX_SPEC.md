# fixformat v6 — K5 CLI UX Spec & Display Contract Mapping

**Task:** t_e7dea4e6 (Audit K5 display contracts and define CLI UX spec)
**Author:** lead (orchestrator)
**Date:** 2026-10-29
**Status:** SPEC ONLY — no implementation code. Companion to `v6/VOLUME_GATE_GUIDE.md`,
`v6/RUN_DB_GUIDE.md`, `v6/VALIDATORS_GUIDE.md`.
**Companion contract source:** kanban t_8531ea0b (UI/UX study + contracts,
`docs/UI_UX_CONTRACTS.md`), t_22576698 (ANSI color contracts), t_3a0ca57d
(K4 ANSI verification report `K4_ANSI_CONTRACT_VERIFICATION.md`).

---

## 1. Purpose and scope

This document maps every **display-related interface, event, schema, and theming hook**
in the approved K5 contracts to concrete CLI behavior for `fixformat` v6. It lists the
modules and public APIs that must change, the fallback behavior for non-color terminals,
and the GUI items that are explicitly out of scope.

It contains **no implementation code**. Pseudo-signatures below are contractual
descriptions, not source. All changes must remain stdlib-only (embedded Python,
no pip, no new runtime dependencies) and must not alter scan/decision logic
(display is a presentation concern).

In scope:
- UX audit of `raw_scan.py` prompts (unify question/error/title formatting).
- Terminal formatting system (ANSI colors, themes, long-scan readability).
- Prefill / memory (last path, auto device/image detection, safe suggestions).
- Progress screens (counts, files, read rate; non-blocking during long ops).
- Smart reports (`report.md`/`.csv` printable/shareable with plan + timing).
- Onboarding (README workflow, shortcuts, Arabic/English per user preference).

Out of scope (Section 7): any graphical or browser-based UI.

---

## 2. Source contracts (K4 + K5)

### 2.1 K4 — ANSI / color helper contract (from t_22576698, UNAPPROVED formal spec)

Per `K4_ANSI_CONTRACT_VERIFICATION.md` (t_3a0ca57d, NO-GO for implementation until
K1 merges and a formal spec is ratified). Candidate contract:

- Auto-disable color when stdout is not a TTY.
- Respect `NO_COLOR` environment variable (disable when set, per no-color.org).
- No ANSI escape sequences may leak into plain-text artifacts: the text report
  (`RECOVERY REPORT` / `report.txt` via `write_report`), `.csv` outputs,
  `provenance.json` (`write_provenance` / `provenance_payload`), `run.db`,
  `index.html` (its own styling), and `audit.txt`.
- Color applied to **terminal output only**.
- Coverage in `selftest.py` on synthetic fixtures, both enabled and disabled modes.
- Colors: green = success, red = error, yellow = warning, blue = info.
- Helpers: `green()`, `red()`, `yellow()`, `blue()` in `raw_scan.py` and
  `volume_gate.py`.

Ambiguities still open (must be settled before K4 implementation — see Section 6):
helper signatures (`green(text)` vs `green(*args)`), reset code, TTY detection scope
(`FORCE_COLOR`, `TERM=dumb`, `CLICOLOR`), `NO_COLOR` non-empty semantics, stdout vs
stderr, import direction between `raw_scan` and `volume_gate`, HTML scope.

### 2.2 K5 — display contracts (from t_8531ea0b)

QA constraints (the contract):

1. **Display layer separated from scan logic** (testable).
2. **Defined inputs (mockable).**
3. **Strict schema validation for `report.md` / `.csv`.**
4. **Arabic/English encoding tests on synthetic fixtures.**
5. **Progress as callbacks** (core_developer must define before coding).

Study areas (the scope) are listed in Section 1.

---

## 3. Contract → CLI behavior mapping

Each row maps a contract item to concrete CLI behavior. "Module" is the file that
owns the behavior; "API" is the public function/callback name (proposed).

### 3.1 Display layer separated from scan logic (K5 #1)

- **Contract:** No `print()` or styling inside scan/decision code paths; the scan
  returns data and invokes callbacks; rendering lives in a display module.
- **CLI behavior:** `scan()`, `parallel_phase1()`, `carve_phase()`, `probe_source()`
  never print progress directly. They accept an optional `progress` callback and
  return structured results. A thin CLI shell renders them.
- **Module:** new `v6/display.py` (rendering) + existing `raw_scan.py` (scan core,
  stripped of direct prints where feasible), `volume_gate.py` (gate core).
- **API:** `display.render_progress(state)`, `display.render_verdict(probe)`,
  `display.render_report(summary)`.

### 3.2 Defined, mockable inputs (K5 #2)

- **Contract:** All user-facing I/O goes through injectable streams/contexts so
  tests can mock stdin/stdout/env/TTY without patching globals.
- **CLI behavior:** Prompt helpers accept `stdin`, `stdout`, `env` parameters with
  sensible defaults (`sys.stdin`, `sys.stdout`, `os.environ`). TTY detection is a
  function of the injected stdout, not a global.
- **Module:** `raw_scan.py` (prompt helpers), new `display.py`.
- **API:** `ask(prompt, *, stdin=None, stdout=None)`, `confirm(prompt, ...)`,
  `pick_image(*, stdin, stdout, env)`, `color_enabled(stdout, env)`.

### 3.3 Strict schema validation for report.md / .csv (K5 #3)

- **Contract:** Reports are generated from a validated schema; malformed rows are
  rejected before writing; deterministic output.
- **CLI behavior:** `write_report()` and `write_organized_index()` validate their
  payload against a report schema (field set, types, encoding) before emitting.
  `run_db.write_report()` already derives from the `ranked` view; the CLI layer
  adds a schema check. Export targets (stdout, file, clipboard-not-available) share
  one renderer.
- **Module:** `run_db.py` (`write_report`, `ranked`, `summary`), new `display.py`
  (schema validator + renderer), `raw_scan.py` (`write_report`, `print_report`).
- **API:** `report_schema.validate(payload) -> (ok, errors)`,
  `render_report_markdown(conn, run_id)`, `render_report_csv(conn, run_id)`,
  `write_report(path, payload)`.

### 3.4 Arabic/English encoding tests on synthetic fixtures (K5 #4)

- **Contract:** All text artifacts round-trip as UTF-8; synthetic fixtures include
  Arabic and English strings; tests assert byte-identical output.
- **CLI behavior:** Reports, CSV, provenance, and DB text fields are written with
  explicit `encoding="utf-8"` and `newline=""` for CSV. Prompt text supports both
  scripts. No locale-dependent formatting of numbers in artifacts (use fixed
  `fmt_bytes`/`fmt_int`).
- **Module:** `raw_scan.py` (file writes), `run_db.py` (text columns),
  `selftest.py` (new encoding cases).
- **API:** unchanged write functions, but with explicit encoding; new selftest
  cases `test_report_utf8_roundtrip`, `test_csv_utf8_roundtrip`.

### 3.5 Progress as callbacks (K5 #5)

- **Contract:** Long-running operations report progress via callbacks, never by
  blocking the operation or printing inline unconditionally.
- **CLI behavior:** `scan()`/`parallel_phase1()` call `progress(state)` where
  `state` is a dict with at least `scanned_bytes`, `total_bytes`, `percent`,
  `hits`, `rate_bytes_per_s`, `eta_s`, `phase`. The default callback renders a
  single updating line (respecting terminal width); in non-TTY/CI mode it emits
  periodic plain lines. Callers may pass `None` to silence.
- **Module:** `raw_scan.py` (scan core emits callbacks), new `display.py`
  (default renderer), `selftest.py`.
- **API:** `ProgressCallback = Callable[[dict], None]`;
  `scan(..., progress=None)`, `parallel_phase1(..., progress=None)`,
  `default_progress_renderer(stdout, color_enabled) -> ProgressCallback`.

### 3.6 UX audit of prompts (K5 study area 1)

- **Contract:** Unify question/error/title formatting across all prompts.
- **CLI behavior:** All prompts use a shared set of helpers: titled section
  headers (`== Title ==`), consistent question marker (`? `), error lines
  (red when color enabled, plain otherwise), success lines (green). No ad-hoc
  `print("====")` variants.
- **Module:** `raw_scan.py` (all `ask_*`/`pick_*`/`confirm_*`), new `display.py`.
- **API:** `display.title(text)`, `display.question(text)`, `display.error(text)`,
  `display.success(text)`, `display.warn(text)`, `display.info(text)`.

### 3.7 Terminal formatting system / themes (K5 study area 2)

- **Contract:** Named themes, color/style application, no-color fallback, env
  overrides, safe rendering of contract-defined messages.
- **CLI behavior:** A theme is a mapping of semantic roles (success/error/warn/
  info/title/prompt/progress) to style descriptors. Built-in themes: `default`,
  `plain` (no color, no decoration), `high-contrast`. Selection via
  `FIXFORMAT_THEME` env or `--theme` flag; `NO_COLOR` forces `plain` regardless.
  Theme is resolved once at startup into a `color_enabled` flag + style map.
- **Module:** new `display.py` (theme engine), `raw_scan.py` (consume theme),
  `volume_gate.py` (`explain()` uses theme).
- **API:** `Theme(name)`, `resolve_theme(env, stdout) -> Theme`,
  `style(role, text) -> str`, `color_enabled(stdout, env) -> bool`.

### 3.8 Prefill / memory (K5 study area 3)

- **Contract:** Remember last path, auto-detect device/image, safe suggestions;
  never suggest a path that is the source disk (safety).
- **CLI behavior:** `load_last_paths()`/`remember_path()` (already present) are
  the storage; a new `suggest_source()` enumerates candidate images/devices and
  filters out the source's own physical disk (`same_physical_disk`). Suggestions
  are offered, never auto-selected silently.
- **Module:** `raw_scan.py` (`load_last_paths`, `remember_path`, `pick_image`,
  `choose_source`, `same_physical_disk`), new `display.py` (render suggestions).
- **API:** `suggest_sources(env) -> list[str]`, `safe_suggest(path, source) -> bool`.

### 3.9 Progress screens (K5 study area 4)

- **Contract:** Counts, files, read rate; non-blocking during long ops; works in
  interactive and CI/non-TTY.
- **CLI behavior:** The progress callback renders: bytes scanned / total
  (percent), hit count, carved count, current rate, ETA. In TTY mode, a single
  in-place line (carriage return, no newline) updated at most once per second.
  In non-TTY mode, a plain line every N seconds or per slice. Cancellation
  (`P`/`Q`) is handled by the scan loop checking a flag; display never blocks.
- **Module:** `raw_scan.py` (scan loop + cancellation flag), `display.py`
  (renderer).
- **API:** `ProgressState` (dict contract), `render_progress(state, stdout,
  color_enabled, tty)`.

### 3.10 Smart reports (K5 study area 5)

- **Contract:** `report.md`/`.csv` printable/shareable with plan + timing; aligns
  with v6 Task 5 (plan screen).
- **CLI behavior:** `write_report()` emits markdown with sections: run identity,
  source/size, mode, signature set, scan stats, candidates (validated first),
  plan selection rule, timing, cap policy. CSV mirrors the `candidate` table
  (offset, length, label, ext, sha256, validated, confidence, fragments). Both
  deterministic given the same DB rows.
- **Module:** `run_db.py` (`write_report`, `ranked`, `summary`, `plan_extraction`),
  `raw_scan.py` (`write_report`, `print_report`), new `display.py` (schema).
- **API:** `render_report_markdown(conn, run_id) -> str`,
  `render_report_csv(conn, run_id) -> str`.

### 3.11 Onboarding (K5 study area 6)

- **Contract:** README workflow, shortcuts, Arabic/English per user preference.
- **CLI behavior:** `main()` prints a short workflow banner (source → scan mode →
  output) on first run; `--help` lists shortcuts (`P` pause, `Q` checkpoint).
  Language selection via `FIXFORMAT_LANG` env (`en`/`ar`) or auto from locale;
  all user-facing strings routed through a small message table (no hardcoded
  English in scan paths where avoidable).
- **Module:** `README.md` (docs), `raw_scan.py` (`main`, message table), new
  `display.py`.
- **API:** `messages(lang) -> dict`, `banner(stdout, lang)`.

### 3.12 K4 color subset (Section 2.1)

- **Contract:** color helpers, terminal only, no leak.
- **CLI behavior:** `green/red/yellow/blue` wrap text with ANSI codes when color
  is enabled, return input verbatim when disabled. Used only in terminal output
  paths; never in file writes. `volume_gate.explain()` uses them for the verdict
  message.
- **Module:** `raw_scan.py`, `volume_gate.py` (helpers defined in both or imported
  from a shared display module — import direction to be settled, see Section 6).
- **API:** `green(text)`, `red(text)`, `yellow(text)`, `blue(text)`,
  `color_enabled(stdout, env)`.

---

## 4. Module touch list

| Module | Change | Reason |
|---|---|---|
| `v6/raw_scan.py` | Strip direct `print()` from scan core where feasible; route through display helpers; add progress callback param to `scan()`/`parallel_phase1()`; add theme/color helpers; add prompt unification; add encoding-explicit writes; add message table + banner. | K5 #1, #2, #3, #4, #5, #6; K4 |
| `v6/volume_gate.py` | `explain()` uses theme/color helpers; `probe_source()` data stays machine-readable (no color in dicts). | K4; K5 #1 |
| `v6/run_db.py` | `write_report()` schema check; explicit UTF-8; `ranked`/`summary` consumed by report renderer. | K5 #3, #4 |
| `v6/selftest.py` | New cases: ANSI enabled/disabled, no-leak to artifacts, progress callback, report schema, UTF-8 round-trip (Arabic/English), non-TTY fallback, theme selection. | K5 #4, K4 |
| `v6/display.py` | **NEW.** Theme engine, color helpers, prompt formatting, progress renderer, report schema validator + renderers, message table, banner. Stdlib only. | K5 all |
| `v6/progress.py` | **NEW (optional).** Progress state dataclass + default renderer if kept separate from display. Prefer folding into `display.py` to limit module count; split only if `display.py` exceeds ~600 lines. | K5 #5 |
| `v6/docs/UI_UX_CONTRACTS.md` | Owned by t_8531ea0b (technical_writer_docs); this spec consumes it. | K5 source |
| `README.md` / `CHANGELOG.md` | Onboarding section, shortcut list, v6 display notes (per `docs_plan.md`). | K5 area 6 |
| `V6_PLAN.md` | Cross-reference this spec from Section 9/10. | tracking |

No changes to: `run_db` schema (additive only if needed), volume decision logic,
scan algorithms, or any third-party dependency.

---

## 5. Public API summary (proposed, not implemented)

All signatures are proposals for ratification; final names may change, but the
contract (inputs/outputs/fallback) must hold.

Display / theme (`display.py`):
- `color_enabled(stdout, env) -> bool`
- `resolve_theme(env, stdout) -> Theme`
- `Theme(name)` with roles: success, error, warn, info, title, prompt, progress
- `style(role, text) -> str`
- `green(text)`, `red(text)`, `yellow(text)`, `blue(text)` (K4)
- `title(text)`, `question(text)`, `error(text)`, `success(text)`,
  `warn(text)`, `info(text)`
- `suggest_sources(env) -> list[str]`, `safe_suggest(path, source) -> bool`
- `ProgressState` (dict contract: scanned_bytes, total_bytes, percent, hits,
  carved, rate_bytes_per_s, eta_s, phase)
- `default_progress_renderer(stdout, color_enabled, tty) -> ProgressCallback`
- `render_progress(state, stdout, color_enabled, tty)`
- `report_schema.validate(payload) -> (ok, list[str])`
- `render_report_markdown(conn, run_id) -> str`
- `render_report_csv(conn, run_id) -> str`
- `messages(lang) -> dict`, `banner(stdout, lang)`

Core integration points (signature changes, additive only):
- `scan(target, patterns, ..., progress=None)` 
- `parallel_phase1(..., progress=None)`
- `carve_phase(..., progress=None)`
- `probe_source(path, ...)` unchanged (data stays machine-readable)
- `explain(probe)` unchanged signature, themed output internally

---

## 6. Fallback behavior for non-color terminals

Precedence (highest to lowest) for disabling color:
1. `NO_COLOR` set to any non-empty value → disable (no-color.org).
2. stdout is not a TTY (`not stdout.isatty()`) → disable.
3. `TERM` is `dumb` or empty → disable.
4. `CLICOLOR` == `0` → disable.
5. `FIXFORMAT_THEME` == `plain` → disable.

Enabling overrides (only when not disabled by 1–3):
- `FORCE_COLOR` set and non-empty → enable even on non-TTY (for CI log capture
  where color is desired), unless `NO_COLOR` is set (which always wins).
- `CLICOLOR_FORCE` == `1` → enable.

When disabled:
- `green/red/yellow/blue` and `style()` return the input text verbatim — no
  escape bytes, no reset codes.
- `render_progress` uses plain ASCII (no spinner glyphs that may not render);
  in non-TTY mode emits one line per update instead of in-place redraw.
- All file writes (`write_report`, `write_provenance`, CSV, DB, `audit.txt`,
  `index.html`) are always plain regardless of terminal state — color never
  reaches disk.

Determinism: same `(input, env, tty state)` ⇒ byte-identical output. This is a
testable invariant in `selftest.py`.

---

## 7. Explicit out-of-scope GUI items

The following are **not** part of v6 and must not be implemented under this spec:

- Any graphical desktop application (Tkinter, Qt, wx, etc.).
- Any browser-based or web dashboard UI served by the tool.
- Electron/Tauri or any bundled webview.
- Mouse-driven or windowed interaction.
- New runtime dependencies or pip packages (embedded Python is stdlib only).
- Color in `index.html` beyond its existing CSS (HTML owns its own styling;
  ANSI must not be injected).
- Internationalization beyond the English/Arabic message table (no third language
  in v6).

A real GUI, if ever needed, is a **separate project** with its own dependency
budget and must consume the same display contracts (theme roles, progress callback,
report schema) defined here so the CLI and GUI stay consistent. GUI work is gated
on a developer report and is not scheduled in v6.

---

## 8. Open ambiguities to settle before implementation

Carried from K4 verification (Section 5 of `K4_ANSI_CONTRACT_VERIFICATION.md`) and
K5:

1. Helper signatures: `green(text)` vs `green(*args)`; reset code
   (`\x1b[0m` vs `\x1b[39m`).
2. TTY detection scope: `sys.stdout.isatty()` only, or also `FORCE_COLOR` /
   `TERM=dumb` / `CLICOLOR` (Section 6 proposes a resolution — ratify).
3. `NO_COLOR` semantics: any non-empty value disables (no-color.org) — ratify.
4. stdout vs stderr scope: do helpers apply to stderr too?
5. Import direction: do `raw_scan` and `volume_gate` duplicate helpers or share
   via `display.py`? (Recommend: single home in `display.py`, imported by both;
   avoids circular imports given K1 wiring.)
6. Whether color applies to `index.html` organized output (recommend: no — HTML
   owns styling).
7. Selftest synthetic fixture definitions and expected escape sequences.
8. Progress callback exact field set and frequency (K5 #5 — core_developer to
   define before coding).
9. Report schema field set for `report.md`/`.csv` (core_developer + qa to agree).
10. K1 (t_fae66517) must merge before any `raw_scan.py` edit to avoid conflicts.

---

## 9. Go / No-Go

**GO for documentation and spec work; GATED for implementation.**

- This spec (and the K5 contracts in t_8531ea0b) are sufficient to write the
  display layer design and tests. No blockers for doc.
- Implementation tasks remain gated:
  - t_c9e59c05 (CLI theme engine) — waits on this spec + K4 formal spec ratification.
  - t_20771cc0 (progress indicators) — waits on the progress callback contract
    (Section 3.5 / ambiguity 8).
  - t_5cfd5a33 (report formatting) — already completed; must be checked against
    the report schema in Section 3.10 / ambiguity 9 for compliance.
  - t_19432aaf / t_5ba81829 (ANSI helpers) — remain blocked per K4 NO-GO until
    K1 merges + K4 formal spec is ratified (ambiguities 1–7).
- No implementation code was produced by this task.

---

## 10. Deliverables of this task

- This document: `v6/CLI_UX_SPEC.md` (contract mapping + module touch list).
- No code changes.
- Downstream workers (core_developer, qa_automated_tester, technical_writer_docs)
  should treat Sections 3, 4, 5, 6 as the contract for implementation and tests.
