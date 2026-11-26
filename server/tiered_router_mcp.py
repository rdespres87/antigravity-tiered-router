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

import contextlib
import sys
import json
import math
import re
import os
import socket
import subprocess
import time
import urllib.request
import urllib.error
from typing import Dict, Any, IO, Iterator, List, Optional, Tuple

if sys.platform == "win32":
    import msvcrt  # type: ignore[import]
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[import]
    msvcrt = None  # type: ignore[assignment]

# LM Studio runs on localhost:1234 by default (OpenAI-compatible API)
LMSTUDIO_HOST = os.environ.get("LMSTUDIO_HOST", "http://127.0.0.1:1234")
DECISION_MODEL = os.environ.get("LMSTUDIO_DECISION_MODEL", "qwen/qwen3.6-35b-a3b")
CONNECT_TIMEOUT_SEC = 4.0
LMSTUDIO_LOCK_PATH = os.environ.get(
    "LMSTUDIO_LOCK_PATH",
    os.path.expanduser("~/.gemini/tmp/lmstudio_queue.lock"),
)
FAIL_CLOSED_ACTION = (
    "STOP: Do NOT fallback to cloud automatically. "
    "Wait/retry local execution or ask the user via ask_question before using any cloud model."
)


def _env_float(name: str, default: float) -> float:
    """Parse a positive finite float from environment variables with safe fallback."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
        return val if math.isfinite(val) and val > 0 else default
    except ValueError:
        return default


LOCAL_QUEUE_TIMEOUT_SEC = _env_float("LMSTUDIO_QUEUE_TIMEOUT_SEC", 900.0)
LOCAL_IDLE_TIMEOUT_SEC = _env_float("LMSTUDIO_IDLE_TIMEOUT_SEC", 180.0)
LOCAL_TOTAL_TIMEOUT_SEC = _env_float("LMSTUDIO_TIMEOUT_SEC", 600.0)


class LocalQueueTimeoutError(TimeoutError):
    """Raised when waiting for the cross-process LM Studio queue lock times out."""


def _try_lock_exclusive(fd: IO[str]) -> bool:
    """Attempt a non-blocking exclusive advisory lock on an open file descriptor."""
    if sys.platform == "win32" and msvcrt is not None:
        try:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    if fcntl is not None:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False
    return True


def _release_lock(fd: IO[str]) -> None:
    """Release an advisory lock held on an open file descriptor."""
    if sys.platform == "win32" and msvcrt is not None:
        try:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    elif fcntl is not None:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


@contextlib.contextmanager
def _acquire_lmstudio_lock(
    lock_path: Optional[str] = None,
    queue_timeout: Optional[float] = None,
    poll_interval: float = 0.25,
) -> Iterator[float]:
    """Serialize cross-process LM Studio calls via an exclusive file lock.

    Yields queue_wait_seconds once acquired. Raises LocalQueueTimeoutError if
    queue_timeout elapses before the lock becomes available.
    """
    effective_path = lock_path or LMSTUDIO_LOCK_PATH
    effective_timeout = (
        float(queue_timeout)
        if isinstance(queue_timeout, (int, float))
        and not isinstance(queue_timeout, bool)
        and math.isfinite(float(queue_timeout))
        and queue_timeout > 0
        else LOCAL_QUEUE_TIMEOUT_SEC
    )
    os.makedirs(os.path.dirname(effective_path) or ".", exist_ok=True)
    fd = open(effective_path, "a+", encoding="utf-8")
    start = time.monotonic()
    acquired = False
    try:
        while True:
            if _try_lock_exclusive(fd):
                acquired = True
                break
            elapsed = time.monotonic() - start
            if elapsed >= effective_timeout:
                raise LocalQueueTimeoutError(
                    f"LM Studio queue wait timed out after {effective_timeout:.1f}s"
                )
            time.sleep(min(poll_interval, max(0.01, effective_timeout - elapsed)))
        yield round(time.monotonic() - start, 2)
    finally:
        if acquired:
            _release_lock(fd)
        try:
            fd.close()
        except OSError:
            pass


def is_lmstudio_lock_busy(lock_path: Optional[str] = None) -> bool:
    """Return True if another process currently holds the LM Studio queue lock."""
    effective_path = lock_path or LMSTUDIO_LOCK_PATH
    if not os.path.exists(effective_path):
        return False
    try:
        fd = open(effective_path, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        if _try_lock_exclusive(fd):
            _release_lock(fd)
            return False
        return True
    finally:
        try:
            fd.close()
        except OSError:
            pass


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


def get_loaded_models(host: str = LMSTUDIO_HOST) -> Tuple[list, Optional[str]]:
    """
    Get all LLM models currently loaded in LM Studio.
    Returns: (list_of_model_keys, warning_message_if_none)
    """
    loaded = []
    try:
        res = subprocess.run(['lms', 'ps', '--json'], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            for item in data:
                if item.get('type') == 'llm' and item.get('modelKey'):
                    loaded.append(item.get('modelKey'))
            if loaded:
                return loaded, None
    except Exception:
        pass

    # Fallback to HTTP API /v1/models
    try:
        req = urllib.request.Request(f"{host}/v1/models")
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_SEC) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("id") for m in data.get("data", []) if "embed" not in m.get("id", "").lower()]
                if models:
                    return models, None
                return [], "Attention : LM Studio est actif mais aucun modèle LLM n'est disponible."
    except Exception as e:
        return [], f"Attention : LM Studio n'est pas joignable sur {host} ({e})."

    return [], "Attention : Impossible de détecter un modèle actif dans LM Studio."


def resolve_local_model(
    requested_model: Optional[str] = None,
    requested_role: Optional[str] = None,
    prompt: str = "",
    system_prompt: Optional[str] = None,
    fast: bool = False,
    host: str = LMSTUDIO_HOST
) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Resolves target model and assigned role among loaded models in LM Studio.
    Returns: (resolved_model_key, assigned_role, error_warning)
    Roles:
      - 'maker' (Code generation, tests, fast implementation) -> Qwen3.8 Splash
      - 'inquisitor' (Adversarial code audit, logic, edge cases) -> Qwen3.6 35B A3B
    """
    loaded, warning = get_loaded_models(host)
    if not loaded:
        return None, "none", warning

    maker_cand = None
    inquisitor_cand = None

    for m in loaded:
        m_lower = m.lower()
        if "splash" in m_lower or "27b" in m_lower:
            maker_cand = m
        elif "35b" in m_lower or "a3b" in m_lower or "moe" in m_lower:
            inquisitor_cand = m

    if not maker_cand and loaded:
        maker_cand = loaded[0]
    if not inquisitor_cand and loaded:
        inquisitor_cand = loaded[-1] if len(loaded) > 1 else loaded[0]

    # 1. Explicit model requested
    if requested_model:
        req_low = requested_model.lower().strip()
        for m in loaded:
            if req_low == m.lower() or req_low in m.lower():
                role = "maker" if m == maker_cand else ("inquisitor" if m == inquisitor_cand else "custom")
                return m, role, None
        if req_low in ("splash", "27b", "maker"):
            return maker_cand, "maker", None
        if req_low in ("35b", "a3b", "inquisitor", "inquisiteur", "audit"):
            return inquisitor_cand, "inquisitor", None

    # 2. Explicit role requested
    if requested_role:
        r_low = requested_role.lower().strip()
        if r_low in ("maker", "code", "fast", "worker"):
            return maker_cand, "maker", None
        if r_low in ("inquisitor", "inquisiteur", "audit", "review", "adversarial"):
            return inquisitor_cand, "inquisitor", None

    # 3. Semantic / Contextual detection
    combined_text = f"{prompt} {system_prompt or ''}".lower()
    audit_indicators = [
        "audit", "inquisit", "inquisiteur", "merciless", "adversarial",
        "blocking defects", "code smell", "failles", "vulnérabilité",
        "diff audit", "security audit", "review diff"
    ]
    if any(ind in combined_text for ind in audit_indicators):
        return inquisitor_cand, "inquisitor", None

    # Default to maker for fast or general implementation
    return maker_cand, "maker", None


