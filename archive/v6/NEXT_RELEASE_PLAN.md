# Next Release Plan (fixformat + proposals)

**Prepared by:** lead (orchestrator)
**Status:** Draft for discussion with @eyad.
**Source:** Kanban board (default) + chat room (lead room) + memories.

---

## A. Completed in current release (v6)

| Item | Status | Evidence |
|---|---|---|
| v6 Task 1 — Wire volume gate → raw_scan.py pre-scan (hook verdict exposed) | ✅ Done | commit (worktree), syntax-verified |
| v6 Task 1 QA — assertions (plaintext/encrypted/size=None C:/D:/latency) | ✅ Done | qa_automated_tester |
| v6 Volume gate guide (VOLUME_GATE_GUIDE.md) folded into release notes | ✅ Done | technical_writer_docs |
| v6 Steps 2–3 — DB + 2 extraction modes | ✅ Done | core_developer |
| v6 Steps 4,5,12 — plan screen, reports, audit.txt | ✅ Done | core_developer |
| v6 Steps 6,7 — validators, fragmented assembly | ✅ Done | core_developer |
| v6 Steps 8–11 — adaptive ops, entropy, penalty, fast-scan calibration, Direct I/O | ✅ Done | core_developer |
| ANSI color contracts + helpers (non-TTY, NO_COLOR, no leak, selftest) | ✅ Done | core_developer + qa |
| ANSI docs fold (VOLUME_GATE_GUIDE §ANSI rules) | ✅ Done | technical_writer_docs |
| UI/UX study + contracts (v6/UI_UX_CONTRACTS.md) | ✅ Done | technical_writer_docs |
| Report formatting + export | ✅ Done | core_developer |
| QA tests (v6 bundle + ANSI) | ✅ Done | qa_automated_tester |
| Docs (v6 bundle + ANSI + UI/UX) | ✅ Done | technical_writer_docs |

---

## B. Proposed for next release (deferred / held)

### B1. Webhook Receiver Phase 1 (TradingView → Binance) — DEFERRED
- **Scope (DoD defined in chat room):** HMAC signature verification (constant-time), replay protection (timestamp tolerance window + nonce TTL), rate limiting.
- **Negative cases:** invalid/missing signature, out-of-window timestamp, duplicate nonce, missing config.
- **Status:** Blocked on `t_4ab87249` (approval by @eyad). Subtasks (HMAC, replay, rate limit, negative tests, docs) reassigned to qa/technical_writer_docs but held.
- **Approval history:** @eyad approved Phase 1 scope earlier in the chat room ("موافق على النطاق المذكور لـ Phase 1 — Webhook Receiver. يمكنكم إنشاء المهام والبدء."), then deferred in favor of fixformat v6. Re-approval needed to resume.
- **Decision needed:** resume now, or keep deferred to a later release.

### B2. ANSI Full Implementation (post K4 contracts) — HELD
- **Scope:** Full integration of ANSI color helpers across raw_scan.py/volume_gate.py/selftest.py per K4 contracts.
- **Status:** Held (t_19549617 reassigned to technical_writer_docs; t_19432aaf/t_5ba81829/t_7a64169d/t_e077a81b done).
- **Decision needed:** @eyad approval of K4 contracts → unblock.

### B3. UI/UX Full Implementation (post K5 contracts) — HELD
- **Scope:** CLI theme engine, progress indicators, report formatting/export (t_c9e59c05, t_20771cc0). Gated on core_developer defining progress callback signatures + @eyad approval of v6/UI_UX_CONTRACTS.md.
- **Status:** Held (2 tasks blocked).
- **Decision needed:** @eyad approval of UI/UX contracts + callback contract definition.

### B4. Real GUI (out of scope for current release)
- **Scope:** Desktop/web GUI for fixformat.
- **Status:** Out of scope for embedded Python (no new libs). Separate project if needed.
- **Decision needed:** @eyad — separate project or keep CLI-only.

### B5. v6 Tasks 2–12 bundle — DONE
- All steps 2–12 completed (DB, extraction modes, plan screen, reports, validators, fragmented assembly, adaptive ops, entropy, penalty, scan calibration, Direct I/O, audit.txt). Proposal t_34ad09e3 reviewed by lead and completed.

---

## C. Blocked tasks requiring decision (current board)

| Task | Assignee | Block reason | Decision needed from |
|---|---|---|---|
| t_4ab87249 — Approve webhook receiver Phase 1 | eyad | needs_input (deferred) | @eyad |
| t_c9e59c05 — CLI theme engine | core_developer | needs_input (hold, K5 gate) | @eyad (approve contracts) + core_developer (callback signatures) |
| t_20771cc0 — Progress indicators | core_developer | needs_input (hold, K5 gate) | same as above |

---

## D. Release readiness (v6)

- Core v6 work: **complete** (26 done).
- Release artifacts (tag, CHANGELOG, SHA256SUMS, README): verified by QA.
- Next: tag v6 release after @eyad review of completed work + decide on deferred items (B1–B4).

---

## E. Recommendation

1. **Tag v6.0.0** now (core work complete, QA verified).
2. **Defer B1–B4** to v6.1 unless @eyad wants them in v6.0.
3. **Unblock B3 (UI/UX impl)** only after @eyad approves v6/UI_UX_CONTRACTS.md and core_developer defines callback signatures.
