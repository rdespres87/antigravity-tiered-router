#!/usr/bin/env python3
"""
Unit and integration tests for Antigravity Tiered Router MCP Server.
Verifies:
  1. Heuristic classification for Low, Medium, and High tiers.
  2. Uncertainty evaluation and automatic cascading triggers.
  3. High-tier consultation formatting.
  4. Full MCP JSON-RPC 2.0 protocol over stdio subprocess.
"""

import io
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import sys
import os
import json
import subprocess

# Ensure server module is in python path
current_dir = os.path.dirname(os.path.abspath(__file__))
server_dir = os.path.join(current_dir, "..", "server")
sys.path.insert(0, server_dir)

from unittest.mock import MagicMock, patch
import tiered_router_mcp
from tiered_router_mcp import (
    heuristic_classifier,
    evaluate_uncertainty,
    format_consultation,
    handle_tool_call,
    execute_local_task,
    TOOLS
)


class TestTieredRouterLogic(unittest.TestCase):

    def test_classify_low_tier(self):
        prompts = [
            "listar arquivos do diretório src",
            "grep por 'PORT' no arquivo config.ts",
            "verifique a sintaxe deste pequeno arquivo",
            "veja se tem algum typo no comentário do README"
        ]
        for p in prompts:
            result = heuristic_classifier(p)
            self.assertEqual(result["tier"], "low", f"Failed for prompt: {p}")
            self.assertEqual(result["suggested_subagent"], "task-runner-low")
            self.assertIn("85", result["estimated_token_savings"])

    def test_classify_medium_tier(self):
        prompts = [
            "implementar autenticação JWT no controller de usuários",
            "adicionar testes unitários para a classe PaymentService",
            "corrigir bug de cálculo de desconto no checkout",
            "criar nova rota POST /api/products com validação de payload"
        ]
        for p in prompts:
            result = heuristic_classifier(p)
            self.assertEqual(result["tier"], "medium", f"Failed for prompt: {p}")
            self.assertEqual(result["suggested_subagent"], "task-runner-medium")

    def test_classify_high_tier(self):
        prompts = [
            "reestruturar arquitetura do sistema distribuído e auditar segurança contra race conditions",
            "system design e redesign completo de persistência com benchmark de arquitetura",
            "auditoria de segurança de concorrência com migração de banco e breaking changes"
        ]
        for p in prompts:
            result = heuristic_classifier(p)
            self.assertEqual(result["tier"], "high", f"Failed for prompt: {p}")
            self.assertEqual(result["suggested_subagent"], "consultant-high")

    def test_uncertainty_clean_output(self):
        clean_output = (
            "Análise concluída com sucesso. O arquivo foi localizado em src/utils.ts "
            "e a função calculateTotal foi verificada. Não foram encontrados erros."
        )
        res = evaluate_uncertainty(clean_output, current_tier="low")
        self.assertFalse(res["should_escalate"])
        self.assertIsNone(res["escalate_to"])
        self.assertGreater(res["confidence_score"], 0.8)

    def test_uncertainty_low_to_medium_escalation(self):
        doubt_output = (
            "Analisei o arquivo mas não tenho certeza sobre a dependência externa. "
            "Além disso, ocorreu um erro: SyntaxError: Unexpected token."
        )
        res = evaluate_uncertainty(doubt_output, current_tier="low")
        self.assertTrue(res["should_escalate"])
        self.assertEqual(res["escalate_to"], "medium")
        self.assertIn("task-runner-medium", res["action"])

    def test_uncertainty_medium_to_high_escalation(self):
        failing_output = (
            "Tentei aplicar a correção no controller, mas o teste falhou com Traceback: "
            "Failed with code 1. Não consegui resolver o conflito de tipos."
        )
        res = evaluate_uncertainty(failing_output, current_tier="medium")
        self.assertTrue(res["should_escalate"])
        self.assertEqual(res["escalate_to"], "high")
        self.assertIn("consultant-high", res["action"])

    def test_format_high_consultation(self):
        task = "Migrar autenticação para sessões stateless"
        findings = "Descobrimos que 4 microsserviços dependem do token em memória"
        dilemma = "Devemos usar Redis compartilhado ou assinar tokens JWT com rotação de chaves?"

        res = format_consultation(task, findings, dilemma)
        self.assertIn("CONSULTATION REQUEST: HIGH TIER ORACLE", res["formatted_prompt"])
        self.assertIn("Redis compartilhado", res["formatted_prompt"])
        self.assertEqual(res["target_agent"], "consultant-high")

    @staticmethod
    def _make_sse_response(chunks, status=200):
        raw = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        stream = io.BytesIO(raw.encode("utf-8"))
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.__iter__ = MagicMock(side_effect=lambda: iter(stream))
        resp.read.return_value = raw.encode("utf-8")
        return resp

    def test_execute_local_task_default_max_tokens_4096(self):
        tool_spec = next(t for t in TOOLS if t["name"] == "execute_local_task")
        self.assertEqual(tool_spec["inputSchema"]["properties"]["max_tokens"]["default"], 4096)

        captured = {}

        def fake_urlopen(req, timeout=60.0):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return self._make_sse_response([
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}},
            ])

        with patch("tiered_router_mcp.resolve_local_model", return_value=("qwen3.8-27b-splash", "maker", None)), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = execute_local_task(prompt="hello", role="maker")
            self.assertTrue(res["success"])
            self.assertEqual(captured["payload"]["max_tokens"], 4096)
            self.assertIs(captured["payload"]["stream"], True)
            self.assertEqual(captured["payload"]["stream_options"], {"include_usage": True})

            res_fast = execute_local_task(prompt="hello", role="maker", fast=True)
            self.assertTrue(res_fast["success"])
            self.assertEqual(captured["payload"]["max_tokens"], 2048)
            self.assertEqual(captured["payload"]["reasoning_effort"], "none")

            res_clamped = execute_local_task(prompt="hello", role="maker", max_tokens=0, fast=True)
            self.assertTrue(res_clamped["success"])
            self.assertEqual(captured["payload"]["max_tokens"], 1)

            res_mcp = handle_tool_call("execute_local_task", {"prompt": "hello", "max_tokens": None})
            self.assertTrue(res_mcp["success"])
            self.assertEqual(captured["payload"]["max_tokens"], 4096)

    def test_execute_local_task_sse_streaming(self):
        captured = {}
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "Let me think"}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": " carefully."}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 42}},
        ]

        def fake_urlopen(req, timeout=60.0):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return self._make_sse_response(chunks)

        with patch("tiered_router_mcp.resolve_local_model", return_value=("qwen3.8-27b-splash", "maker", None)), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = execute_local_task(prompt="hi", role="maker")

        self.assertTrue(res["success"])
        self.assertIs(captured["payload"]["stream"], True)
        self.assertEqual(captured["payload"]["stream_options"], {"include_usage": True})
        self.assertEqual(res["output"], "Hello world")
        self.assertTrue(res["has_reasoning"])
        self.assertEqual(res["tokens_used"], 42)
        self.assertEqual(res["finish_reason"], "stop")

    def test_execute_local_task_reasoning_effort_override(self):
        captured = {}

        def fake_urlopen(req, timeout=60.0):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return self._make_sse_response([
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"total_tokens": 5}},
            ])

        with patch("tiered_router_mcp.resolve_local_model", return_value=("qwen3.8-27b-splash", "maker", None)), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = execute_local_task(prompt="hi", role="maker", reasoning_effort="low")
            self.assertTrue(res["success"])
            self.assertEqual(captured["payload"]["reasoning_effort"], "low")

            res_mcp = handle_tool_call("execute_local_task", {"prompt": "hi", "reasoning_effort": "none", "timeout": 120})
            self.assertTrue(res_mcp["success"])
            self.assertEqual(captured["payload"]["reasoning_effort"], "none")

    def test_execute_local_task_maker_timeout_falls_back_to_inquisitor(self):
        called_models = []

        def fake_urlopen(req, timeout=60.0):
            payload = json.loads(req.data.decode("utf-8"))
            called_models.append(payload["model"])
            if payload["model"] == "qwen3.8-27b-splash":
                raise socket.timeout("timed out")
            return self._make_sse_response([
                {"choices": [{"delta": {"content": "fallback ok"}, "finish_reason": "stop"}], "usage": {"total_tokens": 7}},
            ])

        with patch("tiered_router_mcp.resolve_local_model", return_value=("qwen3.8-27b-splash", "maker", None)), \
             patch("tiered_router_mcp.get_loaded_models", return_value=(["qwen3.8-27b-splash", "qwen/qwen3.6-35b-a3b"], None)), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = execute_local_task(prompt="do it", role="maker")

        self.assertTrue(res["success"])
        self.assertEqual(called_models, ["qwen3.8-27b-splash", "qwen/qwen3.6-35b-a3b"])
        self.assertEqual(res["model_used"], "qwen/qwen3.6-35b-a3b")
        self.assertEqual(res["fallback_from"], "qwen3.8-27b-splash")
        self.assertEqual(res["output"], "fallback ok")

    def test_execute_local_task_pinned_model_no_fallback_on_timeout(self):
        called_models = []

        def fake_urlopen(req, timeout=60.0):
            payload = json.loads(req.data.decode("utf-8"))
            called_models.append(payload["model"])
            raise socket.timeout("pinned timeout")

        with patch("tiered_router_mcp.resolve_local_model", return_value=("qwen3.8-27b-splash", "maker", None)), \
             patch("tiered_router_mcp.get_loaded_models", return_value=(["qwen3.8-27b-splash", "qwen/qwen3.6-35b-a3b"], None)), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = execute_local_task(prompt="x", model="qwen3.8-27b-splash", role="maker")

        self.assertFalse(res["success"])
        self.assertNotIn("fallback_from", res)
        self.assertEqual(called_models, ["qwen3.8-27b-splash"])

    def test_execute_local_task_ignores_malformed_sse_and_non_finite_timeout(self):
        captured = {}
        raw_sse = (
            "data: [1, 2, 3]\n\n"
            "data: not-json\n\n"
            'data: {"choices": [{"delta": {"content": 123, "reasoning_content": ["bad"]}, "finish_reason": null}]}\n\n'
            'data: {"choices": [{"delta": {"content": "clean"}, "finish_reason": "stop"}], "usage": {"total_tokens": 9}}\n\n'
            "data: [DONE]\n\n"
        )

        def fake_urlopen(req, timeout=60.0):
            captured["timeout"] = timeout
            stream = io.BytesIO(raw_sse.encode("utf-8"))
            resp = MagicMock()
            resp.status = 200
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.__iter__ = MagicMock(side_effect=lambda: iter(stream))
            return resp

        with patch("tiered_router_mcp.resolve_local_model", return_value=("qwen3.8-27b-splash", "maker", None)), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = execute_local_task(prompt="hi", role="maker", timeout=float("inf"))

        self.assertTrue(res["success"])
        self.assertEqual(res["output"], "clean")
        self.assertEqual(res["tokens_used"], 9)
        self.assertLess(captured["timeout"], float("inf"))

    def test_default_timeouts_and_fail_closed_when_no_model(self):
        self.assertEqual(tiered_router_mcp.LOCAL_QUEUE_TIMEOUT_SEC, 900.0)
        self.assertEqual(tiered_router_mcp.LOCAL_IDLE_TIMEOUT_SEC, 180.0)
        self.assertEqual(tiered_router_mcp.LOCAL_TOTAL_TIMEOUT_SEC, 600.0)

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "lmstudio_queue.lock")
            with patch("tiered_router_mcp.LMSTUDIO_LOCK_PATH", lock_path), \
                 patch("tiered_router_mcp.resolve_local_model", return_value=(None, "none", "offline")):
                res = execute_local_task(prompt="hello", role="maker")

        self.assertFalse(res["success"])
        self.assertFalse(res["allow_cloud_fallback"])
        self.assertEqual(res["error_code"], "NO_LOCAL_MODEL")
        self.assertIn("Do NOT fallback to cloud", res["action"])

    def test_queue_lock_serializes_and_reports_wait(self):
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "lmstudio_queue.lock")
            started = threading.Event()

            def _hold():
                with open(lock_path, "a+") as fd:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
                    started.set()
                    time.sleep(0.35)
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)

            t = threading.Thread(target=_hold)
            t.start()
            started.wait(timeout=2.0)

            def fake_urlopen(req, timeout=180.0):
                return self._make_sse_response([
                    {"choices": [{"delta": {"content": "queued ok"}, "finish_reason": "stop"}], "usage": {"total_tokens": 11}},
                ])

            with patch("tiered_router_mcp.LMSTUDIO_LOCK_PATH", lock_path), \
                 patch("tiered_router_mcp.resolve_local_model", return_value=("qwen3.8-27b-splash", "maker", None)), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                res = execute_local_task(prompt="hello", role="maker")

            t.join(timeout=2.0)
            self.assertTrue(res["success"])
            self.assertEqual(res["output"], "queued ok")
            self.assertGreaterEqual(res["queue_wait_seconds"], 0.2)

    def test_queue_timeout_is_fail_closed(self):
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "lmstudio_queue.lock")
            started = threading.Event()
            release = threading.Event()

            def _hold():
                with open(lock_path, "a+") as fd:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
                    started.set()
                    release.wait(timeout=2.0)
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)

            t = threading.Thread(target=_hold)
            t.start()
            started.wait(timeout=2.0)
            try:
                with patch("tiered_router_mcp.LMSTUDIO_LOCK_PATH", lock_path):
                    res = execute_local_task(prompt="hello", role="maker", queue_timeout=0.15)
            finally:
                release.set()
                t.join(timeout=2.0)

        self.assertFalse(res["success"])
        self.assertFalse(res["allow_cloud_fallback"])
        self.assertEqual(res["error_code"], "QUEUE_TIMEOUT")
        self.assertIn("Do NOT fallback to cloud", res["action"])

    def test_execution_error_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "lmstudio_queue.lock")
            with patch("tiered_router_mcp.LMSTUDIO_LOCK_PATH", lock_path), \
                 patch("tiered_router_mcp.resolve_local_model", return_value=("qwen3.8-27b-splash", "maker", None)), \
                 patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
                res = execute_local_task(prompt="hello", role="maker")

        self.assertFalse(res["success"])
        self.assertFalse(res["allow_cloud_fallback"])
        self.assertEqual(res["error_code"], "LOCAL_ERROR")
        self.assertIn("Do NOT fallback to cloud", res["action"])
        self.assertNotIn("Escalate to cloud", res["action"])