def detect_active_model(host: str = LMSTUDIO_HOST) -> Tuple[Optional[str], Optional[str]]:
    """Backward-compatible helper returning the primary active model."""
    loaded, warning = get_loaded_models(host)
    if loaded:
        return loaded[0], None
    return None, warning


def _is_timeout_error(exc: BaseException) -> bool:
    """Return True if the exception represents a socket or streaming timeout."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return True
        if "timed out" in str(exc.reason).lower():
            return True
    return False


def _find_fallback_model(failed_model: str, host: str = LMSTUDIO_HOST) -> Optional[str]:
    """Pick an alternative loaded model, preferring MoE / 35B / A3B variants for speed."""
    loaded, _ = get_loaded_models(host)
    candidates = [m for m in loaded if m != failed_model]
    if not candidates:
        return None
    preferred = [
        m for m in candidates
        if any(marker in m.lower() for marker in ("moe", "35b", "a3b"))
    ]
    return preferred[0] if preferred else candidates[0]


def _stream_chat_completion(
    host: str,
    payload: Dict[str, Any],
    idle_timeout: float,
    total_timeout: float,
) -> Tuple[str, str, int, str]:
    """Stream an SSE chat completion from LM Studio and aggregate the final output."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "AntigravityLocalExecutor/1.0",
        },
        method="POST",
    )

    start = time.monotonic()
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    finish_reason = "unknown"
    total_tokens = 0

    with urllib.request.urlopen(req, timeout=idle_timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"LM Studio returned HTTP {resp.status}")

        for raw_line in resp:
            if time.monotonic() - start > total_timeout:
                raise TimeoutError(
                    f"Total streaming timeout exceeded ({total_timeout:.1f}s)"
                )

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue

            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            if not isinstance(chunk, dict):
                continue

            usage = chunk.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), (int, float)) and not isinstance(usage.get("total_tokens"), bool):
                total_tokens = int(usage["total_tokens"])

            choices = chunk.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    content_piece = delta.get("content")
                    if isinstance(content_piece, str) and content_piece:
                        content_parts.append(content_piece)

                    reasoning_piece = delta.get("reasoning_content")
                    if isinstance(reasoning_piece, str) and reasoning_piece:
                        reasoning_parts.append(reasoning_piece)

                fr = choice.get("finish_reason")
                if isinstance(fr, str) and fr:
                    finish_reason = fr

    return (
        "".join(content_parts).strip(),
        "".join(reasoning_parts).strip(),
        total_tokens,
        finish_reason,
    )


