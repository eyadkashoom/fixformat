# Progress indicators — fixformat v6 (K5 §3.5 / 3.9, CLI_UX_SPEC.md)
# Stdlib only; non-blocking; respects TTY/non-TTY, NO_COLOR, terminal width,
# cancellation and completion states.

import sys, os, time, math

# ------------------------------------------------------------------
# Progress state contract (dict per CLI_UX_SPEC.md §3.5)
# ------------------------------------------------------------------
# Required fields: scanned_bytes, total_bytes, percent, hits, carved,
# rate_bytes_per_s, eta_s, phase
# Optional: status ("running", "cancelling", "complete", "interrupted")


def _term_width(stdout, default=80) -> int:
    try:
        return os.get_terminal_size(stdout.fileno()).columns
    except Exception:
        return default


def default_progress_renderer(stdout=None, color_enabled=None, tty=None):
    import sys, os
    import importlib.util
    # Ensure sibling display module importable (same directory as this file)
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    stdout = stdout if stdout is not None else sys.stdout
    if color_enabled is None:
        import display
        color_enabled = display.color_enabled(stdout=stdout)
    if tty is None:
        try:
            tty = stdout.isatty()
        except Exception:
            tty = False

    import display
    theme = display.resolve_theme(stdout=stdout) if color_enabled else {"enabled": False, "roles": {}}
    last_line_len = 0
    last_write_time = 0.0

    def render(state: dict) -> None:
        nonlocal last_line_len, last_write_time
        if stdout is None:
            return
        # Non-interactive: emit plain lines at reduced frequency, no redraw
        if not tty:
            # Only emit on state change or every ~5 seconds to avoid spam
            now = time.time()
            if now - last_write_time < 5.0 and state.get("status") not in ("complete", "interrupted", "cancelling"):
                return
            last_write_time = now
            phase = state.get("phase", "scan")
            pct = state.get("percent", 0)
            rate = state.get("rate_bytes_per_s", 0)
            eta = state.get("eta_s", 0)
            line = (
                f"[{phase}] {pct:.1f}% | scanned={state.get('scanned_bytes',0)} "
                f"total={state.get('total_bytes',0)} hits={state.get('hits',0)} "
                f"carved={state.get('carved',0)} rate={rate:.0f} B/s eta={eta:.0f}s"
            )
            # Always include status line so CI logs show progress
            stdout.write(line + "\n")
            stdout.flush()
            last_line_len = len(line)
            return

        # TTY interactive mode: single updating line (carriage return)
        phase = state.get("phase", "scan")
        status = state.get("status", "running")
        percent = state.get("percent", 0.0)
        scanned = state.get("scanned_bytes", 0)
        total = state.get("total_bytes", 1) or 1
        hits = state.get("hits", 0)
        carved = state.get("carved", 0)
        rate = state.get("rate_bytes_per_s", 0.0)
        eta = state.get("eta_s", 0.0)

        # Spinner / status glyph based on phase/status
        if status == "complete":
            glyph = "✓"
        elif status == "interrupted" or status == "cancelling":
            glyph = "⏹"
        else:
            # Simple rotating spinner based on percent for variety
            glyph = ["|", "/", "-", "\\"][int(percent / 25) % 4]

        # Build compact progress bar respecting terminal width.
        # Spec section 6: no block glyphs when color is disabled (plain
        # ASCII fallback for terminals that may not render them).
        width = max(40, _term_width(stdout, 80) - 40)
        filled = int(width * percent / 100)
        if color_enabled:
            bar = "█" * filled + "░" * (width - filled)
        else:
            bar = "#" * filled + "-" * (width - filled)

        # Color only when enabled; progress role from theme
        from display import style
        bar_text = bar
        if color_enabled:
            bar_text = style("progress", bar_text, theme)
        else:
            bar_text = bar

        line = (
            f"{glyph} {phase:10s} [{bar_text}] {percent:5.1f}% "
            f"scanned={scanned} total={total} hits={hits} carved={carved} "
            f"rate={rate:.0f}B/s eta={eta:.0f}s status={status}"
        )
        # Truncate to terminal width without breaking escape codes.
        # Guarantee fit: truncate the plain text to max_len first, then
        # re-apply styling. Keep the trailing status fields (they carry
        # the machine-readable state) and cut the middle instead.
        max_len = _term_width(stdout, 120)
        import re
        plain_line = re.sub(r"\033\[[0-9;]*m", "", line)
        if len(plain_line) > max_len and max_len > 20:
            keep = max_len - 3
            head_len = keep // 2
            tail_len = keep - head_len
            plain_line = (plain_line[:head_len] + "..."
                          + plain_line[len(plain_line) - tail_len:])
            if color_enabled:
                # Re-apply bar styling on the (possibly truncated) bar.
                line = plain_line.replace(bar, style("progress", bar, theme), 1)
            else:
                line = plain_line
        last_line_len = len(re.sub(r"\033\[[0-9;]*m", "", line))

        # In-place redraw with carriage return; newline only on complete/interrupted
        if status in ("complete", "interrupted", "cancelling"):
            stdout.write("\r" + " " * last_line_len + "\r" + line + "\n")
        else:
            stdout.write("\r" + line)
        stdout.flush()
        last_line_len = len(re.sub(r"\033\[[0-9;]*m", "", line))

    return render


# ------------------------------------------------------------------
# Convenience helpers matching CLI_UX_SPEC.md §3.5 / 3.9
# ------------------------------------------------------------------

def ProgressCallback(state: dict, stdout=None, color_enabled=None, tty=None):
    """Fire-and-forget progress event handler (non-blocking)."""
    renderer = default_progress_renderer(stdout=stdout, color_enabled=color_enabled, tty=tty)
    renderer(state)