class TestMCPProtocolStdio(unittest.TestCase):

    def setUp(self):
        server_path = os.path.join(server_dir, "tiered_router_mcp.py")
        self.proc = subprocess.Popen(
            [sys.executable, server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()
        if self.proc.stdin:
            self.proc.stdin.close()
        if self.proc.stdout:
            self.proc.stdout.close()
        if self.proc.stderr:
            self.proc.stderr.close()

    def send_and_receive(self, request_obj):
        line = json.dumps(request_obj) + "\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()
        resp_line = self.proc.stdout.readline()
        return json.loads(resp_line)

    def test_mcp_handshake_and_tool_call(self):
        # 1. Initialize
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"}
        }
        init_res = self.send_and_receive(init_req)
        self.assertEqual(init_res["id"], 1)
        self.assertEqual(init_res["result"]["serverInfo"]["name"], "antigravity-tiered-router")

        # 2. List tools
        tools_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }
        tools_res = self.send_and_receive(tools_req)
        self.assertEqual(tools_res["id"], 2)
        tool_names = [t["name"] for t in tools_res["result"]["tools"]]
        self.assertIn("classify_and_route_task", tool_names)
        self.assertIn("evaluate_task_uncertainty", tool_names)
        self.assertIn("format_high_consultation", tool_names)
        self.assertIn("get_tier_status", tool_names)

        # 3. Call tool classify_and_route_task
        call_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "classify_and_route_task",
                "arguments": {"prompt": "listar os arquivos da pasta docs"}
            }
        }
        call_res = self.send_and_receive(call_req)
        self.assertEqual(call_res["id"], 3)
        content_text = call_res["result"]["content"][0]["text"]
        data = json.loads(content_text)
        self.assertEqual(data["tier"], "low")
        self.assertEqual(data["suggested_subagent"], "task-runner-low")


if __name__ == "__main__":
    unittest.main()
