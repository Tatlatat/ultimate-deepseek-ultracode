# Token-Reduction Experiment System — Design Spec

**Date:** 2026-06-24
**Scope:** workflow fan-out + fleet (reasonix CLI tay KHÔNG nằm trong scope).
**Nguồn:** deep-research (14 findings verified) + multi-agent design fan-out (7 designers, file:line thật) + đo ledger thật (5804+ lanes).

## Mục tiêu (ưu tiên, từ dữ liệu thật của owner)

Cost split đo thật (cache 96.3%): **output 42.3%, miss 38.1%, hit 19.6%** → output đắt ~101x hit, miss ~51x hit. Vấn đề tập trung ở ĐUÔI: **top 10% lane = 82% miss + 44% output**.

1. Giảm OUTPUT token (42.3% chi phí — đắt nhất)
2. Giảm INPUT/miss token (38.1%, tập trung ở lane khổng lồ under-decomposed)
3. Đẩy cache 96.3% → 98-99% (giảm phần unique 26.5%; shared prefix đã 73.5% byte-identical/cacheable)
4. KHÔNG giảm chất lượng (research: token spend = 80% variance hiệu suất → cắt LÃNG PHÍ, không cắt việc).

## Nguyên tắc thiết kế (owner-approved)

- Mỗi cơ chế = **1 lever bật/tắt qua env flag** (như KEEPALIVE/PREFIX_GUIDE), **default 0** (measure-then-promote), đo độc lập.
- Mỗi lever có **lớp mềm** (PREFIX_GUIDE guidance) và/hoặc **lớp cứng** (gateway/shim enforcement).
- **KHÔNG BAO GIỜ truncate** context (đã bị reject — bài học `under-decomposition`). Giảm bằng **decompose / summarize / retrieve**.
- **Harness ĐO trước, quyết sau** — không đoán lever nào hay; chạy ma trận trên workload chuẩn.

---

## PHẦN 0 — HARNESS (backbone, BUILD FIRST)

**File mới:** `runtime/lever-matrix-bench.py` (~350 dòng). KHÔNG reinvent — import internals đã verify của `realworld-bench.py`: `start_gateway` (:50), `ledger_window` (:127-147), `grade` (:218-271). Env-flag injection theo `cross-workflow-bench.py:start_gateway`.

**Workload chuẩn cố định** (`WORKLOAD_SPEC`, prompt byte-identical across mọi config → lever là biến DUY NHẤT):
- **READ group:** 8 lane single-file-summary đồng thời (đại diện 63% read lane, ~145 tok out), mỗi lane StructuredOutput `{summary,file}` để quality-gate check tool_input.
- **EDIT group (MỚI — chưa bench nào có):** 2 lane high-output (đại diện 10% lane = 45% output, ~2644 tok), viết/sửa code thật → đây là cách DUY NHẤT để falsify edit-immunity của F/A.
- **REVIEW group:** 6 lane chia 1 SHARED_BLOCK 12K + suffix 1 từ/lane, warm-up lane đầu (như realworld-bench:199-207) — shape shared-prefix 99.2%, làm **regression guard** cho C/D/E (chứng minh chúng KHÔNG phá cache shared-prefix).
- **WORKFLOW-shaped lane (cho E/PREFIX_GUIDE):** một lane đi qua Workflow hook để đo lever chỉ kích hoạt qua hook (nếu thiếu, harness mù với E-advisory + PREFIX_GUIDE).

**Đo per-config:** weighted + median cache%, total input tok, total output tok, **est cost** (theo split owner: hit:miss:out ≈ 1:51:101), và **QUALITY gate** (output đúng/non-empty, edit_correct, hollow-rate ≤2%).

**Ma trận:** baseline → +mỗi lever (on/off) → best_combo (union các flag default-ON). Output: bảng `config | cache% | input | output | cost | quality`. **Owner nhìn bảng → quyết giữ cái nào.**

