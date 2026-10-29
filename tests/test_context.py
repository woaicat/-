"""Context telemetry follows real middleware work, isolation and lifecycle."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.exceptions import ModelInvalidRequestError
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from evaluation_agent.api import create_app
from evaluation_agent.runtime import ContextBudget
from evaluation_agent.store import WorkspaceStore
from test_api import LocalApiModel, _wait_terminal


TEST_BUDGET = ContextBudget(window_tokens=5000, summarize_at_tokens=120,
                            keep_recent_tokens=25, max_output_tokens=30)


class ContextModel(LocalApiModel):
    summary_delay: float = 0.05
    summary_fails: bool = False

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if "Context Extraction Assistant" in str(messages[-1].content):
            await asyncio.sleep(self.summary_delay)
            if self.summary_fails:
                raise ModelInvalidRequestError("private summary error")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="内部摘要不可显示"))])
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def send(client, cid, key):
    response = client.post(f"/api/conversations/{cid}/messages",
                           json={"body": "测试需求资料。" * 80, "client_key": key})
    assert response.status_code == 200, response.text
    return response.json()["turn_id"]


class ContextTests(unittest.TestCase):
    def test_empty_context_and_usage_survive_restart_without_cross_conversation_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(data_root=directory, model=ContextModel(), model_provider="context-test")) as client:
                cid = client.post("/api/conversations", json={}).json()["conversation_id"]
                other = client.post("/api/conversations", json={}).json()["conversation_id"]
                context = client.get(f"/api/conversations/{cid}").json()["context"]
                self.assertIsNone(context["usage"])
                self.assertEqual(context["window_tokens"], 128000)
                tid = send(client, cid, "first")
                snapshot, _ = _wait_terminal(client, cid, tid)
                usage = snapshot["context"]["usage"]
                self.assertGreater(usage["used_tokens"], 0)
                self.assertTrue(usage["estimated"])
                self.assertEqual(usage["basis"], "last_response")
                self.assertEqual(usage["turn_id"], tid)
                self.assertIsNone(client.get(f"/api/conversations/{other}").json()["context"]["usage"])
                events = client.app.state.store.list_events(cid)
                requests = [e for e in events if e["type"] == "context.usage" and e["payload"]["basis"] == "model_request"]
                self.assertGreaterEqual(usage["used_tokens"], requests[-1]["payload"]["used_tokens"])
            with TestClient(create_app(data_root=directory)) as client:
                self.assertEqual(client.get(f"/api/conversations/{cid}").json()["context"]["usage"], usage)

    def test_real_summary_events_and_smaller_post_summary_usage_without_private_text(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(data_root=directory, model=ContextModel(),
                                      model_provider="context-summary", budget=TEST_BUDGET)) as client:
                cid = client.post("/api/conversations", json={}).json()["conversation_id"]
                _wait_terminal(client, cid, send(client, cid, "first"))
                tid = send(client, cid, "second")
                snapshot, turn = _wait_terminal(client, cid, tid)
                self.assertEqual(turn["status"], "completed")
                compression = snapshot["context"]["compression"]
                self.assertEqual(compression["status"], "completed")
                events = [e for e in client.app.state.store.list_events(cid) if e["turn_id"] == tid]
                types = [e["type"] for e in events]
                self.assertEqual(types.count("context.compression.started"), 1)
                self.assertEqual(types.count("context.compression.completed"), 1)
                start = types.index("context.compression.started")
                done = types.index("context.compression.completed")
                before = [e["payload"]["used_tokens"] for e in events[:start] if e["type"] == "context.usage"][-1]
                after = [e["payload"]["used_tokens"] for e in events[done + 1:] if e["type"] == "context.usage"][0]
                self.assertLess(after, before)
                self.assertTrue(all("内部摘要" not in m["body"] for m in snapshot["messages"]))
                self.assertTrue(all("内部摘要" not in str(e["payload"]) for e in events))
                self.assertEqual(types[-1], "turn.completed")

    def test_summary_failure_is_visible_without_private_error_and_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            model = ContextModel(summary_fails=True)
            with TestClient(create_app(data_root=directory, model=model,
                                      model_provider="context-failure", budget=TEST_BUDGET)) as client:
                cid = client.post("/api/conversations", json={}).json()["conversation_id"]
                _wait_terminal(client, cid, send(client, cid, "first"))
                tid = send(client, cid, "second")
                snapshot, turn = _wait_terminal(client, cid, tid)
                self.assertEqual(turn["status"], "failed")
                self.assertEqual(snapshot["context"]["compression"]["status"], "failed")
                self.assertIn("整理对话上下文失败", snapshot["messages"][-1]["body"])
                self.assertNotIn("private summary", snapshot["messages"][-1]["body"])
                model.summary_fails = False
                _, turn = _wait_terminal(client, cid, send(client, cid, "retry"))
                self.assertEqual(turn["status"], "completed")

    def test_cancel_during_actual_summary_stops_notice_and_has_no_late_events(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(data_root=directory, model=ContextModel(summary_delay=10),
                                      model_provider="context-cancel", budget=TEST_BUDGET)) as client:
                cid = client.post("/api/conversations", json={}).json()["conversation_id"]
                _wait_terminal(client, cid, send(client, cid, "first"))
                tid = send(client, cid, "second")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    context = client.get(f"/api/conversations/{cid}").json()["context"]
                    if (context["compression"] or {}).get("status") == "started":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("Summary never started")
                client.post(f"/api/conversations/{cid}/turns/{tid}/cancel")
                snapshot, turn = _wait_terminal(client, cid, tid)
                self.assertEqual(turn["status"], "cancelled")
                self.assertEqual(snapshot["context"]["compression"]["status"], "cancelled")
                last = snapshot["last_event_id"]
                client.app.state.store.record_context_event(cid, tid, "context.usage", {"used_tokens": 999})
                self.assertEqual(client.get(f"/api/conversations/{cid}").json()["last_event_id"], last)

    def test_crashed_summary_does_not_stay_running(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.sqlite"
            store = WorkspaceStore(path)
            cid = store.create_conversation()["conversation_id"]
            tid = store.begin_message_turn(cid, body="你好", client_key="first")["turn_id"]
            store.record_context_event(cid, tid, "context.compression.started", {})
            reopened = WorkspaceStore(path)
            reopened.recover_interrupted_turns()
            self.assertEqual(reopened.snapshot(cid)["context"]["compression"]["status"], "interrupted")
