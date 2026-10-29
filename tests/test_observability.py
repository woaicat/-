"""Real SDK trace hierarchy with intercepted ingestion; no external requests."""

import asyncio
import contextlib
import io
import json
import os
import runpy
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import langsmith as ls
from langgraph.checkpoint.memory import MemorySaver
from langsmith import Client
from langsmith import utils as ls_utils

from evaluation_agent.executor import TurnExecutor
from evaluation_agent.observability import configure_observability, trace_turn
from evaluation_agent.runtime import create_runtime
from evaluation_agent.store import WorkspaceStore
from test_api import LocalApiModel
from test_executor_validation import EmptyAgent
from test_runtime import LocalScriptedModel, PRODUCT_TOOLS


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        ls_utils.get_env_var.cache_clear()
        ls_utils.get_tracer_project.cache_clear()

    def tearDown(self):
        ls_utils.get_env_var.cache_clear()
        ls_utils.get_tracer_project.cache_clear()

    def test_disabled_tracing_needs_no_key_and_does_not_create_client(self):
        async def check():
            async with trace_turn(conversation_id="c", turn_id="t", thread_id="h", prompt="hello") as run:
                self.assertIsNone(run)
        with patch.dict(os.environ, {"LANGSMITH_TRACING": "false"}, clear=True):
            with patch("evaluation_agent.observability.ls.trace") as trace:
                configure_observability()
                asyncio.run(check())
                trace.assert_not_called()

    def test_opt_in_validates_key_and_unambiguous_destination(self):
        with patch.dict(os.environ, {"LANGSMITH_TRACING": "true"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "LANGSMITH_API_KEY"):
                configure_observability()
            os.environ["LANGSMITH_API_KEY"] = "local-test-key"
            os.environ["LANGSMITH_AGENT_ID"] = "evaluation-agent-v2"
            with self.assertRaisesRegex(RuntimeError, "一起填写"):
                configure_observability()
            os.environ["LANGSMITH_AGENT_ENVIRONMENT"] = "local"
            configure_observability()
            self.assertNotIn("LANGSMITH_PROJECT", os.environ)
            os.environ["LANGSMITH_PROJECT"] = "conflicting-project"
            with self.assertRaisesRegex(RuntimeError, "移除"):
                configure_observability()

    def _run_with_intercepted_ingestion(self, run):
        with patch.dict(os.environ, {
            "LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "local-test-key",
            "LANGSMITH_PROJECT": "unit-test-project",
        }, clear=True):
            client = Client(api_url="http://127.0.0.1:9", api_key="local-test-key", auto_batch_tracing=False)
            try:
                with patch.object(Client, "create_run") as created, patch.object(Client, "update_run") as updated:
                    with ls.tracing_context(client=client, enabled=True):
                        asyncio.run(run())
                    return [call.kwargs for call in created.call_args_list], updated.call_args_list
            finally:
                client.close()

    def test_actual_deep_agent_model_and_file_tool_share_turn_trace(self):
        async def run():
            agent = create_runtime(
                model=LocalScriptedModel(), model_provider="v2tracing",
                product_tools=PRODUCT_TOOLS, checkpointer=MemorySaver(),
            )
            async with trace_turn(conversation_id="c", turn_id="turn-a", thread_id="h", prompt="write") as root:
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": "write:synthetic note"}]},
                    {"configurable": {"thread_id": "h", "turn_id": "turn-a"}},
                )
                self.assertIn("/drafts/turn-a/note.md", result["files"])
                root.end(outputs={"status": "completed"})
        created, _ = self._run_with_intercepted_ingestion(run)
        roots = [item for item in created if item["name"] == "评测方案智能体 · 对话轮次"]
        self.assertEqual(len(roots), 1)
        root = roots[0]
        self.assertTrue(any(item.get("run_type") == "llm" for item in created))
        self.assertTrue(any(item.get("name") == "write_file" for item in created))
        children = [item for item in created if item["id"] != root["id"]]
        self.assertTrue(all(item["trace_id"] == root["trace_id"] for item in children))
        self.assertTrue(all(item["extra"]["metadata"]["turn_id"] == "turn-a" for item in children))

    def test_executor_records_failed_terminal_status_on_root(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                store = WorkspaceStore(f"{directory}/workspace.sqlite")
                cid = store.create_conversation()["conversation_id"]
                tid = store.begin_message_turn(cid, body="hello", client_key="test")["turn_id"]
                await TurnExecutor(store, EmptyAgent())._run(cid, tid, "hello")
                self.assertEqual(store.get_turn(cid, tid)["status"], "failed")
        _, updated = self._run_with_intercepted_ingestion(run)
        failures = [call.kwargs for call in updated if call.kwargs.get("outputs", {}).get("status") == "failed"]
        self.assertTrue(failures)
        self.assertIn("failed", failures[-1]["error"])

    def test_executor_records_reply_and_conversation_metadata(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                store = WorkspaceStore(f"{directory}/workspace.sqlite")
                cid = store.create_conversation()["conversation_id"]
                tid = store.begin_message_turn(cid, body="hello", client_key="test")["turn_id"]
                agent = create_runtime(
                    model=LocalApiModel(), model_provider="v2tracingreply",
                    product_tools=PRODUCT_TOOLS, checkpointer=MemorySaver(),
                )
                await TurnExecutor(store, agent)._run(cid, tid, "hello")
                self.assertEqual(store.get_turn(cid, tid)["status"], "completed")
        created, updated = self._run_with_intercepted_ingestion(run)
        root = next(item for item in created if item["name"] == "评测方案智能体 · 对话轮次")
        self.assertTrue(root["extra"]["metadata"]["conversation_id"])
        completed = [call.kwargs for call in updated if call.kwargs.get("outputs", {}).get("status") == "completed"]
        self.assertTrue(completed)
        self.assertEqual(completed[-1]["outputs"]["reply"], "你好。")

    def test_concurrent_conversations_have_separate_root_traces(self):
        async def one(cid):
            async with trace_turn(conversation_id=cid, turn_id=cid, thread_id=cid, prompt=cid) as root:
                await asyncio.sleep(0)
                child = ls.get_current_run_tree().create_child("synthetic-child", inputs={"id": cid})
                self.assertEqual(child.parent_run_id, root.id)
                self.assertEqual(child.metadata["conversation_id"], cid)
                root.end(outputs={"status": "completed"})
        async def run():
            await asyncio.gather(one("conversation-a"), one("conversation-b"))
        created, _ = self._run_with_intercepted_ingestion(run)
        self.assertEqual(len(created), 2)
        self.assertEqual(len({item["trace_id"] for item in created}), 2)

    def test_connection_script_submits_only_synthetic_data_to_local_stub(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def record(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                requests.append((self.command, self.path, body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = record
            do_PATCH = record

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {
                "LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "local-test-key",
                "LANGSMITH_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
                "LANGSMITH_AGENT_ID": "evaluation-agent-v2", "LANGSMITH_AGENT_ENVIRONMENT": "local",
            }, clear=True):
                entry = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check_langsmith.py"))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(entry["main"](), 0)
                self.assertIn("测试记录已提交", output.getvalue())
                self.assertNotIn("local-test-key", output.getvalue())
            created = next(body for method, _, body in requests if method == "POST")
            self.assertEqual(created["name"], "LangSmith 连接测试")
            self.assertEqual(created["address"], "lrn:agents/evaluation-agent-v2/environments/local")
            self.assertEqual(set(created["inputs"]), {"message"})
            self.assertTrue(any(method == "PATCH" and body.get("end_time") for method, _, body in requests))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