**Plumbing chung (BUILD ONE — cross-lever risk):**
- `lane_type` field trong `append_reasonix_cost` (gateway:1163) — thêm 1 lần, A+F+G dùng chung.
- `classify_lane_type()` (gateway, dùng lại `_READER_INTENT_RE`/`_SYNTHESIS_INTENT_RE` :778-800 + `_EDIT_INTENT_RE` mới) — **1 classifier dùng chung A+F**, KHÔNG build 2 lần.
- `maxOutputTokens` forwarding: request dict (gateway:1316-1325) → `run-lane.mjs:169` → `CacheFirstLoop.maxOutputTokens` (fork loop.ts:107,963 — VERIFIED đã wired, KHÔNG cần rebuild fork). **1 dòng dùng chung A+F.**
- `output_tokens` vào `ledger_window` (realworld-bench:141-143, 3-dòng patch).

---

## PHẦN 1 — 6 LEVER (xếp hạng theo ROI vs build-cost)

### F — OUTPUT DISCIPLINE [ROI #1: ~10-20% chi phí | build LOW-MED]
**Flag:** `CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE=0` + sub: `_DIRECTIVE`/`_MAX_TOKENS`/`_STOP_SEQ`; budgets `_MAX_TOKENS_EDIT=3500`/`_READ=512`/`_DEFAULT=2048`.
- **Vì sao #1:** lever DUY NHẤT đánh vào bucket output 42.3%. Edit lane = 44% output.
- **Builds on:** `CacheFirstLoop.maxOutputTokens` (loop.ts:107,246,963,978 VERIFIED, no rebuild).
- **Lớp cứng (lever thật):** `max_tokens` cap per-lane-type qua shim. **Lớp mềm:** `output_discipline_directive()` (~25 dòng) — cấm narration/CoT verbosity; edit lane: **diff-only** (Aider: lazy code-eliding giảm 3x), cấm reprint unchanged.
- **CẢNH BÁO (cross-lever):** stop-seq passthrough (provider_chat_payload:525) chỉ áp /v1/messages, **KHÔNG tới fan-out lane** (qua shim) → `_STOP_SEQ` default OFF, không tính nó vào fan-out. Real lever = max_tokens + directive.
- **New:** `detect_lane_type()` (~20 dòng, = classifier chung), `output_discipline_directive()` (~25 dòng), 3 budget flags.

### A — SCHEMA-ENFORCED SUMMARY (read lane) [ROI #2: MED | build LOW, gần subset F]
**Flag:** `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY=0` + `_READ_SUMMARY_MAX_TOKENS=512`.
- **Vì sao #2:** cơ chế gần-subset F (read budget = F's READ budget). Lý do giữ riêng: đo **second-order saving** — output read lane = input của synthesize lane sau (đây là cái owner lo: "output thành input bước sau").
- **Builds on:** cùng shim/loop wiring F; sibling của `structured_output_prompt_instruction()` (:719-749).
- **Lớp mềm:** `read_lane_summary_instruction()` — read lane ép trả JSON summary ngắn `{findings[≤5], files_read, flag}`, cấm dump raw file. **Lớp cứng:** read-lane cap 512.
- **Đo:** drop output read lane TRỰC TIẾP + drop input synthesize lane (second-order — cái prize thật).

### C — SHARED READ-CACHE [ROI #3: MED-HIGH | build MED-HIGH]
**Flag:** `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE=0` + `_CAP=512`/`_TTL_S=300`/`_MAX_BYTES=131072`.
- **Vì sao #3:** convert miss→hit trực tiếp khi nhiều lane re-read cùng file (fan-out same codebase). Miss = 38.1% tập trung ở 10% heavy lane re-read.
- **ARCHITECTURE INVARIANT (cross-lever):** **PHẢI ở GATEWAY, KHÔNG shim** — shim:168 `session:undefined` → mỗi lane là subprocess ephemeral riêng; cache per-process chết với shim, không share. Cache = gateway module-level dict (như `_PRIME_GATES`/`_LANE_COUNTS` :87/97).
- **New:** `_READ_SUMMARY_CACHE` dict+lock+`_evict_oldest`, `extract_file_paths_from_prompt`, lookup/build/inject/populate.
- **RISK (blocking gate):** byte-stability — block inject phải ở vị trí FIXED (sau shared system, trước per-lane tail), byte-deterministic. **Unit test bắt buộc:** 2 prompt khác nhau chỉ ở tail → injected-prefix bytes IDENTICAL. Nếu sai → phá cache 73.5% = regression.

