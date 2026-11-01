#!/usr/bin/env python3
"""Automated tests for the v6 CLI display layer (task t_811012b1).

Covers the three display-layer implementations with synthetic fixtures only:
  v6/display.py             - theme selection + fallback, color/style helpers,
                              safe rendering, terminal edge cases
  v6/progress_indicators.py - progress event handling (TTY redraw vs non-TTY
                              plain lines, states, throttling, width handling)
  v6/k5_report.py           - report rendering (md/csv/json), strict schema
                              validation, contract fixtures, determinism
Plus cross-cutting regression checks: no ANSI leaks into file-oriented
output, byte-identical determinism, stdlib-only imports, Arabic/UTF-8
round-trip.

Run:  python test_display_layer.py   (from the v6/ directory)
Exit 0 iff every check passes. No production source is modified.
"""

import csv
import io
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import display  # noqa: E402
import k5_report  # noqa: E402
import progress_indicators  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PASS_COUNT = 0


def check(cond, label):
    global PASS_COUNT
    if not cond:
        raise AssertionError(label)
    PASS_COUNT += 1


class FakeTTY(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        raise OSError("no fd")


class FakePipe(io.StringIO):
    def isatty(self):
        return False

    def fileno(self):
        raise OSError("no fd")


class BrokenStdout(io.StringIO):
    def isatty(self):
        raise RuntimeError("isatty boom")

    def fileno(self):
        raise OSError("no fd")


BASE_ENV = {"TERM": "xterm"}


def env(**over):
    e = dict(BASE_ENV)
    e.update(over)
    return e


FULL_STATE = {"scanned_bytes": 512, "total_bytes": 1024, "percent": 50.0,
              "hits": 2, "carved": 1, "rate_bytes_per_s": 2048.0,
              "eta_s": 1.0, "phase": "scan", "status": "running"}

FIXTURE = [
    {"offset": 1024, "length": 8192, "label": "JPEG", "ext": ".jpg",
     "sha256": "a" * 64, "validated": 1, "confidence": 0.92,
     "fragments": 1, "gap_bytes": 0, "name_guess": "img_01"},
    {"offset": 2048, "length": 4096, "label": "PNG", "ext": ".png",
     "sha256": "b" * 64, "validated": 0, "confidence": 0.55,
     "fragments": 2, "gap_bytes": 512, "name_guess": "unknown"},
]


# ------------------------------------------------------------------
# 1. Theme selection and fallback (display.color_enabled/resolve_theme)
# ------------------------------------------------------------------
def test_theme_selection():
    check(display.color_enabled(stdout=FakeTTY(), env=env()) is True,
          "tty+xterm enables color")
    check(display.color_enabled(stdout=FakePipe(), env=env()) is False,
          "non-tty disables color")
    check(display.color_enabled(stdout=FakeTTY(), env=env(NO_COLOR="1")) is False,
          "NO_COLOR wins on tty")
    check(display.color_enabled(stdout=FakePipe(),
                                env=env(NO_COLOR="1", FORCE_COLOR="1")) is False,
          "NO_COLOR beats FORCE_COLOR on pipe")
    check(display.color_enabled(stdout=FakePipe(), env=env(FORCE_COLOR="1")) is True,
          "FORCE_COLOR enables on non-tty")
    check(display.color_enabled(stdout=FakePipe(), env=env(CLICOLOR_FORCE="1")) is True,
          "CLICOLOR_FORCE=1 enables on non-tty")
    check(display.color_enabled(stdout=FakeTTY(), env=env(NO_COLOR="")) is True,
          "empty NO_COLOR is unset per no-color.org")
    check(display.color_enabled(stdout=FakeTTY(), env=env(TERM="dumb")) is False,
          "TERM=dumb disables on tty")
    check(display.color_enabled(stdout=FakeTTY(), env={}) is False,
          "missing TERM disables on tty")
    check(display.color_enabled(stdout=FakeTTY(), env=env(CLICOLOR="0")) is False,
          "CLICOLOR=0 disables on tty")
    check(display.color_enabled(stdout=FakeTTY(), env=env(FIXFORMAT_THEME="plain")) is False,
          "FIXFORMAT_THEME=plain disables on tty")
    check(display.color_enabled(stdout=BrokenStdout(), env=env()) is False,
          "isatty() raising disables instead of propagating")


def test_theme_names():
    th = display.resolve_theme(env=env(), stdout=FakeTTY())
    check(th["name"] == "default" and th["enabled"] is True,
          "default theme resolves enabled on tty")
    check(set(("success", "error", "warn", "info", "title", "prompt",
               "progress")) <= set(th["roles"]),
          "default theme exposes all seven roles")
    th_plain = display.resolve_theme(env=env(FIXFORMAT_THEME="plain"), stdout=FakeTTY())
    check(th_plain["name"] == "plain" and th_plain["enabled"] is False
          and th_plain["roles"] == {}, "plain theme is colorless")
    th_hc = display.resolve_theme(env=env(FIXFORMAT_THEME="high-contrast"),
                                  stdout=FakeTTY())
    check(th_hc["name"] == "high-contrast" and th_hc["enabled"] is True,
          "high-contrast resolves enabled on tty")
    check(th_hc["roles"]["success"] == 92 and th_hc["roles"]["error"] == 91,
          "high-contrast uses bright codes")
    th_bogus = display.resolve_theme(env=env(FIXFORMAT_THEME="neon"), stdout=FakeTTY())
    check(th_bogus["name"] == "default", "unknown theme name falls back to default")
    th_off = display.resolve_theme(env=env(NO_COLOR="1"), stdout=FakeTTY())
    check(th_off["enabled"] is False and th_off["roles"] == {},
          "disabled theme carries no roles")
    # determinism: same (env, tty) -> identical resolution
    again = display.resolve_theme(env=env(), stdout=FakeTTY())
    check(again == th, "theme resolution is deterministic")


def test_style_helpers():
    th = display.resolve_theme(env=env(), stdout=FakeTTY())
    check(display.green("t", theme=th) == "\x1b[32mt\x1b[0m", "green wraps code 32")
    check(display.red("t", theme=th) == "\x1b[31mt\x1b[0m", "red wraps code 31")
    check(display.yellow("t", theme=th) == "\x1b[33mt\x1b[0m", "yellow wraps code 33")
    check(display.blue("t", theme=th) == "\x1b[34mt\x1b[0m", "blue wraps code 34")
    check(display.title("t", theme=th) == "\x1b[34m=== t ===\x1b[0m", "title decorates")
    check(display.question("t", theme=th) == "\x1b[36m? t\x1b[0m", "question decorates")
    check(display.error("t", theme=th).startswith("\x1b[31m")
          and display.error("t", theme=th).endswith("\x1b[0m"),
          "error wraps code 31 with reset")
    check("\u2717 t" in display.error("t", theme=th), "error prefixes glyph")
    check("\u2713 t" in display.success("t", theme=th), "success prefixes glyph")
    check("! t" in display.warn("t", theme=th), "warn prefixes bang")
    check(display.style("nope", "t", theme=th) == "t", "unknown role returns text verbatim")
    check(display.style("success", "", theme=th) == "\x1b[32m\x1b[0m",
          "empty text still wraps (no crash)")
    off = display.resolve_theme(env=env(NO_COLOR="1"), stdout=FakeTTY())
    for fn in (display.green, display.red, display.yellow, display.blue,
               display.title, display.question, display.error,
               display.success, display.warn, display.info):
        check("\x1b[" not in fn("t", theme=off), "disabled helper emits no ANSI")
    check(display.style("success", "t", theme=off) == "t",
          "disabled style() returns input verbatim")


def test_safe_render():
    th = display.resolve_theme(env=env(), stdout=FakeTTY())
    off = display.resolve_theme(env=env(NO_COLOR="1"), stdout=FakeTTY())
    dirty = "\x1b[32mgreen\x1b[0m plain"
    check(display.safe_render(dirty, for_file=True, theme=th) == "green plain",
          "safe_render strips ANSI for file paths")
    check(display.safe_render(dirty, for_file=False, theme=off) == "green plain",
          "safe_render strips ANSI when theme disabled")
    check(display.safe_render(dirty, for_file=False, theme=th) == dirty,
          "safe_render passes through on enabled tty")
    check(display.safe_render("", for_file=True, theme=th) == "",
          "safe_render handles empty string")


# ------------------------------------------------------------------
# 2. Progress event handling (progress_indicators)
# ------------------------------------------------------------------
def test_progress_nontty_lines():
    out = FakePipe()
    render = progress_indicators.default_progress_renderer(
        stdout=out, color_enabled=False, tty=False)
    render(dict(FULL_STATE))
    text = out.getvalue()
    check(text.endswith("\n") and "\r" not in text,
          "non-tty emits plain newline-terminated line")
    check("[scan]" in text and "50.0%" in text and "scanned=512" in text
          and "total=1024" in text and "hits=2" in text and "carved=1" in text,
          "non-tty line carries contract fields")
    check("\x1b[" not in text, "non-tty line has no ANSI when color disabled")


def test_progress_nontty_throttle_and_terminal_states():
    out = FakePipe()
    render = progress_indicators.default_progress_renderer(
        stdout=out, color_enabled=False, tty=False)
    render(dict(FULL_STATE))
    first = out.getvalue()
    render(dict(FULL_STATE))  # immediate repeat: throttled
    check(out.getvalue() == first, "non-tty throttles rapid running updates")
    render(dict(FULL_STATE, status="complete"))
    check(out.getvalue().count("\n") == 2,
          "complete bypasses throttle so CI logs show completion")
    out2 = FakePipe()
    r2 = progress_indicators.default_progress_renderer(
        stdout=out2, color_enabled=False, tty=False)
    r2(dict(FULL_STATE, status="cancelling"))
    r2(dict(FULL_STATE, status="interrupted"))
    check(out2.getvalue().count("\n") == 2,
          "cancelling/interrupted always emitted on non-tty")


def test_progress_tty_redraw():
    out = FakeTTY()
    render = progress_indicators.default_progress_renderer(
        stdout=out, color_enabled=False, tty=True)
    render(dict(FULL_STATE))
    text = out.getvalue()
    check(text.startswith("\r") and "\n" not in text,
          "tty running update redraws in place without newline")
    check("status=running" in text,
          "tty line preserves trailing status after truncation")
    check("50.0%" in text or "..." in text,
          "tty line carries percent or a width-truncation marker")
    render(dict(FULL_STATE, status="complete"))
    check(out.getvalue().endswith("\n"),
          "tty complete terminates with newline")
    check("\u2713" in out.getvalue(), "tty complete shows check glyph")


def test_progress_tty_status_glyphs():
    for status, glyph in (("cancelling", "\u23f9"), ("interrupted", "\u23f9")):
        out = FakeTTY()
        render = progress_indicators.default_progress_renderer(
            stdout=out, color_enabled=False, tty=True)
        render(dict(FULL_STATE, status=status))
        check(glyph in out.getvalue(),
              "tty %s shows stop glyph" % status)
        check(out.getvalue().endswith("\n"),
              "tty %s terminates with newline" % status)
    for pct in (0.0, 25.0, 50.0, 75.0, 100.0):
        out = FakeTTY()
        render = progress_indicators.default_progress_renderer(
            stdout=out, color_enabled=False, tty=True)
        render(dict(FULL_STATE, percent=pct, status="complete"))
        check("status=complete" in out.getvalue(),
              "tty complete at %g%% renders" % pct)


def test_progress_color_and_width():
    out = FakeTTY()
    render = progress_indicators.default_progress_renderer(
        stdout=out, color_enabled=True, tty=True)
    render(dict(FULL_STATE))
    check("\x1b[37m" in out.getvalue(),
          "tty color-enabled styles the progress bar")
    out2 = FakeTTY()
    r2 = progress_indicators.default_progress_renderer(
        stdout=out2, color_enabled=False, tty=True)
    r2(dict(FULL_STATE))
    check("\x1b[" not in out2.getvalue(),
          "tty color-disabled emits no ANSI")
    plain = ANSI_RE.sub("", out.getvalue()).lstrip("\r")
    check(len(plain) <= 120, "tty line truncated to terminal width")


def test_progress_minimal_and_fire_and_forget():
    out = FakePipe()
    render = progress_indicators.default_progress_renderer(
        stdout=out, color_enabled=False, tty=False)
    render({})
    check("[scan] 0.0%" in out.getvalue(),
          "empty state dict uses documented defaults")
    out2 = FakePipe()
    progress_indicators.ProgressCallback(dict(FULL_STATE, status="complete"),
                                         stdout=out2, color_enabled=False, tty=False)
    check("50.0%" in out2.getvalue(),
          "ProgressCallback fire-and-forget emits")
    # renderer factory never blocks on odd stdout
    render_none = progress_indicators.default_progress_renderer(
        stdout=FakePipe(), color_enabled=False, tty=False)
    render_none(dict(FULL_STATE))
    check(True, "renderer factory returns without blocking")


# ------------------------------------------------------------------
# 3. Report rendering + strict schema (k5_report)
# ------------------------------------------------------------------
def test_report_markdown():
    md = k5_report.export(FIXTURE, "md")
    check("# K5 Report Summary" in md, "md has summary header")
    check("- Candidates: 2" in md and "- Validated: 1" in md,
          "md counts candidates and validated")
    check(".jpg=1" in md and ".png=1" in md, "md breaks down by extension")
    check("| offset | length | label | ext | validated | confidence |" in md,
          "md table header pins column order")
    check("| 1024 | 8192 | JPEG | .jpg | 1 | 0.92 |" in md,
          "md table row pins values")
    check("\x1b[" not in md, "md output is plain (no ANSI)")


def test_report_csv():
    text = k5_report.export(FIXTURE, "csv")
    rows = list(csv.reader(io.StringIO(text)))
    check(rows[0] == ["offset", "length", "label", "ext", "sha256", "validated",
                      "confidence", "fragments", "gap_bytes", "name_guess"],
          "csv header pins schema column order")
    check(rows[1][0] == "1024" and rows[1][2] == "JPEG" and rows[2][2] == "PNG",
          "csv rows carry fixture values")
    check("\x1b[" not in text, "csv output is plain (no ANSI)")
    tricky = [dict(FIXTURE[0], label='a,b "q"', name_guess="x|y")]
    rt = list(csv.reader(io.StringIO(k5_report.export(tricky, "csv"))))[1]
    check(rt[2] == 'a,b "q"' and rt[9] == "x|y",
          "csv quoting round-trips commas/quotes/pipes")
    check(text.endswith("\n"), "csv ends with newline")


def test_report_json():
    payload = json.loads(k5_report.export(FIXTURE, "json"))
    check(payload["summary"]["total"] == 2, "json summary total")
    check(len(payload["rows"]) == 2, "json rows length")
    check(payload["rows"][0]["offset"] == 1024, "json row value")
    check("\x1b[" not in k5_report.export(FIXTURE, "json"),
          "json output is plain (no ANSI)")


def test_report_empty_and_errors():
    md = k5_report.export([], "md")
    check("Candidates: 0" in md and "No candidates." in md,
          "empty md reports zero with sentinel")
    check(list(csv.reader(io.StringIO(k5_report.export([], "csv")))) == [[
        "offset", "length", "label", "ext", "sha256", "validated",
        "confidence", "fragments", "gap_bytes", "name_guess"]],
        "empty csv is header-only")
    check(json.loads(k5_report.export([], "json")) == {
        "summary": {"total": 0}, "rows": []}, "empty json shape")
    try:
        k5_report.validate_row(dict(FIXTURE[0], bogus=1))
        check(False, "unknown key must raise")
    except k5_report.ReportSchemaError:
        check(True, "unknown key raises ReportSchemaError")
    try:
        k5_report.export(FIXTURE, "xml")
        check(False, "unsupported format must raise")
    except k5_report.ReportSchemaError:
        check(True, "unsupported format raises ReportSchemaError")


def test_report_contract_fixture_and_determinism():
    for fmt in ("md", "csv", "json"):
        check(k5_report.export(FIXTURE, fmt) == k5_report.export(FIXTURE, fmt),
              "%s export is byte-identical across runs" % fmt)
    arabic = [dict(FIXTURE[0], label="JPEG \u0627\u0644\u0645\u0634\u0631\u0648\u0639")]
    md = k5_report.export(arabic, "md")
    check(md.encode("utf-8").decode("utf-8") == md,
          "arabic label survives UTF-8 round-trip in md")
    check("\u0627\u0644\u0645\u0634\u0631\u0648\u0639" in md, "arabic label present in md")
    check(set(k5_report.SCHEMA_KEYS) == {"offset", "length", "label", "ext",
                                         "sha256", "validated", "confidence",
                                         "fragments", "gap_bytes", "name_guess"},
          "SCHEMA_KEYS pins the ten-column contract")


# ------------------------------------------------------------------
# 4. Terminal edge cases + regression behavior
# ------------------------------------------------------------------
def test_terminal_edge_cases():
    check(display.color_enabled(stdout=BrokenStdout(), env=env()) is False,
          "broken isatty disables (no propagate)")
    th = display.resolve_theme(env=env(), stdout=BrokenStdout())
    check(th["enabled"] is False, "broken isatty resolves disabled theme")
    th_none_term = display.resolve_theme(env={}, stdout=FakeTTY())
    check(th_none_term["enabled"] is False, "missing TERM disables theme")
    # narrow terminal: tty render must not raise and stays bounded
    out = FakeTTY()
    render = progress_indicators.default_progress_renderer(
        stdout=out, color_enabled=False, tty=True)
    render(dict(FULL_STATE, phase="a-very-long-phase-name" * 10))
    visible = ANSI_RE.sub("", out.getvalue()).lstrip("\r")
    check(len(visible) <= 120,
          "long phase truncated to terminal width")
    check("status=running" in visible,
          "truncation preserves trailing status fields")
    # zero-total edge: no ZeroDivisionError
    out2 = FakeTTY()
    r2 = progress_indicators.default_progress_renderer(
        stdout=out2, color_enabled=False, tty=True)
    r2({"scanned_bytes": 0, "total_bytes": 0, "percent": 0.0,
        "hits": 0, "carved": 0, "rate_bytes_per_s": 0.0,
        "eta_s": 0.0, "phase": "scan", "status": "running"})
    check("status=running" in out2.getvalue(),
          "zero totals render without error (status preserved)")


def test_no_ansi_leak_to_files():
    fd, path = tempfile.mkstemp(prefix="display_layer_", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(k5_report.export(FIXTURE, "md"))
            fh.write("\n")
            fh.write(k5_report.export(FIXTURE, "csv"))
        with open(path, "r", encoding="utf-8") as fh:
            check("\x1b[" not in fh.read(),
                  "report file carries no ANSI bytes")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    th = display.resolve_theme(env=env(), stdout=FakeTTY())
    check("\x1b[" not in display.safe_render(
        display.green("x", theme=th), for_file=True, theme=th),
        "themed text stripped before file write")


def test_stdlib_only_and_determinism():
    import ast
    for mod_file in ("display.py", "progress_indicators.py", "k5_report.py"):
        with open(os.path.join(HERE, mod_file), "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        allowed = {"os", "sys", "re", "io", "csv", "json", "time", "math",
                   "importlib", "__future__", "typing", "display"}
        check(imports <= allowed,
              "%s imports stdlib only (got %s)" % (mod_file, sorted(imports)))
    th = display.resolve_theme(env=env(), stdout=FakeTTY())
    check(display.green("x", theme=th) == display.green("x", theme=th),
          "style() deterministic for same input")


TESTS = [
    test_theme_selection,
    test_theme_names,
    test_style_helpers,
    test_safe_render,
    test_progress_nontty_lines,
    test_progress_nontty_throttle_and_terminal_states,
    test_progress_tty_redraw,
    test_progress_tty_status_glyphs,
    test_progress_color_and_width,
    test_progress_minimal_and_fire_and_forget,
    test_report_markdown,
    test_report_csv,
    test_report_json,
    test_report_empty_and_errors,
    test_report_contract_fixture_and_determinism,
    test_terminal_edge_cases,
    test_no_ansi_leak_to_files,
    test_stdlib_only_and_determinism,
]


def main():
    failures = []
    for fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failures.append("%s: %s" % (fn.__name__, exc))
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors
            failures.append("%s: UNEXPECTED %r" % (fn.__name__, exc))
    print("display-layer tests: %d checks passed, %d test(s) failed out of %d"
          % (PASS_COUNT, len(failures), len(TESTS)))
    for item in failures:
        print("  FAIL " + item)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
