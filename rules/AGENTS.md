# Tiered Model Escalation & Fallback Rule

This rule governs how tasks and subagent operations are delegated across model tiers in Antigravity to maximize speed, save 100% cloud tokens on easy tasks, and ensure rock-solid reliability.

## 1. The 3-Tier Hierarchy

| Tier | Execution Target | Mechanism | Role & Best Used For | Token Economy |
| :--- | :--- | :--- | :--- | :---: |
| **Tier 1 (Low)** | **Local LM Studio Model** (dynamically detected, e.g. Qwen 3.6 35B) | MCP Tool: `execute_local_task` | Mechanical tasks, standalone functions, unit tests, docstrings, boilerplate, syntax fixes, explanations, regex. | **100% cloud tokens saved** (0 cost) |
| **Tier 2 (Medium)** | Gemini 3.8 Flash / Cloud Subagent | `task-runner-medium` or `pro` subagent | Multi-file features, complex refactoring, test-driven debugging, standard implementation. | ~50-65% savings |
| **Tier 3 (High)** | Claude / Gemini 4 Argon / Primary Session | Primary Session / `consultant-high` | System architecture, distributed consensus, security audits, database migrations, subtle race conditions. | Baseline |

---

## 2. Delegation & Routing Protocol

Whenever processing a user request or decomposing a project into subtasks:

1. **Instant Triage (0.2 ms)**:
   - Use the `classify_and_route_task` tool (or instant regex heuristic) to determine the recommended tier.
   - **If classified as Low (T0/T1)**: Call the MCP tool `call_mcp_tool(ServerName="tiered-router", ToolName="execute_local_task", Arguments={"prompt": ...})`.
     - The active local model in LM Studio executes the task offline at zero token cost.
     - **Auto-Healing Loop (0 cloud tokens)**: Code generated locally must be strictly idiomatic to the target language (zero lazy `.clone()`, no unwrap in production paths, idiomatic iterators). Immediately validate generated code using native linters/compilers (`rtk cargo check`, `rtk cargo test`, `tsc --noEmit`). If errors arise, pass the compiler diagnostic back to `execute_local_task(context=error)` to fix locally. Escalate only after 2 local failures.
     - Verify or integrate the returned code/text.
   - **If classified as Medium (T2)**: Delegate to cloud executor (`task-runner-medium` or self/subagent).
   - **If classified as High (T3)**: Keep execution in the primary session or consult `consultant-high`.

2. **Uncertainty Evaluation & Automatic Escalation**:
   - If `execute_local_task` returns an error or warning (e.g. LM Studio not running or no model loaded), immediately fallback to cloud execution (`flash` / `pro`).
   - If the local model output is incomplete, contains errors, or expresses doubt, inspect with `evaluate_task_uncertainty` and escalate to Medium.
   - If Medium encounters an architectural roadblock, format consultation with `format_high_consultation` and consult High.

3. **Context Preservation & Headroom Compression Protocol**:
   - During escalation, never restart from scratch. Pass accumulated code findings, investigated files, and error traces to the higher tier.
   - **Mandatory Headroom Compression (>80 lines)**: For any payload (file reads, compiler logs, `git diff`) exceeding 80 lines or 800 tokens, you MUST invoke `call_mcp_tool(ServerName="headroom", ToolName="headroom_compress", Arguments={"content": ...})`. Reason on the compressed output and retain the hash for retrieval (`headroom_retrieve`).

---

## 3. Standard Execution Flows & Mandatory Local Audit

### Mandatory Post-Dev / Post-Fix Merciless Local Audit (0 Cloud Tokens)
Across **every project**, immediately after completing any code development, refactoring, or bug fix (and before any git commit or final validation):
1. **Trigger Local Audit**: Collect the `git diff` and invoke the local model via `execute_local_task` (or skill `merciless-local-audit`). Prompt the local model to act as a merciless, ultra-strict auditor checking for language idiomaticity, boundary errors, silent failures, race conditions, and missing tests.
2. **Auto-Healing**: If the audit returns `[BLOCKING]` defects, resolve them immediately using the local auto-healing loop.
3. **Escalation**: Only proceed to commit validation or user feedback once the audit verdict is clean (`APPROVED`). If LM Studio is offline, warn the user and fallback to a cloud subagent.

### Standard On-Ramps
Organize tasks along clear pathways rather than executing isolated ad-hoc commands:

