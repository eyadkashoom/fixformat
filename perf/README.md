# text_search performance contract

This folder holds the measurement tooling for `text_search.py`. The numbers below
are the acceptance target for the v7 text-search work; the CI gate
(`test_text_search_perf.py`) fails when they are missed by a wide margin.

## Target (acceptance criteria)

| workload | target | CI floor |
|---|---|---|
| runless input: random bytes, zero fill, compressed data | ≥ 10 MiB/s | ≥ 3 MiB/s |
| text-dense input: ASCII, Arabic UTF-8, UTF-16LE | ≥ 0.5 MiB/s | ≥ 0.2 MiB/s |
| scaling | 10× input ≤ 15× time | ≤ 25× |
| peak RSS growth, 1 MiB → 10 MiB text | ≤ 96 MiB | ≤ 128 MiB |

The CI floor is lower than the target because GitHub's `windows-latest` runner is
slower and noisier than the development host. It still fails on any structural
regression: the pre-optimisation implementation measured **0.006 MiB/s** on the
text corpora and **0.006 MiB/s** on zeros, i.e. 30–2000× below the floor.

Why the text target is 0.5 MiB/s and not 10: scoring is a pure-Python trigram
model, and one character costs ~1 µs on the development host (measured:
`for gram in zip(s, s[1:], s[2:])` with two dict lookups and `math.log`). A
1 MiB text run therefore costs ~1 s no matter how the windows are arranged. The
remaining speed-up is obtained by scoring each character once instead of
8×(overlap) × 15×(trim cuts) times, and by never decoding bytes that hold no
text. Beating the per-character cost would need a C extension, which is out of
scope for a stdlib-only tool.

## How to reproduce

```bash
# size ladder, deterministic corpora, throughput + peak RSS
python perf/bench_text_search.py                 # 1, 10, 100 MiB
python perf/bench_text_search.py --sizes 1024 --corpus noise   # 1 GiB noise

# before/after against a fixed git ref (prints the baseline SHA)
python perf/compare_refs.py --baseline v7.0.3

# the CI gate
python test_text_search_perf.py          # ~40 s at 10 MiB
python test_text_search_perf.py --quick  # ~8 s at 2 MiB
```

`compare_refs.py` exits non-zero if any corpus produces a different set of
regions (offset, length, encoding) than the baseline. Scores may differ by a few
hundredths: the optimised path takes the full-window reading where the old one
took the maximum over the 15 trim cuts.

## Recall contract

A planted text block counts as found when **a reported region covers it**
(`offset <= plant < offset + length`), not when a region starts within a fixed
distance of it. A 512-byte window that holds 384 bytes of the plant legitimately
scores above threshold, so a region may begin up to one window before the plant;
`offset` is the region start and `length` is how far it runs. The CI step runs
`bench_text_search.py --sizes 5 --corpus mixed` and fails on any uncovered plant.

A row's `encoding` always describes the bytes at its own `offset`, and its
`preview` is decoded with that same encoding (`errors="replace"`, cut at the
first NUL). A preview decoded with a different codec than the row advertises is a
bug: it reported UTF-16LE regions as `utf-8` with `"E\x00 \x00r\x00..."` previews.

## Design notes (what made it fast)

1. **Text-run prefilter** (`_text_runs`): one `bytes.translate` mask pass plus
   `bytes.find` (memmem) marks where text can live. Windows outside every run are
   never decoded. Runless input costs one scan instead of one decode+score per
   window.
2. **Full-window fast path** (`score_window(..., stop_at=)`): if the whole window
   already clears the threshold, return it instead of sweeping 15 trim lengths.
3. **Prefix-sum scoring** (`_RunScorer`): inside a run, a window score is
   `(pre[j-2] - pre[i]) / (n-2)` — O(1) — with a byte→character map for UTF-8 and
   a byte//2 map for UTF-16LE, so all three encodings take the same path.
4. **Bounded memory**: prefix arrays are built per 1 MiB chunk and the previous
   chunk is dropped as the scan advances. Peak RSS does not grow with input size.
   Measured 1 MiB → 10 MiB of ASCII text: 44.2 MiB → 67.2 MiB peak working set
   (23 MiB of growth for 9 MiB more text), and at 100–200 MiB the scan adds
   nothing to the peak already set by the input itself (`scan RSS 0.0 MiB` in
   `perf/bench_text_search.py --sizes 100`). Regions are merged as the scan
   advances, so the pending-region list is O(regions), not O(windows).
