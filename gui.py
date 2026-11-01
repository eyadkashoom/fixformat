#!/usr/bin/env python3
"""fixformat v7.0.1 — GUI module shim.

The web server moved to gui_web.py and the markup to gui_web.html (Phase 0).
This module re-exports the public surface so existing callers (and selftest's
"old GUI still works as a module" check) keep working.
"""
from gui_web import *  # noqa: F401,F403