### B — SUB-AGENT READ-IN-ISOLATION [ROI #4: MED, adoption-risk | build MED]
**Flag:** `REASONIX_READ_ISOLATED=0`.
- **Vì sao #4:** infra hoàn chỉnh nhất (fork đã có `spawnSubagent` src/tools/subagent.ts, `EXPLORE_SYSTEM`), nhưng ROI phụ thuộc **model có CHỌN tool không** (adoption). B = đọc-1-file-lớn-1-lần; C = cùng-file-nhiều-lane. Bổ sung nhau.
- **Builds on:** `spawnSubagent` + `subagentClient` closure (đã có).
- **New:** ~30 dòng tool block trong setup.ts + nudge ở `read_file` description chống adoption-risk. **Cần fork rebuild + re-vendor.**
- **RISK:** adoption — đo **tỷ lệ model gọi tool** là metric make-or-break.

### E — SPECULATIVE PREFETCH [ROI #5: MED, high-variance | build HIGH]
**Flag:** `CLAUDE_REASONIX_PREFETCH_CONTEXT=off|advisory|inject` (default off) + `_MAX_FILES=8`/`_FILE_CAP_BYTES=32768`/`_TIMEOUT=20`/`_PLANNER=0`.
- **Vì sao #5:** owner's "codebase prediction". Đoán file lane cần từ task → summarize 1 lần → nhét vào shared prefix → mọi lane cache-HIT thay vì mỗi lane miss.
- **Builds on:** PreToolUse(Workflow) hook (reasonix-workflow.py:main có full script+cwd trước fan-out).
- **New:** `predict_prefetch_files` (grep/regex ~50 dòng), `summarize_files` (parallel HTTP ~80 dòng), assemble vào prefix.
- **RISK:** đoán SAI = dead tokens vào MỌI lane (8-file/256-tok = ~2K dead/lane ~10%). **Ship advisory mode trước** (zero prefix risk), đo precision, chỉ promote inject nếu precision đủ cao. Byte-stable hazard như C.

### D — PRE-INDEX [ROI #6: LOW-vs-cost | build HIGH, external dep]
**Flag:** `CLAUDE_REASONIX_PREINDEX=0` + `_TIMEOUT=120` + `REASONIX_EMBED_PROVIDER/MODEL/BASE_URL`.
- **Vì sao #6 (LAST):** ZERO tác động bucket output 42.3%; chỉ ~5-15% trên subset read-exploration lane; phụ thuộc **embedding provider** (Ollama/openai-compat — UNVERIFIED máy này) + fork rebuild.
- **Builds on:** `semantic_search` đã tồn tại + đã trong toolSpecs khi index có.
- **Đúng cách:** dùng **query-tool path** (KHÔNG inject prefix → sidestep byte-stable hazard). Build index 1 lần per codebase, gateway là SOLE build trigger (per-lane chỉ `indexCompatible()` read-only — tránh JSONL race khi 2 lane cùng build).
- **New:** export `buildIndex`/`indexCompatible` từ index.ts + rebuild + re-vendor.

---

## PHẦN 2 — THỨ TỰ TRIỂN KHAI

