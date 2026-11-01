# CLI Them­ing, Progress, and Reports — fixformat v6

**Author:** @technical_writer_docs
**Trigger:** Task 3 (CLI theming, progress, and reports).
**Companions:** `v6/display.py`, `v6/progress_indicators.py`, `v6/k5_report.py`,
`v6/test_display_layer.py` (111 checks / 18 groups, green).
**Status:** IMPLEMENTED — docs merged for v6.1.0.

---

## 1. Scope

This guide documents the CLI presentation layer of fixformat v6:

- **Theming** — color/theme selection, environment variables, style helpers,
  safe rendering (no ANSI in files).
- **Progress** — progress state contract, TTY redraw, non-TTY plain lines,
  throttling, terminal states.
- **Reports** — `md` / `csv` / `json` export, strict schema validation,
  export options, examples.

Scope is **text/terminal only**. There is no GUI in v6: no desktop window,
no browser dashboard, no bundled webview. See `v6/UI_UX_CONTRACTS.md` §7
(Explicit out-of-scope GUI items). The user preference is Arabic prose with
English technical terms in parentheses (e.g. raw-disk recovery, CHANGELOG).

---

## 2. Theming

### 2.1 Built-in themes

A theme maps *semantic roles* to ANSI color codes. Three built-in themes:

| Theme | Result |
|---|---|
| `default` | Color enabled (TTY, no `NO_COLOR`). Roles for all seven colors. |
| `plain` | Color **disabled**; `roles` is empty. No ANSI codes, no decorations. |
| `high-contrast` | Color enabled; uses bright (90–99) codes for readability. |

If `FIXFORMAT_THEME` names an unknown theme, resolution falls back to `default`
(those roles are used). If the theme is disabled, `roles` is an empty dict
(`{}`) and no style helper emits ANSI bytes.

### 2.2 Environment variables

| Variable | Effect | Notes |
|---|---|---|
| `FIXFORMAT_THEME` | Selects a built-in theme by name (`default` / `plain` / `high-contrast`). | Unknown names fall back to `default`. |
| `NO_COLOR` | Disables color when set to **any non-empty value**. | Per no-color.org. An empty `NO_COLOR` (`""`) is treated as **unset**. |
| `FORCE_COLOR` | Enables color **even on a non-TTY** (CI logs, pipes, redirects). | Overrides the non-TTY disable; lowest precedence; never beats `NO_COLOR`. |
| `CLICOLOR_FORCE` | Enables color even on a non-TTY, value `1`. | Same override role as `FORCE_COLOR`. |
| `CLICOLOR` | `0` disables color on a TTY. | Standard CLI convention. |
| `TERM` | `dumb` (or an empty `TERM`) disables color on a TTY. | e.g. `TERM=dumb` → plain. |

Precedence (highest first), from `display.color_enabled()`:

1. `NO_COLOR` non-empty → **disable** (always wins, even on a TTY).
2. stdout is not a TTY → disable **unless** `FORCE_COLOR` or `CLICOLOR_FORCE=1` overrides.
3. On a TTY: `TERM=dumb`/empty, or `CLICOLOR=0` → disable.
4. `FORCE_COLOR` / `CLICOLOR_FORCE=1` → enable (only after 1–3 did not disable).
5. `FIXFORMAT_THEME=plain` → disable.

`isatty()` raising (a broken/detached stdout) fails **closed**: color is
disabled and no exception propagates.

### 2.3 Theme resolution

`resolve_theme(env=None, stdout=None) -> dict` returns:

```json
{"name": "default", "enabled": true, "roles": {"success": 32, "error": 31,
  "warn": 33, "info": 34, "title": 34, "prompt": 36, "progress": 37}}
```

- `"name"` — the theme name actually used.
- `"enabled"` — `true` only when color is on.
- `"roles"` — role → ANSI code map; `{}` when disabled.

`color_enabled(stdout=None, env=None) -> bool` is the boolean gate used by
progress and report renderers.

### 2.4 Style helpers

All helpers wrap text in ANSI codes and reset after. When the theme is disabled
(or the role has no code) they return the **input text verbatim** (no escape
bytes):

| Helper | Role | Renders as (enabled) |
|---|---|---|
| `green(text)` | `success` | `✓ text` |
| `red(text)` | `error` | `✗ text` |
| `yellow(text)` | `warn` | `! text` |
| `blue(text)` | `info` | `text` |
| `title(text)` | `title` | `=== text ===` |
| `question(text)` | `prompt` | `? text` |
| `error(text)` | `error` | `✗ text` |
| `success(text)` | `success` | `✓ text` |
| `warn(text)` | `warn` | `! text` |
| `info(text)` | `info` | `text` |

`style(role, text, theme=None)` is the generic form: `style("success", "t")`
→ `✓ t`. Unknown roles return `text` verbatim.

### 2.5 Safe rendering

`safe_render(text, for_file=False, theme=None) -> str` strips ANSI escapes
whenever `for_file` is `True` **or** the theme is disabled. This guarantees no
ANSI leaks into file-oriented output (reports, CSV, provenance, HTML, DB text).
Use it for any text you are about to write to a file.

### 2.6 Examples

```bash
# Plain theme on a TTY (no colors at all)
FIXFORMAT_THEME=plain python v6/raw_scan.py

# High-contrast on a TTY for poor-vision terminals
FIXFORMAT_THEME=high-contrast python v6/raw_scan.py

# Force color into a CI log (pipe / redirected output)
FORCE_COLOR=1 python v6/raw_scan.py > scan.log

# Respect the no-color standard (wins even on a TTY)
NO_COLOR=1 python v6/raw_scan.py
```

```python
import display

th = display.resolve_theme(env={"FIXFORMAT_THEME": "default"},
                           stdout=open(os.devnull, "w"))
print(display.green("recovered", theme=th))      # green
print(display.title("Scan complete"))            # blue, decorated
print(display.safe_render(display.error("bad path"), for_file=True, theme=th))
# -> "✗ bad path"  (ANSI stripped before the file write)
```

### 2.7 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No color on a real terminal | `NO_COLOR` set | `unset NO_COLOR` |
| No color on a real terminal | `TERM=dumb` or `CLICOLOR=0` | `TERM=xterm` (or `FORCE_COLOR=1`) |
| Color in a redirected log | You need it: `FORCE_COLOR=1` | `FORCE_COLOR=1 python v6/raw_scan.py > scan.log` |
| Color leaking into a report file | A `for_file` path was styled directly | Use `display.safe_render(..., for_file=True)` |
| `FIXFORMAT_THEME=neon` silently uses default colors | Unknown theme falls back to `default` | Use a known theme name (`default` / `plain` / `high-contrast`) |
| `ColorEnabledError` / exception on startup | `isatty()` raised on detached stdout | Expected fail-closed behavior; color is disabled, no exception |

---

## 3. Acceptance

- [x] Theme names and fallback verified by `display_theme_selection` + `resolve_theme` tests.
- [x] Environment-variable precedence verified (NO_COLOR beats FORCE_COLOR; `isatty` failure fails closed).
- [x] Style helper ANSI codes pinned (32/31/33/34/36).
- [x] `safe_render(for_file=True)` strips ANSI.
- [x] Zero ANSI bytes under `NO_COLOR` (negative control passed).
