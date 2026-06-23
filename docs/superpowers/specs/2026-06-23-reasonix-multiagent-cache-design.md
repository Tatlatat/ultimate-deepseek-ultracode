# Reasonix Multi-Agent Cache & Synthesis — Design Spec

**Ngày:** 2026-06-23
**Mục tiêu một câu:** Cho native dynamic workflow (UltraCode / `/effort` / `/deep-research`) chạy nhiều tác nhân reasonix song song mà (1) tiết kiệm token tối đa — weighted cache cao nhất có thể tới ≥99.2% khi workload cho phép, (2) không quá chậm, (3) **hoàn thành công việc thật — không fallback rỗng**, đặc biệt ở bước tổng hợp cuối.

**Ưu tiên (theo user):** #1 Giá (tiết kiệm token). #2 Tốc độ. Bắt buộc: ghép trong suốt vào native dynamic workflow, vận hành bình thường.

---

## Bối cảnh & Nghiên cứu (đã làm — cơ sở của mọi quyết định)

**Cơ chế cache DeepSeek (docs chính thức api-docs.deepseek.com/guides/kv_cache):**
- Cache là **toàn cục theo chuỗi byte prefix**, server-side, tự động, KHÔNG thuộc session.
- Sliding Window Attention: mỗi cached prefix là **một unit hoàn chỉnh độc lập**; request chỉ hit khi **khớp HOÀN TOÀN** một unit.
- Persist xảy ra: (a) tại boundary cuối user-input + cuối model-output **sau khi request xong**; (b) khi hệ thống phát hiện **common prefix qua nhiều request** → persist common prefix; (c) tại mốc token cố định cho input/output dài.
- Hệ quả: **N lane fan-out đồng thời đều MISS prefix chung** vì chưa request nào xong để persist (đúng "Example 2" trong docs). Đơn vị lưu = 64 token; cache tự xóa sau vài giờ–vài ngày; "best-effort", không đảm bảo 100%.

**Reasonix v2 (esengine/DeepSeek-Reasonix main-v2):** đạt 90%+ bằng cách **tránh** vấn đề — mỗi agent (planner/executor/subagent) chạy **session riêng, append/prepend-only**, không fan-out đồng thời. "Switching models inside one shared conversation breaks the prefix — so we don't." Có sẵn `spawn_subagent` tool + skill `runAs: subagent` + `subagent_models` config; đọc skill từ `~/.claude/skills/` (Claude Code compat).

**Đo thật trên `/deep-research` (39.7M input, 280–359 lượt):**
- weighted cache ổn định ~**80%**, không tự lên.
- Phân bố miss TRẢI ĐỀU: dải 60-80% (25% input) gánh 47% miss; dải 80-95% (43% input) gánh 34% miss. **Không phải vài lane unique cá biệt.**
- **62% tổng input đến từ các lượt >150K** = lane synthesize/agent **loop nhồi history** mỗi lượt (input leo 27K→227K, 134 lượt). Verify thật thì NHẸ (1% input).
- Nguyên nhân synthesize loop (DeepSeek docs + issues): nhiệm vụ suy luận lớn + **REPORT_SCHEMA lồng nhau** → flash emit JSON lồng hay sai/`finish_reason=length` → Claude Code gọi lại → loop.

**Toán học bù trừ (đã kiểm chứng):** weighted ≥99.2% ⇒ tổng MISS ≤ 0.8% tổng input. Không thể "bù trừ" (phase sau cao kéo phase trước thấp) — chỉ cần phần unique miss >0.8% là vỡ. Đường khả thi duy nhất: **đồng-đều-hóa** — đẩy gần-như-mọi-lane lên ~99% (phần thiếu hiện tại chủ yếu là prefix-chưa-persist, cache-được, không phải nội dung unique thật).

---

## Kiến trúc tổng thể

Mọi cơ chế nằm ở **tầng gateway** (`codex-native-gateway.py`) + **skill/config reasonix** — **trong suốt với Claude Code**. Workflow/UltraCode fan-out y nguyên; không đổi cách user dùng.

```
UltraCode/Workflow/deep-research fan-out (Claude Code — KHÔNG đổi)
  → N lane → gateway /v1/messages (& /v1/chat/completions)
      → [A. Prefix byte-stable]      (phần lớn đã có)
      → [B. Prime gate]              (lane đầu persist prefix → lane sau hit)
      → [C. Chặn loop]               (đếm lượt per-lane → ngăn loop nhồi-history)
      → [D. Map-reduce synthesis]    (lane tổng hợp nặng → reasonix tự spawn sub-agent nhỏ)
      → run_reasonix_acp → DeepSeek
  → [E. Đo lường + cảnh báo]         (weighted cache từ ledger; phân loại miss)
```

