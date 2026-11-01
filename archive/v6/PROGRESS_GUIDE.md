# Progress — CLI — fixformat v6

**Author:** @technical_writer_docs
**Trigger:** Task 3 (CLI theming, progress, and reports).
**Companions:** `v6/progress_indicators.py`, `v6/display.py`, `v6/test_display_layer.py`.
**Status:** IMPLEMENTED — docs merged for v6.1.0.

---

## 1. Scope

This guide documents progress reporting in fixformat v6:

- The **progress state contract** (dict fields).
- The **progress renderer** (`default_progress_renderer`) and its TTY /
  non-TTY behavior.
- **Terminal states** (running, cancelling, complete, interrupted) and
  throttling.
- The **fire-and-forget** `ProgressCallback` helper.
- Examples and troubleshooting.

Scope is **text/terminal only**. There is no GUI in v6.

---

## 2. Progress state contract

`progress_indicators` expects a *state dict* with at least these **required**
fields:

| Field | Type | Meaning |
|---|---|---|
| `scanned_bytes` | int | Bytes scanned so far. |
| `total_bytes` | int | Bytes to scan. |
| `percent` | float | Completion percentage (0.0–100.0). |
| `hits` | int | Signature hits found so far. |
| `carved` | int | Carved (recovered) items so far. |
| `rate_bytes_per_s` | float | Current read rate (bytes/s). |
| `eta_s` | float | Estimated time remaining (seconds). |
| `phase` | str | Current phase name, e.g. `"scan"`. |

Optional field:

| Field | Values | Meaning |
|---|---|---|
| `status` | `"running"`, `"cancelling"`, `"complete"`, `"interrupted"` | Terminal state. Defaults to `"running"`. |

The renderer uses only its own defaults for missing fields, so an **empty**
state dict `{}` renders with documented defaults: `[scan] 0.0%`, `scanned=0`,
`total=0`, `hits=0`, `carved=0`, `rate=0 B/s`, `eta=0s`.

### 2.1 Zero-total safety

A state with `total_bytes == 0` (or `total` falsy) never raises a
`ZeroDivisionError`: the renderer treats `total_bytes` falsy as `1` and keeps
`percent` at its value, preserving the `status` field. This was verified by the
zero-total test.

---

## 3. The renderer

`default_progress_renderer(stdout=None, color_enabled=None, tty=None)` returns a
**render callback** `render(state) -> None` that is **non-blocking**. It never
waits on stdout or blocks the operation it serves.

- `stdout` — the stream to write to (defaults to `sys.stdout`).
- `color_enabled` — color flag; if `None`, resolved via `display.color_enabled()`.
- `tty` — whether stdout is a TTY; if `None`, detected via `stdout.isatty()`,
  failing closed to `False` when `isatty()` raises.

The renderer resolves the theme once (`display.resolve_theme`) when color is
enabled, and styles the progress bar with the `progress` role only.

---

## 4. TTY behavior (interactive)

On a TTY the renderer redraws a **single in-place line** using carriage return
(`\r`) — no newline during a running update:

```
| scan      [████░░░░░░░░]  50.0% scanned=512 total=1024 hits=2 carved=1 rate=2048B/s eta=1s status=running
```

