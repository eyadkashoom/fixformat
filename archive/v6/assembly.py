#!/usr/bin/env python3
"""v6 fragmented assembly (step 7) — sequential + BGC behind validators.
Disjoint from data, UI/reporting, performance modules. Stdlib only."""


def assemble(fragments, gap_search_limit=4096):
    """Combine validated fragments into a final artifact.
    fragments: list of bytes (each validated by validators module).
    Returns (assembled_bytes, fragments_n, gap_bytes, confidence, error).
    Sequential continuation then BGC fallback for 2-fragment case.
    """
    r = {"assembled": b"", "fragments": len(fragments),
         "gap_bytes": 0, "confidence": 0.0, "error": ""}
    if not fragments:
        r["error"] = "no fragments provided for assembly"
        return r
    if len(fragments) == 1:
        r["assembled"] = fragments[0]
        r["confidence"] = 0.95
        return r
    if len(fragments) == 2:
        # BGC fallback: smallest gap whose concatenation validates (simulated)
        best_gap = 0
        best_conf = 0.0
        for gap in range(0, min(gap_search_limit, 256)):
            assembled = fragments[0] + b"\x00" * gap + fragments[1]
            # Accept minimal gap; real pipeline would re-validate assembled
            conf = max(0.0, 0.85 - gap * 0.003)
            if conf > best_conf:
                best_conf = conf
                best_gap = gap
        r["assembled"] = fragments[0] + b"\x00" * best_gap + fragments[1]
        r["gap_bytes"] = best_gap
        r["confidence"] = round(best_conf, 3)
        return r
    # Sequential assembly: concatenate in order (bounded)
    assembled = b"".join(fragments)
    r["assembled"] = assembled
    r["confidence"] = 0.82
    return r
