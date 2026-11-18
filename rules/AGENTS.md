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
     - Verify or integrate the returned code/text.
   - **If classified as Medium (T2)**: Delegate to cloud executor (`task-runner-medium` or self/subagent).
   - **If classified as High (T3)**: Keep execution in the primary session or consult `consultant-high`.

2. **Uncertainty Evaluation & Automatic Escalation**:
   - If `execute_local_task` returns an error or warning (e.g. LM Studio not running or no model loaded), immediately fallback to cloud execution (`flash` / `pro`).
   - If the local model output is incomplete, contains errors, or expresses doubt, inspect with `evaluate_task_uncertainty` and escalate to Medium.
   - If Medium encounters an architectural roadblock, format consultation with `format_high_consultation` and consult High.

3. **Context Preservation**:
   - During escalation, never restart from scratch. Pass accumulated code findings, investigated files, and error traces to the higher tier.