- **Main Flow (Feature: Concept to Production)**:
  1. *Framing & Spec*: Draft architecture artifact / spec (`RequestFeedback: true`).
  2. *Implementation*: Offload standalone modules/tests to Tier 1 local model or Tier 2 subagent.
  3. *Merciless Local Audit*: Mandatory zero-token audit of `git diff` via `execute_local_task`.
  4. *Adversarial Verification*: Trigger `grill-orchestrator` (Cross-family Maker vs Inquisitor, black-box red-team test generation) for critical components.
  5. *Validation & Mutation Testing*: Native test suite (`rtk cargo test`, `tsc`) + mutation testing oracle (`cargo mutants --in-diff` for Rust).
  6. *Phase Boundary*: Choose `/compact` or `HANDOFF` before starting next phase.

- **On-Ramp Bug / Regression**:
  1. Reproduce with minimal isolated test case (Tier 1 local model at 0 tokens).
  2. Implement fix with local auto-healing loop.
  3. *Merciless Local Audit*: Mandatory local audit of fix diff via `execute_local_task`.
  4. Validate test pass and run clippy/linter.

- **On-Ramp Architecture & Codebase Health**:
  1. Retrieve architectural memory via Headroom (`headroom_retrieve`).
  2. Identify coupling / refactoring seams.
  3. Enter Main Flow via `grill-orchestrator` if high blast radius.

---

## 4. The "Recommend and Stop" Principle

When advising the user on an architectural dilemma, debugging approach, or workflow selection:
- Clearly state the recommended flow, why competing alternatives were ruled out, and the next command/step.
- **Stop and await confirmation** before creating or modifying code files, running heavy builds, or firing autonomous multi-agent loops prematurely.

---

## 5. Git Commit Hygiene & Quality Gates (Universal Standard)

- **Atomic Commits**: Sliced into small, single-purpose changes. Never mix a bug fix with a new feature or unaligned changes.
- **Bisectability**: Every commit must independently compile and pass CI/tests.
- **CI Validation**: Run tests and linter before staging/committing.
- **README & Doc Sync**: Update README and documentation alongside any code/feature changes.
- **Zero Autonomous Commits or Pushes & Strict Separation**:
  - Never bundle commit and push in a single request. They are two independent gates.
  - **Gate A (Local Commit)**: Display unified diff, propose message, and invoke `ask_question`:
    - Question: `"Souhaitez-vous enregistrer ce commit atomique localement ?"`
    - Options: `["(Recommended) Oui, enregistrer le commit local (sans push)", "Ajuster le message ou le périmètre du commit", "Annuler / apporter d'autres modifications"]`
  - **Gate B (Remote Push - Dedicated Turn)**: Keep commits local by default to preserve CI runner quotas. In a separate turn, ask via `ask_question`:
    - Question: `"Le commit local est enregistré. Souhaitez-vous pousser les modifications vers origin/main ?"`
    - Options: `["(Recommended) Non, conserver en local (préserve les quotas des runners CI)", "Oui, pousser vers origin/main"]`

---

## 6. Mandatory Phase Tracking & Detailed Testing Protocol

Every development or bug fixing interaction MUST explicitly display progress through clear banners:
- **Zero Silent Tool Calls (MANDATORY FIRST-TOKEN OUTPUT)**: The agent is strictly forbidden from invoking tools silently without preceding visible prose.
- **First Visible Markdown Token**: At the very start of every turn and **BEFORE invoking any tool**, you MUST output visible text in the chat declaring the active phase banner in Markdown:
  `### [PHASE 1/5 : CONCEPTION & DÉVELOPPEMENT]` (or active phase `[PHASE 2/5]`, `[PHASE 3/5]`, etc.)
  followed by a one-sentence summary of the action being taken.
- The 5 phases:
  1. `### [PHASE 1/5 : CONCEPTION & DÉVELOPPEMENT]` : Architecture & implementation.
  2. `### [PHASE 2/5 : VALIDATION NATIVE & TESTS CI]` : Full toolchain execution (`cargo test && cargo clippy`, `tsc`, `pytest`).
  3. `### [PHASE 3/5 : AUDIT LOCAL IMPITOYABLE (0 TOKENS)]` : LM Studio adversarial review via `execute_local_task`.
  4. `### [PHASE 4/5 : AUTO-HEALING & CORRECTIFS]` : Direct resolution of any blocking findings.
  5. `### [PHASE 5/5 : RAPPORT DÉTAILLÉ & PROTOCOLE DE TEST]` : Comprehensive report including exact automated test commands and step-by-step manual validation scenarios before Gate 3.
