---
name: tiered-router
description: Global Local-First tiered router and Strict Fail-Closed workflow. Guides task routing across Dual Local LM Studio (Maker Splash 27B / Inquisitor 35B) and cloud tiers, with <=150 LOC maker chunking and zero silent cloud fallback.
---

# Tiered Router — Operational Guide

Single source of truth for routing lives in `rules/AGENTS.md` (Permanent Local-First across `GREEN`/`YELLOW`/`RED`, Strict Fail-Closed). This skill summarizes the operational MCP workflow.

## 1. Core MCP Tools

1. **Triage (`classify_and_route_task`)**: Optional heuristic check when task complexity (T0–T3) is ambiguous.
2. **Local Execution (`execute_local_task`)** — **0 cloud tokens**, serialized via FIFO queue (`~/.gemini/tmp/lmstudio_queue.lock`):
   - `role="maker"` (**Qwen3.8 27B Splash**): modular functions, algorithms, unit tests, fixtures, boilerplate, docstrings, commit messages.
   - **Chunking rule**: target at most **1 file or 1 focused function/slice (`<=150 LOC`)** per call to prevent `finish_reason: "length"` truncations.
   - `role="inquisitor"` (**Qwen3.6 35B A3B**): mandatory Phase 3 merciless diff audit (`merciless-local-audit` skill).
3. **Uncertainty Check (`evaluate_task_uncertainty`)**: Inspect local outputs for errors/hesitation; retry locally with diagnostics (`context=error`) up to 2 times before asking the user via `ask_question`.
4. **High Consultation (`format_high_consultation`)**: Package accumulated findings concisely before T3 architectural escalation.
5. **Headroom (`headroom_compress` / `headroom_retrieve`)**: Mandatory for payloads >80 lines / >800 tokens.

## 2. Strict Fail-Closed Policy

If `execute_local_task` fails, times out, or returns `allow_cloud_fallback: false`:
- **NEVER** fallback silently to `flash`, `pro`, or primary cloud generation — in ANY budget state (`GREEN`, `YELLOW`, `RED`).
- **STOP** and invoke `ask_question` (retry local vs explicit user consent for cloud).

## 3. Standard Flows

1. **Feature**: Spec (`RequestFeedback: true`) -> local `maker` (`<=150 LOC`/call) -> Phase 2 native tests -> Phase 3 local `inquisitor` -> `grill-orchestrator` (T3 critical only) -> Gate A/B (`idiomatic-craft` §4) -> mandatory Phase Boundary (`/compact` / `/clear`).
2. **Bug / Regression**: Failing test (`maker`) -> local fix + auto-healing -> Phase 3 `inquisitor` -> native validation -> Gate A/B -> Phase Boundary.
3. **Architecture / Health**: `headroom_retrieve` -> identify seams -> Recommend & Stop before editing.