4 thành phần A-D đánh 4 nguyên nhân miss/kẹt KHÁC NHAU; E là quan sát.

---

## Thành phần A — Prefix byte-stable (phần lớn ĐÃ CÓ, giữ + kiểm chứng)

Đã có trong code (các phiên trước): `normalize_prefix` strip volatile billing-header; unify 6 agent-type system prompt về 1; hoist khối shared (system+tools) lên đầu; schema-instruction đặt CUỐI cho StructuredOutput. **Việc của spec này:** xác nhận chúng còn active + đo prefix4k family = 1 trên fan-out thật.

---

## Thành phần B — Prime Gate (làm đúng theo cơ chế DeepSeek)

**Vấn đề:** N lane chung prefix `P` bắn đồng thời → `P` chưa persist → cả N miss `P`.

**Cơ chế:** key theo hash prefix; lane ĐẦU mỗi key = **primer** (chạy lane thật, 0 request thừa); lane sau = **waiter** chờ primer xong (persist `P`) rồi chạy → hit `P`.

**Quyết định (đã cân giá/tốc):** prime bằng **1 lane thật** (không request mồi) — rẻ nhất (0 token thừa), chỉ chậm 1 lane đầu/họ.

**4 cải tiến so với prime-gate cũ:**
1. Key theo prefix **~32KB** đã normalize (không phải 8KB) — khớp common-prefix unit DeepSeek persist.
2. Waiter chờ thêm **grace ~1–2s** sau khi primer set (docs: "cache construction takes seconds").
3. **Stagger** waiters thành đợt nhỏ (vd 3 lane/đợt) thay vì thả cả 16 cùng lúc — tránh đợt waiter lại đồng-thời-miss đuôi.
4. Lane prefix-độc-nhất (không họ) → **bỏ gate**, chạy ngay.

**An toàn (giữ nguyên đã có):** `prime_gate.wait(timeout=20s)` bounded; primer `set()` trong `finally` (fail-open). Env tắt: `CLAUDE_CODEX_GATEWAY_PRIME_GATE=0`.

---

## Thành phần C — Chặn loop (đòn bẩy GIÁ lớn nhất: cắt 62% input lãng phí)

**Vấn đề:** lane bị Claude Code gọi lại nhiều lượt (model trả JSON sai/narrate), mỗi lượt nhồi cả history → input phình 150K-227K → vừa tốn token, vừa kẹt, vừa cache loãng. (Lưu ý: D sẽ giải gốc cho synthesize; C là lưới an toàn chung cho MỌI lane forced-schema.)

**Cơ chế:** gateway đếm lượt lặp per-lane (nhận diện qua prefix-hash + schema giống nhau gọi lại lần thứ N trong cửa sổ thời gian ngắn). Sau **N lượt** (mặc định 3) mà vẫn không ra JSON hợp lệ → ép trả object hợp-schema (`structured_timeout_fallback`, ĐÃ CÓ) **làm lưới cuối** để workflow không hang.

**Quan trọng — không mâu thuẫn "không fallback":** C chỉ là **lưới an toàn cuối cùng** cho lane vẫn loop sau khi D đã cố giải. Mục tiêu là D làm cho synthesize hoàn thành THẬT để C **gần như không bao giờ phải kích hoạt**. Khi C phải fallback → **log cảnh báo rõ** (không im lặng) để biết có lane chưa giải được.

**Cảnh báo lane nặng:** log khi input 1 lane leo >150K (dấu hiệu loop) → quan sát được, không kẹt âm thầm.

Env: `CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES` (mặc định 3).

---

## Thành phần D — Map-reduce synthesis: "tác nhân trong tác nhân" (giải GỐC, không fallback)

**Ý tưởng (user):** Claude vẫn gọi 1 lane synthesize (không sửa script `/deep-research`). Nhưng BÊN TRONG reasonix, lane đó không tự làm một mình — nó **spawn nhiều sub-agent nhỏ** (Map) rồi **1 sub-agent gộp** (Reduce), trả JSON cuối. Reasonix đã có sẵn `spawn_subagent` + skill `runAs:subagent`.

**Một tên trúng 2 đích:** (1) mỗi sub-agent map nhận task NHỎ → flash làm được, không vỡ schema, không loop → synthesize hoàn thành THẬT; (2) không còn 1 lane khổng lồ loop 134 lượt → tiết kiệm token + không kẹt.

**3 phần cần làm:**