def execute_local_task(
    prompt: str,
    system_prompt: Optional[str] = None,
    context: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    fast: bool = False,
    model: Optional[str] = None,
    role: Optional[str] = None,
    host: str = LMSTUDIO_HOST,
    reasoning_effort: Optional[str] = None,
    timeout: Optional[float] = None,
    queue_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Executes a task locally on an LM Studio model (0 cloud tokens) using SSE streaming.
    Serializes concurrent cross-process requests via a shared queue lock so parallel
    projects wait their turn instead of timing out and falling back to cloud.
    Strict fail-closed: never instructs automatic cloud fallback on error or timeout.
    """
    queue_wait_sec = 0.0
    t0 = time.monotonic()
    try:
        with _acquire_lmstudio_lock(queue_timeout=queue_timeout) as waited:
            queue_wait_sec = waited
            target_model, assigned_role, warning = resolve_local_model(
                requested_model=model,
                requested_role=role,
                prompt=prompt,
                system_prompt=system_prompt,
                fast=fast,
                host=host
            )

            if not target_model:
                return {
                    "success": False,
                    "allow_cloud_fallback": False,
                    "error_code": "NO_LOCAL_MODEL",
                    "error": warning or "No LLM model is currently loaded in LM Studio.",
                    "queue_wait_seconds": queue_wait_sec,
                    "output": "",
                    "action": FAIL_CLOSED_ACTION,
                }

            safe_max = max(1, max_tokens) if isinstance(max_tokens, int) else 4096
            if fast:
                sys_content = system_prompt or (
                    "You are an ultra-fast code generator. Output ONLY the requested code block. "
                    "Skip internal reasoning, skip explanations, output clean working code immediately."
                )
                temp = 0.1 if temperature == 0.2 else temperature
                limit_tokens = min(safe_max, 2048)
            else:
                sys_content = system_prompt or (
                    "You are an expert, concise software engineer. Provide direct, working code and clear explanations. "
                    "Do not output unrequested chatter."
                )
                temp = temperature
                limit_tokens = safe_max

            full_prompt = prompt
            if context:
                full_prompt = f"### CONTEXT:\n{context}\n\n### TASK:\n{prompt}"

            messages = [
                {"role": "system", "content": sys_content},
                {"role": "user", "content": full_prompt}
            ]

            payload: Dict[str, Any] = {
                "model": target_model,
                "messages": messages,
                "temperature": temp,
                "max_tokens": limit_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
            }

            if isinstance(reasoning_effort, str) and reasoning_effort.strip():
                payload["reasoning_effort"] = reasoning_effort.strip()
            elif fast:
                payload["reasoning_effort"] = "none"

            effective_total = (
                float(timeout)
                if isinstance(timeout, (int, float))
                and not isinstance(timeout, bool)
                and math.isfinite(float(timeout))
                and timeout > 0
                else LOCAL_TOTAL_TIMEOUT_SEC
            )
            effective_idle = min(LOCAL_IDLE_TIMEOUT_SEC, effective_total)

            t_infer = time.monotonic()
            active_model = target_model
            fallback_from: Optional[str] = None

            try:
                content, reasoning, total_tokens, finish_reason = _stream_chat_completion(
                    host=host,
                    payload=payload,
                    idle_timeout=effective_idle,
                    total_timeout=effective_total,
                )
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                if _is_timeout_error(exc) and model is None and assigned_role == "maker":
                    fallback_model = _find_fallback_model(target_model, host=host)
                    if fallback_model is not None:
                        log(f"Maker ({target_model}) timed out ({exc}); retrying on {fallback_model}...")
                        payload["model"] = fallback_model
                        active_model = fallback_model
                        fallback_from = target_model
                        content, reasoning, total_tokens, finish_reason = _stream_chat_completion(
                            host=host,
                            payload=payload,
                            idle_timeout=effective_idle,
                            total_timeout=effective_total,
                        )
                    else:
                        raise
                else:
                    raise

            elapsed = round(time.monotonic() - t_infer, 2)
            final_output = content if content else reasoning
            result: Dict[str, Any] = {
                "success": True,
                "model_used": active_model,
                "role_assigned": assigned_role,
                "elapsed_seconds": elapsed,
                "queue_wait_seconds": queue_wait_sec,
                "output": final_output,
                "has_reasoning": bool(reasoning),
                "tokens_used": total_tokens,
                "finish_reason": finish_reason,
            }
            if fallback_from is not None:
                result["fallback_from"] = fallback_from
            return result
    except LocalQueueTimeoutError as e:
        elapsed = round(time.monotonic() - t0, 2)
        log(f"execute_local_task queue timed out after {elapsed}s: {e}")
        return {
            "success": False,
            "allow_cloud_fallback": False,
            "error_code": "QUEUE_TIMEOUT",
            "error": str(e),
            "elapsed_seconds": elapsed,
            "queue_wait_seconds": elapsed,
            "output": "",
            "action": FAIL_CLOSED_ACTION,
        }
    except Exception as e:
        elapsed = round(time.monotonic() - t0, 2)
        error_code = "LOCAL_TIMEOUT" if _is_timeout_error(e) else "LOCAL_ERROR"
        log(f"execute_local_task failed after {elapsed}s: {e}")
        return {
            "success": False,
            "allow_cloud_fallback": False,
            "error_code": error_code,
            "error": f"Local model execution failed: {e}",
            "elapsed_seconds": elapsed,
            "queue_wait_seconds": queue_wait_sec,
            "output": "",
            "action": FAIL_CLOSED_ACTION,
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
        r"\barchitecture\b", r"\barquitetura\b", r"\bredesign\b", r"\bsystem design\b",
        r"\bsecurity audit\b", r"\baudit.*seguran[çc]a\b", r"\bauditoria\b",
        r"\bvulnerability\b", r"\bvulnerabilidade\b", r"\bdeadlock\b",
        r"\brace conditions?\b", r"\bconcorr[êe]ncia\b",
        r"\bdistributed\b", r"\bdistribu[ií]d[oa]s?\b", r"\bbenchmark.*architecture\b",
        r"\bstrategy\b", r"\bconsultant\b", r"\bcomplex debugging\b",
        r"\bbreaking change\b", r"\bbreaking changes\b", r"\bconsensus\b",
        r"\bmigration\b", r"\bmigra[çc][ãa]o\b", r"\brestructure\b", r"\breestrutur", r"\brearchitect\b",
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
        r"\blist\b", r"\blistar\b", r"\bview\b", r"\bread\b", r"\blea\b",
        r"\bgrep\b", r"\bsearch\b", r"\bfind\b", r"\bwhere\b",
        r"\bwhat is\b", r"\bformat\b", r"\blint\b",
        r"\bsyntax\b", r"\bsintaxe\b", r"\btypo\b", r"\bdocstring\b",
        r"\bquick test\b", r"\bstatus\b", r"\bshow\b",
        r"\bprint\b", r"\bcheck\b", r"\bverify\b", r"\bverifique\b", r"\bconfirm\b",
        r"\bveja\b", r"\barquivos?\b",
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
        "description": "Executes a task locally on an LM Studio model at 0 cloud tokens. Supports dual-model routing: automatically directs code generation & fast tasks to Maker (Qwen3.8 Splash) and adversarial audits & logic checks to Inquisitor (Qwen3.6 35B A3B).",
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
                    "description": "Optional surrounding code, file snippets, diffs, or error messages."
                },
                "max_tokens": {
                    "type": "integer",
                    "default": 4096,
                    "description": "Maximum tokens to generate (default: 4096; accommodates reasoning models like Splash/35B)."
                },
                "temperature": {
                    "type": "number",
                    "default": 0.2,
                    "description": "Sampling temperature (default: 0.2 for deterministic code)."
                },
                "fast": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, optimizes for maximum speed (minimal reasoning tokens, direct code output)."
                },
                "role": {
                    "type": "string",
                    "enum": ["maker", "inquisitor", "auto"],
                    "default": "auto",
                    "description": "Specialized role: 'maker' (Qwen3.8 Splash - fast code & tests), 'inquisitor' (Qwen3.6 35B A3B - merciless audit & reasoning), or 'auto' (contextual triage)."
                },
                "model": {
                    "type": "string",
                    "description": "Optional exact model identifier or alias ('splash', '35b', 'a3b', 'qwen3.8-27b-splash', 'qwen/qwen3.6-35b-a3b'). Overrides automatic role routing."
                },
                "reasoning_effort": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high"],
                    "description": "Optional reasoning effort override ('none' disables thinking tokens for maximum speed; fast=true defaults to 'none')."
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional total streaming timeout in seconds after acquiring the local lock (defaults to LMSTUDIO_TIMEOUT_SEC env var or 600s)."
                },
                "queue_timeout": {
                    "type": "number",
                    "description": "Optional max seconds to wait in the cross-process LM Studio queue when another project is using the local model (defaults to LMSTUDIO_QUEUE_TIMEOUT_SEC or 900s)."
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
        raw_max = args.get("max_tokens")
        max_tokens = raw_max if isinstance(raw_max, int) and raw_max > 0 else 4096
        temperature = args.get("temperature", 0.2)
        fast = args.get("fast", False)
        model = args.get("model")
        role = args.get("role")
        reasoning_effort = args.get("reasoning_effort")
        timeout = args.get("timeout")
        queue_timeout = args.get("queue_timeout")
        return execute_local_task(
            prompt=prompt,
            system_prompt=sys_prompt,
            context=context,
            max_tokens=max_tokens,
            temperature=temperature,
            fast=fast,
            model=model,
            role=role,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
            queue_timeout=queue_timeout,
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
        loaded, warning = get_loaded_models()
        maker_model, _, _ = resolve_local_model(requested_role="maker")
        inquisitor_model, _, _ = resolve_local_model(requested_role="inquisitor")
        lm_running, available_models = check_lmstudio_available()
        return {
            "lmstudio_running": lm_running,
            "dual_model_mode": len(loaded) >= 2,
            "maker_model": maker_model,
            "inquisitor_model": inquisitor_model,
            "loaded_models": loaded,
            "active_model": loaded[0] if loaded else None,
            "available_models": available_models,
            "warning": warning,
            "lmstudio_host": LMSTUDIO_HOST,
            "lmstudio_busy": is_lmstudio_lock_busy(),
            "lock_path": LMSTUDIO_LOCK_PATH,
            "allow_silent_cloud_fallback": False,
            "queue_timeout_sec": LOCAL_QUEUE_TIMEOUT_SEC,
            "idle_timeout_sec": LOCAL_IDLE_TIMEOUT_SEC,
            "total_timeout_sec": LOCAL_TOTAL_TIMEOUT_SEC,
            "routing_engine": "local_heuristic (default 0.2ms)",
            "tier_hierarchy": {
                "low_maker": f"Qwen3.8 27B Splash ({maker_model}) - Code generation, tests, fast implementation (0 cloud tokens)",
                "low_inquisitor": f"Qwen3.6 35B A3B ({inquisitor_model}) - Adversarial code audit, logic, edge cases (0 cloud tokens)",
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
                        "name": "antigravity-tiered-router",
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
