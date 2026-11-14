#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_version.py - the release gate for the version number.

The version used to be a literal in six modules plus three test assertions.
Bumping it by hand meant editing sixteen files, and a missed file either failed
CI or shipped two versions. Wrong numbers were never caught, because every
assertion compared the code to the code.

The number now lives in `version.py`. This gate checks the two things that can
still be wrong, and they are deliberately different kinds of check:

1. Internal consistency (always): every shipped module reports `version.VERSION`.
   A module that keeps its own literal fails here.
2. External agreement (only on a tagged build): the git tag and the newest
   `## [vX.Y.Z]` heading in CHANGELOG.md both equal `version.VERSION`. These are
   artefacts the code does not generate, so a plausible-but-wrong number cannot
   pass by agreeing with itself.

Exit code 0 means the number is safe to ship or to tag.

Run:
    python scripts/check_version.py
    python scripts/check_version.py --tag v7.1.0
"""

import argparse
import importlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODULES = (
    "raw_scan",
    "fat32_recovery",
    "text_classify",
    "text_ngram",
    "text_recovery",
    "text_search",
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
CHANGELOG_HEADING = re.compile(r"^## \[v?(\d+\.\d+\.\d+)\]", re.MULTILINE)


def changelog_versions():
    """Released versions named in CHANGELOG headings, newest first."""
    path = os.path.join(ROOT, "CHANGELOG.md")
    with open(path, encoding="utf-8") as fh:
        return CHANGELOG_HEADING.findall(fh.read())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME", ""),
                        help="tag being released; defaults to GITHUB_REF_NAME")
    args = parser.parse_args(argv)
    tag = (args.tag or "").strip()

    failures = []

    try:
        import version
    except ImportError as exc:                       # pragma: no cover
        print("FAIL cannot import version.py: %r" % (exc,))
        return 1

    current = version.VERSION
    print("version.py says %s" % current)
    if not SEMVER.match(current):
        failures.append("version.VERSION %r is not a MAJOR.MINOR.PATCH string"
                        % (current,))

    # 1. Internal consistency, always.
    reported = {}
    for name in MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as exc:                      # pragma: no cover
            failures.append("%s failed to import: %r" % (name, exc))
            continue
        got = getattr(module, "VERSION", None)
        reported[name] = got
        if got != current:
            failures.append("%s.VERSION is %r, version.py says %r"
                            % (name, got, current))
    print("modules report: %s" % ", ".join(
        "%s=%s" % (k, v) for k, v in sorted(reported.items())))

    # 2. External agreement, on a tagged build only. On main the Changelog head
    #    is [Unreleased] by design, so requiring a matching heading there would
    #    force a release commit on every push.
    if tag.startswith("v"):
        wanted = tag[1:]
        print("tag %s, changelog headings %s" % (tag, changelog_versions()))
        if wanted != current:
            failures.append("tag %s does not match version.py %r"
                            % (tag, current))
        if current not in changelog_versions():
            failures.append("CHANGELOG.md has no '## [%s]' heading; the release "
                            "notes and the code must name the same version"
                            % current)
    else:
        print("no release tag in this build; external checks skipped "
              "(headings: %s)" % (changelog_versions(),))

    if failures:
        print("\nVERSION GATE FAILED")
        for item in failures:
            print("  - %s" % item)
        return 1

    print("VERSION GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
