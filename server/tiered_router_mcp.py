#!/usr/bin/env python3
"""
Antigravity Tiered Model Router MCP Server (LM Studio Edition)
--------------------------------------------------------------
A lightweight, zero-dependency MCP (Model Context Protocol) server running over stdio.
Implements the hybrid routing engine:
  1. Primary: Deterministic semantic heuristic engine (instant, offline, zero tokens)
  2. Fallback: LM Studio decision model via OpenAI-compatible API (/v1/chat/completions)

Exposes tools:
  - classify_and_route_task: Classifies complexity (Low/Medium/High) and suggests tier.
  - evaluate_task_uncertainty: Analyzes subagent execution for doubt, failure, or incomplete work.
  - format_high_consultation: Structures consultation prompts for the High tier consultant.
  - get_tier_status: Reports health of LM Studio, available models, and routing statistics.
"""

import sys
import json
import re
import os
import subprocess
import time
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, Tuple

# LM Studio runs on localhost:1234 by default (OpenAI-compatible API)
LMSTUDIO_HOST = os.environ.get("LMSTUDIO_HOST", "http://127.0.0.1:1234")
DECISION_MODEL = os.environ.get("LMSTUDIO_DECISION_MODEL", "qwen/qwen3.6-35b-a3b")
CONNECT_TIMEOUT_SEC = 4.0


def log(msg: str) -> None:
    """Log to stderr to avoid corrupting stdio JSON-RPC output."""
    sys.stderr.write(f"[TieredRouter] {msg}\n")
    sys.stderr.flush()


def check_lmstudio_available(host: str = LMSTUDIO_HOST) -> Tuple[bool, list]:
    """Check if LM Studio is running and list available models."""
    try:
        req = urllib.request.Request(f"{host}/v1/models")
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_SEC) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("id", "") for m in data.get("data", [])]
                return True, models
    except Exception:
        pass
    return False, []


def detect_active_model(host: str = LMSTUDIO_HOST) -> Tuple[Optional[str], Optional[str]]:
    """
    Detect the LLM currently loaded and active in LM Studio.
    Checks `lms ps --json` first for the exact active model key.
    Falls back to `/v1/models` HTTP API.
    Returns: (active_model_id, warning_message_if_none)
    """
    try:
        res = subprocess.run(['lms', 'ps', '--json'], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            for item in data:
                if item.get('type') == 'llm' and item.get('modelKey'):
                    return item.get('modelKey'), None
            return None, "Attention : LM Studio est actif mais aucun modèle LLM n'est chargé en mémoire. Veuillez charger un modèle dans LM Studio."
    except Exception:
        pass

    # Fallback to HTTP API
    try:
        req = urllib.request.Request(f"{host}/v1/models")
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_SEC) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("id") for m in data.get("data", []) if "embed" not in m.get("id", "").lower()]
                if models:
                    return models[0], None
                return None, "Attention : LM Studio est actif mais aucun modèle LLM n'est disponible."
    except Exception as e:
        return None, f"Attention : LM Studio n'est pas joignable sur {host} ({e})."

    return None, "Attention : Impossible de détecter un modèle actif dans LM Studio."


def execute_local_task(
    prompt: str,
    system_prompt: Optional[str] = None,
    context: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    host: str = LMSTUDIO_HOST
) -> Dict[str, Any]:
    """
    Executes a task locally on the active LM Studio model (0 cloud tokens).
    Handles reasoning models (Qwen 3.6 / R1) and standard LLMs seamlessly.
    """
    active_model, warning = detect_active_model(host)
    if not active_model:
        return {
            "success": False,
            "error": warning or "No LLM model is currently loaded in LM Studio.",
            "output": "",
            "action": "Please load a model in LM Studio (e.g. Qwen 3.6) and retry, or fallback to cloud execution."
        }

    sys_content = system_prompt or (
        "You are an expert, concise software engineer. Provide direct, working code and clear explanations. "
        "Do not output unrequested chatter."
    )

    full_prompt = prompt
    if context:
        full_prompt = f"### CONTEXT:\n{context}\n\n### TASK:\n{prompt}"

    messages = [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": full_prompt}
    ]

    payload = {
        "model": active_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }

    t0 = time.time()
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{host}/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AntigravityLocalExecutor/1.0"
            }
        )
        with urllib.request.urlopen(req, timeout=90.0) as resp:
            if resp.status == 200:
                res = json.loads(resp.read().decode("utf-8"))
                elapsed = round(time.time() - t0, 2)
                choice = res.get("choices", [{}])[0]
                msg = choice.get("message", {})
                content = msg.get("content", "").strip()
                reasoning = msg.get("reasoning_content", "").strip()
                usage = res.get("usage", {})

                # If content is empty (e.g. reasoning model hit limit), fallback to reasoning output
                final_output = content if content else reasoning

                return {
                    "success": True,
                    "model_used": active_model,
                    "elapsed_seconds": elapsed,
                    "output": final_output,
                    "has_reasoning": bool(reasoning),
                    "tokens_used": usage.get("total_tokens", 0),
                    "finish_reason": choice.get("finish_reason", "unknown")
                }
            else:
                return {
                    "success": False,
                    "error": f"LM Studio returned HTTP {resp.status}",
                    "output": ""
                }
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        log(f"execute_local_task failed after {elapsed}s: {e}")
        return {
            "success": False,
            "error": f"Local model execution failed: {e}",
            "elapsed_seconds": elapsed,
            "output": "",
            "action": "Escalate to cloud model (flash or pro)."
        }


