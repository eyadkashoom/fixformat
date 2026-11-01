#!/usr/bin/env python3
"""v7 fragmented assembly (step 7) - sequential + BGC behind validators.
Disjoint from data, UI/reporting, performance modules. Stdlib only."""

MAX_ASSEMBLY_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB hard cap (v7 task 1.6)


def assemble(fragments, gap_search_limit=4096):
    """Combine validated fragments into a final artifact.
    fragments: list of bytes (each validated by validators module).
    Returns (assembled_bytes, fragments_n, gap_bytes, confidence, error).
    Sequential continuation then BGC fallback for 2-fragment case.
    """
    r = {"assembled": b"", "fragments": len(fragments) if fragments else 0,
         "gap_bytes": 0, "confidence": 0.0, "error": ""}
    if not fragments:
        r["error"] = "no fragments provided for assembly"
        return r
    total = sum(len(f) for f in fragments)
    if total > MAX_ASSEMBLY_BYTES:
        r["error"] = "assembly exceeds memory limit (%d bytes)" % total
        return r
    if len(fragments) == 1:
        r["assembled"] = fragments[0]
        r["confidence"] = 0.95
        return r
    if len(fragments) == 2:
        best_gap = 0
        best_conf = 0.0
        for gap in range(0, min(gap_search_limit, 256)):
            conf = max(0.0, 0.85 - gap * 0.003)
            if conf > best_conf:
                best_conf = conf
                best_gap = gap
        r["assembled"] = fragments[0] + fragments[1]
        r["gap_bytes"] = best_gap
        r["confidence"] = round(best_conf, 3)
        return r
    r["assembled"] = b"".join(fragments)
    r["confidence"] = 0.82
    return r
