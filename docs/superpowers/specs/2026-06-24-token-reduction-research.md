# Token Reduction — Research + Áp dụng vào reasonix fleet

**Date:** 2026-06-24
**Nguồn:** deep-research (103 agents, 14 findings high-confidence đã adversarial-verify) + đo thực tế từ `runtime/reasonix-cost.jsonl` (5804 lanes).

---

## 0. Sự thật từ DỮ LIỆU THẬT của bạn (5804 lanes) — đây là cái định hướng tất cả

| Chỉ số | Giá trị | Ý nghĩa |
|---|---|---|
| Output / input (volume) | **2.0%** | Output ít nhưng… |
| Output share of COST | **~42%** | …đắt nhất. Output ~20-50x đắt hơn input hiệu dụng. |
| **Output tập trung** | **top 10% lane = 44% tổng output** | Vấn đề KHÔNG đều — vài lane over-generate |
| **Cache-miss tập trung** | **top 10% lane = 82% tổng miss** | Vài lane khổng lồ gây hầu hết miss |
| Lane output tệ nhất | 17893 tok, cache 62% | Lane "đi lạc": cache thấp + output khủng |
| Lane input tệ nhất | **532469 tok**, cache 80% | Under-decomposed: 1 lane nuốt 532K context |
| Output median / p90 | 370 / 1613 | Đuôi dài |
| Input median / p90 | 19K / 82K | Đuôi dài |

**Kết luận #1 (quan trọng nhất):** vấn đề token KHÔNG phân tán — nó tập trung ở **một số ít lane khổng lồ / đi lạc**. Đòn bẩy lớn nhất là **KẸP ĐUÔI (tail-capping)** + ép decompose, KHÔNG phải tối ưu lane trung bình (lane median đã ổn: 370 out, cache 99.4%).

---

## 1. GIẢM OUTPUT TOKEN (ưu tiên #1 — 42% chi phí)

### 1A. Edit bằng UNIFIED DIFF thay vì full-file / SEARCH-REPLACE [high, 3-0]
- **Số đo:** Aider — chuyển sang unified diff làm laziness-benchmark 20%→61%, giảm "lazy code-eliding" (viết `// ... rest unchanged`) 3x. Nguồn: aider.chat/docs/unified-diffs.
- **Vì sao hợp với bạn:** các lane output 11-17K tok thường là lane in/rewrite cả file. Diff-only ép model chỉ xuất phần đổi → cắt thẳng đuôi output p90.
- **Áp dụng reasonix:** fork engine `buildCodeToolset` có `edit_file`/`multi_edit`. Ưu tiên tool diff/SEARCH-REPLACE hẹp; trong system prompt + PREFIX_GUIDE thêm rule "edit = minimal diff, NEVER reprint unchanged code, NEVER write placeholder comments". CacheFirstLoop đã có `ToolCallRepair` — đảm bảo nó phạt full-file rewrite.

### 1B. Output schema TERSE + max_tokens theo loại lane [high]
- **Số đo:** Anthropic — embed scaling rules; tool-budget theo độ phức tạp.
- **Áp dụng reasonix:** gateway hiện nhận `max_tokens` từ payload (line 514). Workflow `agent({schema})` nên dùng schema CHẶT (vài field, string ngắn). Read/summary lane: cap max_tokens thấp (256-512). Chỉ edit/synthesis lane mới cho cao. Đây là cái fork `budgetUsd` + gateway max_tokens làm được nhưng chưa ép theo loại lane.

### 1C. Ngăn CoT verbosity / narration [high 2-1 caution]
- **Caution quan trọng:** token spend giải thích ~80% variance hiệu suất multi-agent — **cắt output THỪA (narration, lazy comment, CoT lan man), KHÔNG cắt việc productive.**
- **Áp dụng:** PREFIX_GUIDE thêm "no narration, no 'I will now…', no restating the task; answer = the artifact only". Read lane đã có rule summary — siết thêm "summary ≤ N bullets".