def query_lmstudio_decision(prompt: str, host: str = LMSTUDIO_HOST) -> Optional[Dict[str, Any]]:
    """
    Query LM Studio for task classification using chat completions API.
    Sends a structured prompt and parses the JSON response for tier classification.
    Falls back to heuristic if LM Studio is unavailable.
    """
    url = f"{host}/v1/chat/completions"
    
    classification_prompt = f"""You are a task classifier for a tiered AI execution system. Analyze the following task and classify it into one of three tiers.

Task: {prompt[:3000]}

Classification criteria:
- LOW: Simple, read-only, localized tasks. File viewing, grep searches, syntax checks, documentation typos, single-file minor fixes, status checks, "what is this", "list files", "find pattern".
- MEDIUM: Standard programming tasks. Feature development, writing code, unit tests, bug fixes, refactoring, multi-file changes that are well-scoped.
- HIGH: Complex architecture, multi-repository work, security audits, ambiguous planning, breaking changes, distributed systems, deep debugging of elusive bugs.

Respond with ONLY valid JSON, no markdown, no explanation:
{{"tier": "low"|"medium"|"high", "confidence": 0.0-1.0, "reason": "brief explanation"}}"""

    payload = {
        "model": DECISION_MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise task classifier. Always respond with valid JSON only."},
            {"role": "user", "content": classification_prompt}
        ],
        "temperature": 0.0,
        "max_tokens": 100,
        "stream": False
    }

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AntigravityRouter/1.0"
            }
        )
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_SEC * 5) as resp:
            if resp.status == 200:
                result = json.loads(resp.read().decode("utf-8"))
                choice = result.get("choices", [{}])[0]
                msg = choice.get("message", {})
                # Qwen3.6-35b-a3b puts text in reasoning_content, not content
                content = msg.get("reasoning_content", "") or msg.get("content", "")
                
                # Parse JSON from the response (strip markdown code blocks if present)
                content = content.strip()
                if content.startswith("```"):
                    content = re.sub(r"^```(?:json)?\n?|\n?```$", "", content, flags=re.MULTILINE).strip()
                
                parsed = json.loads(content)
                tier = parsed.get("tier", "medium").lower()
                confidence = float(parsed.get("confidence", 0.85))
                reason = parsed.get("reason", "Classified by LM Studio decision model.")

                if tier not in ["low", "medium", "high"]:
                    tier = "medium"

                savings_map = {
                    "low": "85-92% (Fastest response, minimal tokens)",
                    "medium": "50-65% (Balanced speed and reasoning)",
                    "high": "0% (Full reasoning depth, maximum context)"
                }

                return {
                    "tier": tier,
                    "confidence": round(confidence, 2),
                    "engine_used": "lmstudio_decision",
                    "reason": reason,
                    "suggested_subagent": f"task-runner-{tier}" if tier != "high" else "consultant-high",
                    "estimated_token_savings": savings_map[tier]
                }
    except Exception as e:
        log(f"LM Studio decision model skipped: {e}. Using heuristic fallback...")

    return None


