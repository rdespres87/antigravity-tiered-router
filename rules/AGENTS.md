# Tiered Model Router & Local-First Execution (always-on)

Governs 3-tier delegation, local LM Studio offloading (0 cloud tokens), and Headroom context compression.

## 1. 3-Tier Hierarchy & Ceiling Principle
- **Ceiling Principle**: Subagents can only be `flash_lite`, `flash`, `pro`, or `inherit` (never Claude/GPT). If Flash is selected in the IDE, the agent cannot escalate up to Claude/GPT; routing is always downward.

| Tier | Target | Mechanism | Best Used For |
|---|---|---|---|
| **Tier 1 (Dual Local)** | LM Studio: **Maker** (Qwen3.8 27B Splash) + **Inquisitor** (Qwen3.6 35B A3B) | `call_mcp_tool(ServerName="tiered-router", ToolName="execute_local_task", Arguments={"role": "maker"|"inquisitor", "prompt": ...})` | **Maker**: code, tests, fixtures, boilerplate, docstrings, commit messages (0 cloud tokens). **Inquisitor**: Phase 3 merciless diff audit (0 cloud tokens). |
| **Tier 2 (Medium)** | Gemini Flash / `pro` subagent | `invoke_subagent` | Multi-file features, complex refactors, test-driven debugging. |
| **Tier 3 (High)** | Primary session / `consultant-high` | Primary session | Architecture, subtle concurrency/security, high blast radius. |

## 2. Permanent Local-First & Critical RED Protocol
- **Permanent Local-First (GREEN / YELLOW / RED)**: Whenever LM Studio is active (`local=...`), default to `execute_local_task(role="maker")` for modular functions, algorithms, unit tests, fixtures, boilerplate, docstrings, and commit messages, and `execute_local_task(role="inquisitor")` for Phase 3 audits.
- **Critical Quota RED (<15% 7d remaining or RED)**:
  - Cloud model acts as **pure orchestrator**: strictly FORBIDDEN from generating domain code, algorithms, or test implementations directly in chat or cloud subagents.
  - **100% Local Offloading**: code generation, refactoring, bug fixes, unit tests, compiler/test error diagnosis (`context=error`), docs, and conventional commit drafting from `git diff` MUST run via `execute_local_task`.
  - Cloud only frames requirements, calls `execute_local_task`, applies edits (`write_file`/`edit_file`), runs native verification, and outputs telegraphic summaries (<100 words).

## 3. Local Auto-Healing, Fallback & Headroom Compression
- **Auto-Healing Loop**: Include target language idiomaticity constraints in `execute_local_task` prompts. Validate immediately with native toolchains (`cargo check`, `cargo test`, `tsc --noEmit`, `pytest`). On failure, pass diagnostics back to `execute_local_task(context=error)`. After 2 local failures, ask the user via `ask_question` before escalating to cloud.
- **Strict Fail-Closed (Zero Silent Cloud Fallback)**: `execute_local_task` serializes concurrent projects via a cross-process FIFO queue (`~/.gemini/tmp/lmstudio_queue.lock`, 15 min queue wait, 3 min idle, 10 min inference). NEVER fallback silently to cloud (`flash`/`pro`/primary) when LM Studio is busy, slow, or offline — in ANY budget state (GREEN, YELLOW, RED). If `execute_local_task` returns `allow_cloud_fallback: false` or fails, STOP and ask the user via `ask_question` (retry local vs explicit cloud consent) before consuming cloud tokens. On architectural roadblocks, use `format_high_consultation` and pass accumulated findings.
- **Mandatory Headroom Compression (>80 lines / >800 tokens)**: For large file reads, `git diff`, or compiler/test logs >80 lines, invoke `call_mcp_tool(ServerName="headroom", ToolName="headroom_compress", Arguments={"content": ...})`. Reason on the compressed output and fetch exact slices via `headroom_retrieve(hash=...)` only when needed. For CLI/`agy` subagents, set `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787/p/<project>`.

## 4. Standard On-Ramps & "Recommend and Stop"
- **New Project**: trigger `project-scaffold` skill before writing any domain code.
- **Feature Flow**: spec (`RequestFeedback: true`) -> local/T2 implementation -> Phase 3 local audit -> `grill-orchestrator` (critical components) -> native tests + `cargo mutants --in-diff` (Rust) -> Phase Boundary.
- **Bug / Regression**: minimal failing test (Tier 1) -> local fix + auto-healing -> Phase 3 local audit -> full validation.
- **Architecture Health**: `headroom_retrieve` -> identify seams -> `grill-orchestrator` if high blast radius.
- **Recommend and Stop**: On architectural dilemmas or heavy workflow choices, state the recommended path and why alternatives were ruled out, then wait for user confirmation before editing files or launching heavy loops.
