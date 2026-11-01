# CLI theme engine — fixformat v6
# Stdlib only; no external dependencies.
# Contracts: K5 display (§2.7, §3.7) + K4 ANSI (§2.1, §3.12, §6)

import os
import sys

# ------------------------------------------------------------------
# Theme resolution (precedence from CLI_UX_SPEC.md §6)
# ------------------------------------------------------------------

_BUILTIN_THEMES = {
    "default": {"success": 32, "error": 31, "warn": 33, "info": 34, "title": 34, "prompt": 36, "progress": 37},
    "plain":  {},
    "high-contrast": {"success": 92, "error": 91, "warn": 93, "info": 94, "title": 96, "prompt": 95, "progress": 97},
}


def color_enabled(stdout=None, env=None) -> bool:
    stdout = stdout if stdout is not None else sys.stdout
    env = os.environ if env is None else env
    # 1. NO_COLOR wins (any non-empty value)
    if env.get("NO_COLOR"):
        return False
    # 2. Non-TTY disables
    try:
        is_tty = stdout.isatty()
    except Exception:
        is_tty = False
    if not is_tty:
        # Override: FORCE_COLOR / CLICOLOR_FORCE still allowed
        if env.get("FORCE_COLOR"):
            return True
        if env.get("CLICOLOR_FORCE") == "1":
            return True
        return False
    # 3. TTY: check disable signals
    if env.get("TERM") == "dumb" or not env.get("TERM"):
        return False
    if env.get("CLICOLOR") == "0":
        return False
    # Enable overrides (only when not already disabled above)
    if env.get("FORCE_COLOR"):
        return True
    if env.get("CLICOLOR_FORCE") == "1":
        return True
    # Theme override
    theme_name = env.get("FIXFORMAT_THEME", "default")
    if theme_name == "plain":
        return False
    return True


def resolve_theme(env=None, stdout=None):
    env = os.environ if env is None else env
    stdout = stdout if stdout is not None else sys.stdout
    enabled = color_enabled(stdout=stdout, env=env)
    theme_name = env.get("FIXFORMAT_THEME", "default")
    if theme_name not in _BUILTIN_THEMES:
        theme_name = "default"
    roles = dict(_BUILTIN_THEMES.get(theme_name, _BUILTIN_THEMES["default"])) if enabled else {}
    return {"name": theme_name, "enabled": enabled, "roles": roles}


# ------------------------------------------------------------------
# Style / color helpers (K4 + K5)
# ------------------------------------------------------------------

_RESET = "\033[0m"


def _code(role: str, theme: dict) -> str:
    code = theme.get("roles", {}).get(role)
    return f"\033[{code}m" if code is not None else ""


def style(role: str, text: str, theme=None) -> str:
    theme = theme or resolve_theme()
    if not theme.get("enabled"):
        return text
    code = theme.get("roles", {}).get(role)
    if code is None:
        return text
    return f"\033[{code}m{text}{_RESET}"


def green(text, theme=None): return style("success", text, theme)
def red(text, theme=None): return style("error", text, theme)
def yellow(text, theme=None): return style("warn", text, theme)
def blue(text, theme=None): return style("info", text, theme)


def title(text, theme=None): return style("title", f"=== {text} ===", theme)
def question(text, theme=None): return style("prompt", f"? {text}", theme)
def error(text, theme=None): return style("error", f"✗ {text}", theme)
def success(text, theme=None): return style("success", f"✓ {text}", theme)
def warn(text, theme=None): return style("warn", f"! {text}", theme)
def info(text, theme=None): return style("info", text, theme)


# ------------------------------------------------------------------
# Safe rendering: never leaks ANSI into file-oriented paths
# ------------------------------------------------------------------

def safe_render(text: str, for_file: bool = False, theme=None) -> str:
    theme = theme or resolve_theme()
    if for_file or not theme.get("enabled"):
        # Strips any ANSI codes just in case
        import re
        return re.sub(r"\033\[[0-9;]*m", "", text)
    return text