1. **HARNESS (G)** — lever-matrix-bench + output_tokens patch + lane_type field + EDIT workload + edit_correct gate. Capture baseline thật (96.3%) TRƯỚC khi đụng lever nào.
2. **PLUMBING CHUNG** — `classify_lane_type()` 1 lần + maxOutputTokens 1-dòng (gateway→shim→loop, verified). Substrate dùng chung F+A.
3. **F** (ROI#1) — directive + per-type max_tokens trên plumbing. Flag default OFF. Gate cứng: edit_correct + hollow ≤2%.
4. **A** (thin add) — read summary instruction + 512 cap. Đo direct + second-order synthesize drop.
5. **C** (#3) — gateway cache + byte-stability unit test (BLOCKING gate). Scenario C2 (chạy 2 lần, assert +5pts run2).
6. **B** (#4) — read_file_isolated tool (fork rebuild + re-vendor). Đo parent in_tok drop + adoption rate.
7. **E** (#5) — prefetch ở Workflow hook. Advisory mode trước, đo precision, promote inject nếu đáng.
8. **D** (#6 LAST) — chỉ build sau khi xác nhận embedding provider + workload read-heavy.

**B+D batch chung 1 fork rebuild** (cả 2 cần re-vendor) để tránh 2 lần vendor churn.

---

## PHẦN 3 — CROSS-LEVER RISKS (đã verify)
1. **A+F collide:** cùng viết maxOutputTokens + cùng classifier → build ONE classifier + ONE shim line (F owns budget table, A's read=512 = F's READ budget).
2. **lane_type field:** A+F+G cùng cần → thêm 1 lần.
3. **Byte-stable prefix hazard (C+E+D-digest):** inject sai vị trí = phá cache 73.5% = regression. Mỗi cái: byte-deterministic + fixed boundary + unit test. D sidestep bằng query-tool (giữ nguyên).
4. **C phải gateway không shim** (shim ephemeral) — bỏ vào shim = silent no-op.
5. **C+E+KEEPALIVE family drift:** block inject đổi prime-gate/keepalive family key → keepalive warm prefix CŨ. Giữ block stable trong session hoặc chấp nhận 1 lane cold re-prime. Đo bằng PREFIX_TRACE.
6. **B+C overlap:** đo riêng + chung (best_combo) để bắt double-counting.
7. **F stop-seq inert cho fan-out** → default OFF, không credit.
8. **D concurrent-build race:** gateway pre-build SOLE trigger, per-lane read-only.
9. **Harness PREFIX_GUIDE blind spot:** workload direct-to-gateway không kích PREFIX_GUIDE/E-advisory → cần Workflow-shaped lane.

---

## PHẦN 4 — OWNER DECISIONS (đã chốt 2026-06-24)
1. **F default flag:** **default 0** (measure-then-promote). Hướng tới bật trong tương lai NHƯNG bắt buộc đo qua harness trước khi promote.
2. **DeepSeek max_tokens mid-block:** **OK chạy probe** — gửi 1 edit lane với max_tokens thấp có chủ ý, kiểm xem cap có cắt giữa SEARCH/REPLACE làm edit_file fail không, TRƯỚC khi bật output cap cho edit lane.
3. **Edit-lane budget:** **đọc P95 thật từ ledger → EDIT budget = ceil(P95×1.2)** (data-driven, không hardcode 3500).
4. **Summary schema A:** **fixed `{findings, files_read, flag}`** (prefix-stable, không cần metadata channel mới).
5. **Embedding provider D:** owner xác nhận **đã có embedding model** (từ dự án semantic search trước). D buildable; verify provider cụ thể khi tới lượt D (#6).
6. **Fork rebuild/re-vendor (B+D):** **OK re-vendor**, batch B+D chung 1 rebuild (dùng quy trình `build:engine` từ Sub-project 2). Lever fleet-only (F/A/C) làm trước, B/D (cần rebuild) sau.
7. **E prediction:** **ship advisory mode trước** (zero prefix risk, đo precision), chỉ promote sang inject mode nếu precision đủ cao. Grep-symbol fallback KHÔNG default-on.
8. **G cold-order:** **fixed order + warm-up lane chung trước mỗi config** (cache "ấm" như nhau → công bằng + tái lập). Như cách realworld-bench warm-up.
9. **best_combo (G):** **auto = union các flag default-ON** (chống drift, tự cập nhật khi thêm lever).
10. **C persistence:** **persist ra đĩa** `runtime/read-summary-cache.json` (ấm lại sau gateway restart — hệ thống này hay restart gateway), mtime-freshness khi load.