1. **Skill `~/.claude/skills/map-reduce-synthesis/SKILL.md`** (reasonix đọc từ `~/.claude/skills/` — đã xác nhận):
   - Frontmatter: `runAs: subagent`, `description:` (bắt buộc, để vào skills index), `model:` (flash — rẻ).
   - Body hướng dẫn: nhận block claim + schema đích → chia claim thành nhóm (token-bounded) → `spawn_subagent` mỗi nhóm để tóm tắt/nhóm cục bộ (Map) → `spawn_subagent` 1 lần để gộp các kết quả thành JSON đúng `REPORT_SCHEMA` (Reduce) → trả raw JSON.
   - Lý do dùng spawn_subagent: tool-call của sub-agent KHÔNG vào context cha → context không phình.

2. **Gateway nhận diện lane synthesize nặng** → inject chỉ dẫn vào prompt gửi reasonix:
   - Nhận diện: tool StructuredOutput có schema chứa mảng-lồng (`findings[]`/`results[]` với object con) **VÀ** prompt_len lớn (> ngưỡng, vd 20KB).
   - Inject (ở cuối prompt, sau task): "Đây là nhiệm vụ tổng hợp lớn. Dùng `run_skill({name:'map-reduce-synthesis', arguments: <task>})` để chia nhỏ rồi gộp; trả về đúng JSON schema."
   - Env gate: `CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS=1` (mặc định on; =0 tắt).

3. **Config reasonix** đảm bảo: skill load được (`~/.claude/skills/` trong skill paths — mặc định đã có), `spawn_subagent` enabled, `subagent_model` = flash cho skill này (rẻ).

**Đánh đổi giá:** map-reduce dùng nhiều sub-agent nhỏ (mỗi cái 1 prefix-miss nhỏ) nhưng TỔNG vẫn rẻ hơn 134 lượt loop nhồi-227K-history rất nhiều. Net: tiết kiệm.

---

## Thành phần E — Đo lường + đồng-đều-hóa (không tin cảm tính)

- Sau mỗi run: tính **weighted cache = Σ(in_tok×cache%)/Σ(in_tok)** từ `reasonix-cost.jsonl` (ledger chỉ ghi lane reasonix — đã verify).
- **Histogram theo dải cache** (như đã làm) — mục tiêu: phần lớn lane dồn về ≥99%, không tản mát.
- **Phân loại miss:** prefix-cold (B sửa được) vs unique-tail (không tránh được) vs loop-inflation (C/D sửa được).
- **Cảnh báo ngưỡng:** weighted < kỳ vọng-theo-workload → log (không chặn).

**Kỳ vọng trung thực theo workload (KHÔNG hứa 99.2% mọi nơi):**
- Review code cùng file (prefix-chung áp đảo): **≥99.2%** — mục tiêu khả thi.
- Research/fetch web: đẩy gần trần lý thuyết; báo cáo số thật + phần unique không tránh được. Với D giải loop, kỳ vọng weighted tăng mạnh từ 80% (vì 62% input loop-inflation biến mất).

---

## Testing & Verify không phá native workflow

- **A/B prime gate** ON vs OFF cùng task → đo weighted.
- **A/B map-reduce** ON vs OFF trên `/deep-research` → synthesize HOÀN THÀNH (không fallback) + tổng token giảm + weighted tăng.
- **Unit test:** prime gate (primer/waiter/fail-open/key-32KB); chặn-loop (đếm lượt → fallback đúng N); nhận-diện-synthesize (schema lồng + size).
- **Smoke test sau mỗi thay đổi:** 1 UltraCode fan-out nhỏ thật → (a) vẫn fan-out, (b) lane=reasonix (ledger), (c) không hang.
- **Regression:** test StructuredOutput đã fix (qwen/workflow scriptPath) không hỏng.
- **3 lớp an toàn:** env tắt từng thành phần; fail-open; bounded-wait.

**Definition of Done:**
1. `/deep-research` chạy XONG, synthesize ra report thật (KHÔNG fallback), không hang.
2. Tổng token/run giảm rõ so với baseline (cắt loop-inflation).
3. Weighted cache tăng rõ; trên workload prefix-chung-áp-đảo đạt ≥99.2% đo lặp được.
4. UltraCode/workflow vận hành bình thường (smoke + regression xanh).

---

## Phạm vi & thứ tự (đề xuất tách plan)

Đủ lớn để tách. Thứ tự theo đòn-bẩy-giá giảm dần:
1. **D (map-reduce synthesis)** — giải gốc synthesize, cắt 62% input loop. Lớn nhất.
2. **C (chặn loop)** — lưới an toàn chung, rẻ.
3. **B (prime gate hoàn thiện)** — đẩy dải 60-95%→99%.
4. **E (đo lường)** — xuyên suốt, làm sớm để đo A/B các bước trên.
5. **A (kiểm chứng prefix-stable)** — xác nhận nền tảng còn đúng.

Mỗi phần có env tắt riêng → triển khai + verify độc lập, không phá nhau.
