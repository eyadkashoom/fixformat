# Golden fixtures

Reference inputs for the text-recovery layers. Committed so that any checkout
can be verified without trusting a generator run, and reproducible byte for
byte from a fixed seed.

Regenerate (overwrites, then commit the diff):

    python scripts/generate_fixtures.py

Verify the committed bytes and `MANIFEST.json` match the generator:

    python scripts/generate_fixtures.py --check
    python test_fixtures.py

Rules:

* every file is at most 64 KiB;
* generation depends only on `scripts/generate_fixtures.py` — no clock, no
  `/dev/urandom`, no locale, no directory ordering;
* changing a fixture requires bumping `GENERATOR_VERSION`, regenerating, and
  stating in the commit message which acceptance criteria moved.

Folders:

* `negative/` — must never be reported as text (false-positive probes).
* `positive/` — real text in one declared encoding; must be detected.
* `edge_cases/` — valid text at a boundary (BOMs, truncated runs, sparse runs).

`MANIFEST.json` records size, SHA-256, expected verdict and the reason each
file exists. Fixtures whose expected verdict the code does not yet meet are
marked with `known_gap`, so the gap is visible instead of implied.