def heuristic_classifier(prompt: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Deterministic, offline local heuristic classifier.
    Fast, reliable, zero-latency, zero token consumption.
    """
    text = prompt.lower()
    word_count = len(prompt.split())

    # Check for file path patterns to prevent keyword collision
    file_path_pattern = r"[\w./\\-]+\.(?:md|ts|tsx|js|jsx|json|py|html|css|yml|yaml|toml|sql|rs|c|h|cpp|hpp)\b"
    file_paths_found = re.findall(file_path_pattern, text)
    sanitized_text = text
    for fp in file_paths_found:
        sanitized_text = sanitized_text.replace(fp, "filepath_token")

    # High complexity / architecture signals
    high_keywords = [
        r"\barchitecture\b", r"\bredesign\b", r"\bsystem design\b",
        r"\bsecurity audit\b", r"\bvulnerability\b", r"\bdeadlock\b",
        r"\brace condition\b", r"\bdistributed\b", r"\bbenchmark.*architecture\b",
        r"\bstrategy\b", r"\bconsultant\b", r"\bcomplex debugging\b",
        r"\bbreaking change\b", r"\bbreaking changes\b", r"\bconsensus\b",
        r"\bmigration\b", r"\brestructure\b", r"\brearchitect\b",
        r"\bsecurity overhaul\b", r"\bdeep diagnostic\b", r"\broot cause\b"
    ]

    # Medium implementation signals (active coding verbs)
    medium_keywords = [
        r"\bimplement\b", r"\bcreate\b", r"\bdevelop\b", r"\bdevelopp\b",
        r"\bendpoint\b", r"\bcontroller\b", r"\bservice\b", r"\btests?\b",
        r"\brefactor\b", r"\bfix.*bug\b", r"\baddd\b", r"\bmigrat\b",
        r"\bbuild\b", r"\bwebhook\b", r"\bcrud\b", r"\bworker\b",
        r"\bqueue\b", r"\bfunction\b", r"\bwrite\b", r"\bcode\b",
        r"\bimplement\b", r"\bfeature\b", r"\bunit test\b",
        r"\brefactor\b", r"\bmodify\b", r"\bchange\b"
    ]

    # Multi-file or wide-scope signals
    wide_scope_signals = [
        r"\ball\b.*\bfiles?\b", r"\bevery\b.*\bfile\b",
        r"\bproject\b.*\bwide\b", r"\bacross.*\bproject\b",
        r"\bwhole\b.*\bcodebase\b", r"\bentire\b.*\bproject\b",
        r"\bmultiple.*\bfiles?\b", r"\bmany.*\bfiles?\b"
    ]

    # Low complexity / quick inspection signals
    low_keywords = [
        r"\blist\b", r"\bview\b", r"\bread\b", r"\blea\b",
        r"\bgrep\b", r"\bsearch\b", r"\bfind\b", r"\bwhere\b",
        r"\bwhat is\b", r"\bformat\b", r"\blint\b",
        r"\bsyntax\b", r"\btypo\b", r"\bdocstring\b",
        r"\bquick test\b", r"\bstatus\b", r"\bshow\b",
        r"\bprint\b", r"\bcheck\b", r"\bverify\b", r"\bconfirm\b",
        r"\bexplain\b", r"\bdescribe\b", r"\bsummary\b",
        r"\breadme\b", r"\bdemo\b", r"\bsetup\b"
    ]

    # Scoring
    high_hits = sum(1 for kw in high_keywords if re.search(kw, sanitized_text))
    med_hits = sum(1 for kw in medium_keywords if re.search(kw, text))
    low_hits = sum(1 for kw in low_keywords if re.search(kw, text))
    has_wide_scope = any(re.search(ws, text) for ws in wide_scope_signals)

    # Pure search/skip detection
    is_pure_search = (low_hits >= 1 and med_hits == 0 and any(re.search(s, text) for s in [r"\bgrep\b", r"\bsearch\b", r"\blist\b", r"\bfind\b"]))
    if is_pure_search:
        has_wide_scope = False

    # Decision logic: writing > reading; wide scope escalates
    if high_hits >= 2 or (high_hits >= 1 and word_count > 40):
        tier = "high"
        confidence = min(0.95, 0.70 + (high_hits * 0.10))
        reason = f"Identified architectural or high-complexity markers ({high_hits} indicators)."
    elif has_wide_scope or (med_hits >= 1 and high_hits == 0) or word_count > 50:
        tier = "medium"
        confidence = min(0.95, 0.75 + (med_hits * 0.08))
        if has_wide_scope:
            reason = "Wide project scope / multi-file modification detected. Promoted to Medium tier."
        else:
            reason = f"Standard implementation/coding task ({med_hits} action indicators)."
    elif low_hits >= 1 and high_hits == 0 and med_hits == 0 and not has_wide_scope and word_count < 40:
        tier = "low"
        confidence = min(0.95, 0.75 + (low_hits * 0.08))
        reason = f"Simple/read-only/triage markers ({low_hits} indicators). Ideal for Low tier to save tokens."
    else:
        tier = "medium"
        confidence = 0.85
        reason = "Standard development task. Routed to Medium executor for reliable delivery."

    subagent_map = {
        "low": "task-runner-low",
        "medium": "task-runner-medium",
        "high": "consultant-high"
    }

    savings_map = {
        "low": "85-92% (Fastest response, minimal tokens)",
        "medium": "50-65% (Balanced speed and reasoning)",
        "high": "0% (Full reasoning depth, maximum context)"
    }

    return {
        "tier": tier,
        "confidence": round(confidence, 2),
        "engine_used": "local_heuristic",
        "reason": reason,
        "suggested_subagent": subagent_map[tier],
        "estimated_token_savings": savings_map[tier]
    }


def route_task(prompt: str, context: Optional[Dict[str, Any]] = None, use_neural: bool = False) -> Dict[str, Any]:
    """
    Intelligent Hybrid Routing:
    1. Primary: Deterministic heuristic engine (instant 0.2ms, offline, zero tokens)
    2. Optional: LM Studio decision model if use_neural=True is explicitly set
    """
    if use_neural:
        lmstudio_result = query_lmstudio_decision(prompt)
        if lmstudio_result:
            return lmstudio_result

    return heuristic_classifier(prompt, context)


def evaluate_uncertainty(subagent_output: str, current_tier: str = "low") -> Dict[str, Any]:
    """
    Examines subagent output for uncertainty signals, errors, or incompleteness.
    Determines if execution must escalate to the next tier.
    """
    text = subagent_output.lower()

    uncertainty_patterns = [
        "i am unsure", "i am not certain", "cannot determine", "uncertain",
        "failed to resolve", "could not fix", "unknown error",
        "i do not know", "i don't know", "i'm not sure",
        "not possible", "impossible", "no way to",
        "error:", "exception:", "traceback", "stacktrace",
        "syntaxerror", "typeerror", "referenceerror", "test failed",
        "je ne sais pas", "je ne peux pas", "impossible de",
        "incertain", "doute", "hésitation"
    ]

    incomplete_markers = [
        "todo:", "fixme:", "implement later", "to be implemented",
        "placeholder", "[to be implemented]", "[à implémenter]",
        "[faut implémenter]", "[à faire]"
    ]

    matched_uncertainty = [p for p in uncertainty_patterns if p in text]
    matched_incomplete = [m for m in incomplete_markers if m in text]

    has_issue = len(matched_uncertainty) > 0 or len(matched_incomplete) > 0

    if has_issue:
        target_tier = "medium" if current_tier == "low" else "high"
        confidence = 0.90 if len(matched_uncertainty) >= 2 else 0.75
        return {
            "should_escalate": True,
            "current_tier": current_tier,
            "escalate_to": target_tier,
            "confidence_score": confidence,
            "reason": f"Detected uncertainty / failure indicators: {matched_uncertainty + matched_incomplete}. Automatically cascading to {target_tier.upper()}.",
            "action": f"Delegate to subagent task-runner-{target_tier}" if target_tier != "high" else "Delegate to consultant-high"
        }
    else:
        return {
            "should_escalate": False,
            "current_tier": current_tier,
            "escalate_to": None,
            "confidence_score": 0.92,
            "reason": "Subagent output demonstrates clear resolution without hesitation or critical errors.",
            "action": "Proceed with current workflow."
        }


def format_consultation(task: str, current_findings: str, dilemma: str) -> Dict[str, Any]:
    """Structures an inquiry for the High tier consultant."""
    template = (
        "### [CONSULTATION REQUEST: HIGH TIER ORACLE]\n\n"
        f"**Original Task:**\n{task}\n\n"
        f"**Findings / Progress by Lower Tier:**\n{current_findings}\n\n"
        f"**Architectural Dilemma / Uncertainty:**\n{dilemma}\n\n"
        "**Expected High Tier Response:**\n"
        "- Clear strategic decision or architectural guidance.\n"
        "- Root-cause analysis of the blockers.\n"
        "- Direct delegation instructions back to Medium or step-by-step resolution."
    )
    return {
        "formatted_prompt": template,
        "target_agent": "consultant-high",
        "target_model": DECISION_MODEL
    }


# ==============================================================================
# MCP Protocol (JSON-RPC 2.0 over stdio)
# ==============================================================================

TOOLS = [
    {
        "name": "execute_local_task",
        "description": "Executes an easy/mechanical task, code generation, refactoring, or explanation directly on the active local model in LM Studio. Consumes zero cloud tokens.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The exact instruction, code task, or query to execute on the local model."
                },
                "system_prompt": {
                    "type": "string",
                    "description": "Optional custom system instructions for the local model."
                },
                "context": {
                    "type": "string",
                    "description": "Optional surrounding code, file snippets, or error messages."
                },
                "max_tokens": {
                    "type": "integer",
                    "default": 2048,
                    "description": "Maximum tokens to generate (default: 2048)."
                },
                "temperature": {
                    "type": "number",
                    "default": 0.2,
                    "description": "Sampling temperature (default: 0.2 for deterministic code)."
                }
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "classify_and_route_task",
        "description": "Analyzes a prompt or task description and determines the optimal model tier (Low, Medium, High) to maximize speed and minimize token costs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The user prompt, subtask description, or command to triage."
                },
                "context": {
                    "type": "object",
                    "description": "Optional context information (file count, workspace state, etc.)."
                }
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "evaluate_task_uncertainty",
        "description": "Evaluates a subagent's output or execution result for uncertainty, hesitation, or failure, triggering escalation (Low -> Medium -> High).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subagent_output": {
                    "type": "string",
                    "description": "The raw output, answer, or error log generated by the subagent."
                },
                "current_tier": {
                    "type": "string",
                    "enum": ["low", "medium"],
                    "default": "low",
                    "description": "The tier that produced the output (low or medium)."
                }
            },
            "required": ["subagent_output"]
        }
    },
    {
        "name": "format_high_consultation",
        "description": "Formats a structured consultation inquiry for the High tier consultant when lower tiers encounter architectural doubt or complex blockers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The original goal or task."
                },
                "current_findings": {
                    "type": "string",
                    "description": "What the lower tier already discovered or attempted."
                },
                "dilemma": {
                    "type": "string",
                    "description": "The specific doubt, question, or trade-off requiring High tier advice."
                }
            },
            "required": ["task", "current_findings", "dilemma"]
        }
    },
    {
        "name": "get_tier_status",
        "description": "Returns the status of LM Studio connectivity, active loaded model, available models, and router configurations.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]


def handle_tool_call(name: str, args: Dict[str, Any]) -> Any:
    if name == "execute_local_task":
        prompt = args.get("prompt", "")
        sys_prompt = args.get("system_prompt")
        context = args.get("context")
        max_tokens = args.get("max_tokens", 2048)
        temperature = args.get("temperature", 0.2)
        return execute_local_task(
            prompt=prompt,
            system_prompt=sys_prompt,
            context=context,
            max_tokens=max_tokens,
            temperature=temperature
        )
    elif name == "classify_and_route_task":
        prompt = args.get("prompt", "")
        ctx = args.get("context", {})
        return route_task(prompt, ctx)
    elif name == "evaluate_task_uncertainty":
        output = args.get("subagent_output", "")
        tier = args.get("current_tier", "low")
        return evaluate_uncertainty(output, tier)
    elif name == "format_high_consultation":
        task = args.get("task", "")
        findings = args.get("current_findings", "")
        dilemma = args.get("dilemma", "")
        return format_consultation(task, findings, dilemma)
    elif name == "get_tier_status":
        active_model, warning = detect_active_model()
        lm_running, available_models = check_lmstudio_available()
        return {
            "lmstudio_running": lm_running,
            "active_model": active_model,
            "available_models": available_models,
            "warning": warning,
            "lmstudio_host": LMSTUDIO_HOST,
            "routing_engine": "local_heuristic (default 0.2ms)",
            "tier_hierarchy": {
                "low": "Local LM Studio Model (execute_local_task) - 0 cloud tokens",
                "medium": "Task Runner Medium (Flash) - Main feature executor, tests, refactoring",
                "high": "Consultant High (Pro / Argon) - Strategic architect, complex edge cases"
            }
        }
    else:
        raise ValueError(f"Unknown tool: {name}")


def run_mcp_server():
    """Main loop handling JSON-RPC 2.0 messages from stdin."""
    log("Starting Antigravity Tiered Router MCP Server (LM Studio)...")
    lm_ok, models = check_lmstudio_available()
    log(f"LM Studio running: {lm_ok}, Models: {models}")

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError as err:
            log(f"Invalid JSON: {err}")
            continue

        msg_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            res = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "antigravity-tiered-router-lmstudio",
                        "version": "1.0.0"
                    }
                }
            }
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            res = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": TOOLS}
            }
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()

        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            try:
                result_data = handle_tool_call(tool_name, tool_args)
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result_data, indent=2, ensure_ascii=False)
                            }
                        ]
                    }
                }
            except Exception as e:
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32603,
                        "message": str(e)
                    }
                }
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()

        elif method == "ping":
            res = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()

        else:
            if msg_id is not None:
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}"
                    }
                }
                sys.stdout.write(json.dumps(res) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    run_mcp_server()