- **Glyph** — a rotating spinner (`|`, `/`, `-`, `\`) chosen from
  `int(percent / 25) % 4` while `status` is `running`; `✓` on complete;
  `⏹` on `cancelling`/`interrupted`.
- **Bar** — `█`/`░` glyphs when color is enabled; `#`/`-` plain ASCII when
  disabled (plain ASCII fallback, no block glyphs when color is off).
- **Width** — the bar width is `max(40, terminal_width - 40)`; the whole line
  is truncated to the terminal width (default 120) by cutting the **middle**,
  preserving the trailing `status=` fields and the percent. Long phase names
  are truncated the same way.
- **Complete / interrupting** — a newline is appended and the line is flushed;
  `✓` (complete) or `⏹` (cancelling/interrupted) glyph.

Color is applied only when `color_enabled` is true; otherwise the bar is plain
ASCII and no ANSI bytes are emitted.

---

## 5. Non-TTY behavior (pipes, redirects, CI)

On a non-TTY the renderer emits **plain, newline-terminated lines** with no
ANSI and no in-place redraw:

```
[scan] 50.0% | scanned=512 total=1024 hits=2 carved=1 rate=2048 B/s eta=1s
```

Throttling: rapid successive updates (same status, less than ~5 seconds apart)
are **skipped** so CI logs are not spammed. The throttle is **bypassed** when
the state becomes `complete`, `interrupted`, or `cancelling`, so the end of a
run is always visible in logs.

---

## 6. Terminal states

| `status` | TTY glyph | Line ending | Throttle bypass |
|---|---|---|---|
| `running` | rotating spinner | no newline (in-place `\r`) | no |
| `cancelling` | `⏹` | newline | always emitted |
| `interrupted` | `⏹` | newline | always emitted |
| `complete` | `✓` | newline | always emitted |

The `complete`-bypass rule is what makes CI logs show completion even under a
tight update loop.

---

## 7. Fire-and-forget helper

`ProgressCallback(state, stdout=None, color_enabled=None, tty=None)` constructs a
renderer and immediately calls it with the given state. Use it as the callback
passed into a scan; it never blocks and never raises on a detached stdout.

---

## 8. Examples

```python
import progress_indicators

def my_callback(state):
    # call the default renderer; non-blocking, TTY-aware
    progress_indicators.default_progress_renderer(
        stdout=my_file, color_enabled=False, tty=False)(state)

# fire-and-forget (no explicit call needed)
progress_indicators.ProgressCallback({
    "scanned_bytes": 512, "total_bytes": 1024, "percent": 50.0,
    "hits": 2, "carved": 1, "rate_bytes_per_s": 2048.0,
    "eta_s": 1.0, "phase": "scan", "status": "running",
}, stdout=progress_file, color_enabled=False, tty=False)
```

```python
# non-TTY output (CI log): plain lines, throttled, complete always shown
render = progress_indicators.default_progress_renderer(
    stdout=log_file, color_enabled=False, tty=False)
render({"scanned_bytes": 0, "total_bytes": 1024, "percent": 0.0,
        "hits": 0, "carved": 0, "rate_bytes_per_s": 0.0,
        "eta_s": 0.0, "phase": "scan", "status": "running"})
# -> "[scan] 0.0% | scanned=0 total=1024 hits=0 carved=0 rate=0 B/s eta=0s"
```

### 8.1 ANSI-free progress

Use `color_enabled=False` (or a `NO_COLOR` env) and/or `tty=False` to guarantee
progress output carries no ANSI bytes. The negative control confirmed all
style helpers emit zero ANSI bytes under `NO_COLOR`.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No update lines in a redirected log | Throttle (≤5 s) + non-complete status | Complete the run or wait ≥5 s between updates; `complete`/`interrupted`/`cancelling` always flush. |
| `ZeroDivisionError` on a zero-size source | Early read path; renderer expects `total_bytes` | The renderer handles `total_bytes` falsy (treats as 1); if you hit it elsewhere, report the source. |
| Spinner/block glyphs wrong in your terminal | Non-TTY or color disabled → plain ASCII (`#`/`-`) | Ensure a TTY and color enabled for the block glyphs. |
| `✓`/`⏹` showing in a non-TTY log | Terminal-state lines always emit a newline and glyph | Expected: these states always flush; strip for plain-log tooling if needed. |
| ANSI bytes in a log file | Color enabled while not a TTY | Set `color_enabled=False` or `tty=False`, or `NO_COLOR=1`. |
| `isatty()` raised and progress went quiet | Detached stdout fails closed to `False` | Expected; color is disabled on a broken stdout. |
| Width-truncated line loses status | Long phase name cut the middle | Expected: trailing `status=` fields are preserved; if you need less truncation, widen the terminal. |

---

## 10. Acceptance

- [x] State contract fields and types pinned by fixture + tests.
- [x] Zero-total renders without division error.
- [x] Non-TTY: plain newline-terminated lines, throttled, complete bypassed throttle.
- [x] TTY: in-place `\r` redraw, terminal-width truncation preserves trailing status.
- [x] Terminal states (`cancelling`/`interrupted`/`complete`) always flush with glyphs.
- [x] `ProgressCallback` fire-and-forget works; renderer never blocks.
- [x] No ANSI bytes on non-TTY / color-disabled paths (negative control passed).