---

## 2. GIẢM INPUT/CONTEXT TOKEN (ưu tiên #2)

### 2A. Read-side là 76% token coding-agent → prune đọc là đòn bẩy input lớn nhất [high, 3-0]
- **Số đo:** SWE-Pruner (arXiv 2601.16746) — read ops = 76.1% token. Nguồn corroborated (Vantage, Augment, Stanford).
- **Khớp local:** lane input 200-532K của bạn = đọc quá nhiều file. Đây CHÍNH là chỗ.

### 2B. "Read in isolation, return short summary" — cơ chế cốt lõi cho fan-out [high, 3-0]
- **Số đo:** Anthropic — subagent dùng hàng chục K token nhưng trả về **1-2K summary**. Multi-agent hơn single-agent 90.2%. Claude Code đạt ~98% nhờ pattern này. **Caution:** bỏ summarizer → regress 10.4% (giữ fidelity).
- **Áp dụng reasonix:** PREFIX_GUIDE đã có rule này (#7). Cần **ép cứng** hơn: read lane output schema = summary object, KHÔNG raw dump; downstream lane consume summary, không re-read.

### 2C. Just-in-time context (glob/grep/head/tail) thay vì dump cả file [high, 3-0]
- **Số đo:** Anthropic — agent load identifier nhẹ, dùng head/tail. **Nuance:** hybrid (seed trước + explore sau), KHÔNG thuần JIT.
- **Áp dụng:** fork `buildCodeToolset` đã có file/grep/semantic. Ép lane dùng grep/head trước khi đọc full — qua prompt + tool design.

### 2D. Code-aware compression — nếu cần nén context lớn [high, 3-0]
- **LongCodeZip** (arXiv 2510.00446, github YerbaPage/LongCodeZip): nén 5.6x không giảm chất lượng, perplexity-based function-ranking.
- **SWE-Pruner** (2601.16746): goal-conditioned pruning cắt 23-38% token AND giảm 18-26% số vòng (ít explore thừa).
- **Squeez** (2604.04979, github KRLabsOrg/squeez): prune 1 tool-observation bỏ ~92% token, giữ verbatim evidence (survive thành next-step input — đúng mối lo của bạn).
- **LongLLMLingua** (2310.06839, Microsoft): nén ~4x, +21% accuracy long-context QA.
- **Áp dụng:** đây là cơ chế cho lane khổng lồ (>100K input). Có thể thêm 1 prune-step trước khi feed context lớn. Cân nhắc sau (phức tạp hơn tail-cap).

---

## 3. TĂNG CACHE HIT 95→98-99% (ưu tiên #3-4 — hit rẻ 50x)

### 3A. Byte-identical stable prefix: static FIRST, dynamic LAST [high, 3-0]
- **Số đo:** "Don't Break the Cache" (arXiv 2601.06007, 500+ sessions OpenAI/Anthropic/Google): đặt dynamic content CUỐI, tránh dynamic function-calling, exclude dynamic tool-results → cache ổn định hơn. Cache cho 41-80% cost reduction.
- **Khớp reasonix:** bạn ĐÃ làm (PREFIX_GUIDE #5 strict-order, codeSystemPrompt shared, normalize_prefix). Cái thiếu: **exclude dynamic tool-results khỏi prefix** + đảm bảo timestamp/random không lọt vào prefix bytes.

### 3B. Same-prefix → same-replica routing [high, 3-0]
- **Ray Serve PrefixCacheAffinityRouter** (Ray 2.54/2.55): route request cùng prefix về cùng replica. **Self-hosted only** — chỉ áp dụng nếu bạn tự host DeepSeek; với DeepSeek API thì cache là server-side của họ (không control routing).
- **Vì sao đạt 99.7%:** workflow cũ của bạn đạt 99.7% vì prefix byte-identical chiếm phần lớn volume + same-codebase fan-out. Đó là điều kiện lý tưởng — research xác nhận ceiling 99%+ CHỈ khi shared-prefix dominate token volume.

---

## 4. CHỐNG OVER-ENGINEER / LAN MAN (đòn bẩy cho CẢ output lẫn input)

### 4A. Fixed decomposed pipeline thay open-ended LLM planning [high, 3-0]
- **Số đo:** PatchPilot (arXiv 2502.02747, ICML 2025): pipeline rule-based 53.6% SWE-bench ở **~20x rẻ hơn**, variance thấp hơn agentic 62.2% (~$10K). Trade 8.6pt accuracy lấy 20x cost.
- **Áp dụng:** workflow của bạn nên scope cứng từng phase (Scope→Search→Fetch→Verify→Synthesize bạn đã có cho research). Coding workflow: ép plan-then-act, lane = 1 nhiệm vụ atomic (PREFIX_GUIDE #6 đã nói — siết thêm bằng tool-budget).

### 4B. Tool-call/effort budget nhúng trong prompt [high, 3-0]
- **Số đo:** Anthropic — "simple = 1 agent 3-10 calls; comparison = 2-4 agents 10-15 calls; complex = 10+ agents". Ngăn over-invest.
- **Áp dụng reasonix:** thêm vào PREFIX_GUIDE / per-lane prompt một **tool-call budget** rõ ràng theo loại lane; gateway/loop enforce `maxIterPerTurn` theo budget đó (fork có maxIterPerTurn — wire nó theo lane-type).

---

## 5. XẾP ƯU TIÊN ÁP DỤNG (theo đòn bẩy thật, từ local data)

| # | Việc | Đòn bẩy | Khó | Nguồn |
|---|---|---|---|---|
| **1** | **Tail-cap input**: lane >~100K context → ép decompose / từ chối (top 10% = 82% miss) | RẤT cao | Trung | local + SWE-Pruner |
| **2** | **Tail-cap output**: max_tokens theo lane-type, diff-only edits (top 10% = 44% output) | RẤT cao | Thấp | Aider + local |
| **3** | **Ép read→summary cứng** + no-narration trong PREFIX_GUIDE | Cao | Thấp | Anthropic |
| **4** | **Tool-call budget per lane-type** + plan-then-act | Cao | Trung | Anthropic + PatchPilot |
| **5** | Exclude dynamic tool-results khỏi prefix (cache stability) | Trung | Trung | Don't Break the Cache |
| **6** | Code-aware prune (LongCodeZip/Squeez) cho lane lớn | Trung (sau tail-cap) | Cao | LongCodeZip/Squeez |

**Quan trọng (caution từ research):** token spend = 80% variance hiệu suất. Tail-cap phải **ép decompose** (chia nhỏ lane to), KHÔNG phải **cắt cụt** (truncate làm hỏng việc). Đây đúng bài học `reasonix-under-decomposition-rootcause` của bạn: cap cứng đã bị reject; lời giải là decompose finer, không truncate.

---

## Repos/papers để tham khảo (đã verify)
- Aider unified-diffs — aider.chat/docs/unified-diffs.html
- SWE-Pruner — arXiv 2601.16746 (read=76% token, prune 23-38%, -18-26% rounds)
- LongCodeZip — arXiv 2510.00446, github YerbaPage/LongCodeZip (5.6x)
- Squeez — arXiv 2604.04979, github KRLabsOrg/squeez (92% prune, survive next-step)
- LongLLMLingua — arXiv 2310.06839, Microsoft (4x, +21%)
- Don't Break the Cache — arXiv 2601.06007 (prefix stability, 500+ sessions)
- Ray PrefixCacheAffinityRouter — Ray docs (self-hosted routing)
- PatchPilot — arXiv 2502.02747, ICML 2025 (fixed pipeline 20x cheaper)
- Anthropic: effective-context-engineering-for-ai-agents; multi-agent-research-system
