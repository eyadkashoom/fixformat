#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""version.py - the single source of the fixformat version string.

Why this file exists
--------------------
The number used to be written as a literal in six modules plus three test
assertions plus the GUI banners: sixteen files, and a bump that missed one of
them either failed CI or shipped two different versions. Nothing caught a *wrong*
number, because every assertion compared the code to the code.

Now the six modules import this string. A bump edits one line. What still has to
be true is checked against something the code does not generate:

  * every module reports this same string (internal consistency), and
  * on a tagged build, the git tag and the newest `## [vX.Y.Z]` heading in
    CHANGELOG.md both equal it (external agreement) - see scripts/check_version.py.

Rule: never hardcode the version anywhere else. If a module needs it, import it.
"""

VERSION = "7.0.3"

# Convenience alias for tooling that expects PEP 396 style.
__version__ = VERSION

__all__ = ["VERSION", "__version__"]
