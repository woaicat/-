"""HTTP data contract, upload isolation, text stream persistence and cancel."""

import asyncio
import json
import tempfile
import time
import unittest
from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from evaluation_agent.api import create_app
from evaluation_agent.runtime import ContextBudget
from test_product_tools import WorkflowModel


class LocalApiModel(BaseChatModel):
    slow: bool = False

    @property
    def _llm_type(self):
        return "local-api-model"

    def _get_ls_params(self, **kwargs):
        return {"ls_provider": "v2api", "ls_model_type": "chat"}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="你好。"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="你"))
        if self.slow:
            await asyncio.sleep(10)
        yield ChatGenerationChunk(message=AIMessageChunk(content="好。"))


class SummarizingApiModel(LocalApiModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if "Context Extraction Assistant" in str(messages[-1].content):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="内部压缩内容不可外显"))])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class SlowAfterOutlineModel(WorkflowModel):
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if isinstance(messages[-1], ToolMessage) and messages[-1].name == "submit_outline":
            await asyncio.sleep(10)
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _wait_terminal(client, conversation_id, turn_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/conversations/{conversation_id}").json()
        turn = next(row for row in snapshot["turns"] if row["turn_id"] == turn_id)
        if turn["status"] in ("completed", "cancelled", "failed", "interrupted"):
            return snapshot, turn
        time.sleep(0.02)
    raise AssertionError("Turn did not reach a terminal state")


class ApiTests(unittest.TestCase):
    def test_accepted_outline_finishes_turn_without_waiting_for_followup_model_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(
                data_root=directory, model=SlowAfterOutlineModel(), model_provider="v2outline",
            )) as client:
                cid = client.post("/api/conversations", json={}).json()["conversation_id"]
                started = time.monotonic()
                response = client.post(
                    f"/api/conversations/{cid}/messages",
                    json={"body": "请设计整套评测方案", "client_key": "plan"},
                )
                self.assertEqual(response.status_code, 200)
                snapshot, turn = _wait_terminal(client, cid, response.json()["turn_id"])
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(turn["status"], "completed")
                self.assertEqual(snapshot["outlines"][0]["status"], "pending")
                confirmed = client.post(
                    f"/api/conversations/{cid}/outlines/{snapshot['outlines'][0]['outline_id']}/confirm",
                    json={"expected_revision": 1, "client_key": "confirm"},
                )
                self.assertEqual(confirmed.status_code, 200)

    def test_conversations_keep_messages_and_agent_threads_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(
                data_root=directory, model=LocalApiModel(), model_provider="v2api",
            )) as client:
                first = client.post("/api/conversations", json={"title": "对话 A"}).json()["conversation_id"]
                second = client.post("/api/conversations", json={"title": "对话 B"}).json()["conversation_id"]
                first_turn = client.post(
                    f"/api/conversations/{first}/messages",
                    json={"body": "只属于 A", "client_key": "a-message"},
                ).json()["turn_id"]
                second_turn = client.post(
                    f"/api/conversations/{second}/messages",
                    json={"body": "只属于 B", "client_key": "b-message"},
                ).json()["turn_id"]
                first_snapshot, _ = _wait_terminal(client, first, first_turn)
                second_snapshot, _ = _wait_terminal(client, second, second_turn)
                self.assertEqual(first_snapshot["messages"][0]["body"], "只属于 A")
                self.assertEqual(second_snapshot["messages"][0]["body"], "只属于 B")
                self.assertNotEqual(
                    first_snapshot["conversation"]["agent_thread_id"],
                    second_snapshot["conversation"]["agent_thread_id"],
                )
                self.assertTrue(all(event["conversation_id"] == first for event in
                                    client.app.state.store.list_events(first)))
                self.assertTrue(all(event["conversation_id"] == second for event in
                                    client.app.state.store.list_events(second)))

    def test_internal_summary_never_appears_in_public_text_stream(self):
        budget = ContextBudget(
            window_tokens=5000, summarize_at_tokens=120,
            keep_recent_tokens=25, max_output_tokens=30,
        )
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(
                data_root=directory, model=SummarizingApiModel(),
                model_provider="v2summary", budget=budget,
            )) as client:
                conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
                for index in range(2):
                    turn_id = client.post(
                        f"/api/conversations/{conversation_id}/messages",
                        json={"body": "x" * 400, "client_key": f"long-{index}"},
                    ).json()["turn_id"]
                    snapshot, turn = _wait_terminal(client, conversation_id, turn_id)
                    self.assertEqual(turn["status"], "completed")
                self.assertTrue(all("内部压缩内容" not in message["body"] for message in snapshot["messages"]))
                events = client.app.state.store.list_events(conversation_id)
                self.assertTrue(all(
                    "内部压缩内容" not in event["payload"].get("delta", "")
                    for event in events if event["type"] == "message.delta"
                ))

    def test_sse_replays_after_cursor_with_stable_envelope(self):
        class FakeRequest:
            headers = {}

            async def is_disconnected(self):
                return False

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                app = create_app(data_root=directory)
                store = app.state.store
                conversation_id = store.create_conversation()["conversation_id"]
                turn = store.begin_message_turn(conversation_id, body="你好", client_key="m1")
                store.finish_turn(conversation_id, turn["turn_id"], "completed")
                route = next(route for route in app.routes if getattr(route, "path", "") ==
                             "/api/conversations/{conversation_id}/events")
                response = await route.endpoint(conversation_id, FakeRequest(), after=0)
                first = await response.body_iterator.__anext__()
                second = await response.body_iterator.__anext__()
                self.assertIn("event: turn.started", first)
                self.assertIn("event: turn.completed", second)
                event_id = int(first.splitlines()[0].removeprefix("id: "))
                resumed = await route.endpoint(conversation_id, FakeRequest(), after=event_id)
                replay = await resumed.body_iterator.__anext__()
                self.assertEqual(replay, second)
                payload = json.loads(second.split("data: ", 1)[1])
                self.assertEqual(payload["conversation_id"], conversation_id)
                self.assertEqual(payload["event_id"], event_id + 1)
                await response.body_iterator.aclose()
                await resumed.body_iterator.aclose()

        asyncio.run(run())

    def test_local_server_without_model_keeps_data_and_accepts_supported_uploads(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(data_root=directory)) as client:
                self.assertEqual(client.get("/").status_code, 200)
                home = client.get("/")
                self.assertIn("评测工作台", home.text)
                self.assertIn("/assets/app.js?v=", home.text)
                self.assertEqual(home.headers["cache-control"], "no-store")
                self.assertEqual(client.get("/assets/app.js").status_code, 200)
                first = client.post("/api/conversations", json={"title": "产品 A"}).json()["conversation_id"]
                second = client.post("/api/conversations", json={"title": "产品 B"}).json()["conversation_id"]
                upload = client.post(
                    f"/api/conversations/{first}/attachments",
                    data={"upload_key": "spec-1"},
                    files={"file": ("需求.md", b"# Product A", "text/markdown")},
                )
                self.assertEqual(upload.status_code, 200)
                self.assertEqual(client.get(f"/api/conversations/{second}").json()["attachments"], [])
                self.assertNotIn("storage_path", client.get(f"/api/conversations/{first}").json()["attachments"][0])
                document = Document()
                document.add_paragraph("补充说明")
                stream = BytesIO()
                document.save(stream)
                word = client.post(
                    f"/api/conversations/{first}/attachments",
                    data={"upload_key": "word-1"},
                    files={"file": ("补充.docx", stream.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                )
                self.assertEqual(word.status_code, 200)
                rejected = client.post(
                    f"/api/conversations/{first}/attachments",
                    data={"upload_key": "pdf"}, files={"file": ("材料.pdf", b"pdf", "application/pdf")},
                )
                self.assertEqual(rejected.status_code, 422)
                self.assertEqual(client.post(
                    f"/api/conversations/{first}/messages", json={"body": "你好", "client_key": "m1"}
                ).status_code, 503)
                self.assertEqual(client.get("/api/conversations/missing").status_code, 404)

    def test_model_reply_persists_incremental_events_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(data_root=directory, model=LocalApiModel(), model_provider="v2api")) as client:
                conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
                response = client.post(
                    f"/api/conversations/{conversation_id}/messages",
                    json={"body": "你好", "client_key": "m1"},
                )
                self.assertEqual(response.status_code, 200)
                turn_id = response.json()["turn_id"]
                snapshot, turn = _wait_terminal(client, conversation_id, turn_id)
                self.assertEqual(turn["status"], "completed")
                self.assertGreaterEqual(turn["duration_ms"], 0)
                self.assertEqual(snapshot["conversation"]["title"], "你好")
                self.assertEqual(snapshot["messages"][-1]["body"], "你好。")
                events = client.app.state.store.list_events(conversation_id)
                self.assertGreaterEqual(len([event for event in events if event["type"] == "message.delta"]), 2)
                self.assertEqual(events[-1]["type"], "turn.completed")
                self.assertEqual(client.post(
                    f"/api/conversations/{conversation_id}/messages",
                    json={"body": "你好", "client_key": "m1"},
                ).json()["turn_id"], turn_id)

    def test_manual_cancel_blocks_input_and_finishes_without_late_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(data_root=directory, model=LocalApiModel(slow=True), model_provider="v2api")) as client:
                conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
                turn_id = client.post(
                    f"/api/conversations/{conversation_id}/messages",
                    json={"body": "请回答", "client_key": "m1"},
                ).json()["turn_id"]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if any(event["type"] == "message.delta" for event in client.app.state.store.list_events(conversation_id)):
                        break
                    time.sleep(0.02)
                self.assertEqual(client.post(
                    f"/api/conversations/{conversation_id}/messages",
                    json={"body": "插话", "client_key": "m2"},
                ).status_code, 409)
                self.assertEqual(client.post(
                    f"/api/conversations/{conversation_id}/attachments",
                    data={"upload_key": "blocked"},
                    files={"file": ("资料.md", b"extra", "text/markdown")},
                ).status_code, 409)
                cancel = client.post(f"/api/conversations/{conversation_id}/turns/{turn_id}/cancel")
                self.assertEqual(cancel.status_code, 200)
                _, turn = _wait_terminal(client, conversation_id, turn_id)
                self.assertEqual(turn["status"], "cancelled")
                events = client.app.state.store.list_events(conversation_id)
                self.assertEqual(events[-1]["type"], "turn.cancelled")
                self.assertEqual(client.app.state.store.list_events(conversation_id, after=events[-1]["event_id"]), [])

    def test_http_outline_confirmation_and_individual_file_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            model = WorkflowModel()
            with TestClient(create_app(data_root=directory, model=model, model_provider="v2workflow")) as client:
                conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
                plan = client.post(
                    f"/api/conversations/{conversation_id}/messages",
                    json={"body": "请设计一整套评测方案", "client_key": "plan"},
                )
                self.assertEqual(plan.status_code, 200)
                snapshot, turn = _wait_terminal(client, conversation_id, plan.json()["turn_id"])
                self.assertEqual(turn["status"], "completed")
                outline = snapshot["outlines"][0]
                self.assertEqual(outline["status"], "pending")
                self.assertEqual(snapshot["artifacts"], [])
                model.phase = "generation"
                confirm = client.post(
                    f"/api/conversations/{conversation_id}/outlines/{outline['outline_id']}/confirm",
                    json={"expected_revision": 1, "client_key": "confirm"},
                )
                self.assertEqual(confirm.status_code, 200)
                final, turn = _wait_terminal(client, conversation_id, confirm.json()["turn_id"])
                self.assertEqual(turn["status"], "completed")
                self.assertEqual(final["evaluations"][0]["package_status"], "completed")
                self.assertEqual(final["artifacts"][0]["filename"], "评测方案.docx")
                self.assertNotIn("source_path", final["artifacts"][0])
                self.assertEqual(final["messages"][-1]["body"], "评测方案已完成，请打开产物预览。")
                artifact_id = final["artifacts"][0]["artifact_id"]
                self.assertEqual(final["messages"][-1]["artifact_ids"], [artifact_id])
                self.assertEqual(client.get(
                    f"/api/conversations/{conversation_id}/artifacts/{artifact_id}/preview"
                ).status_code, 200)
                self.assertEqual(client.get(
                    f"/api/conversations/{conversation_id}/artifacts/{artifact_id}/download"
                ).status_code, 200)
                foreign_conversation = client.post("/api/conversations", json={}).json()["conversation_id"]
                self.assertEqual(client.get(
                    f"/api/conversations/{foreign_conversation}/artifacts/{artifact_id}/preview"
                ).status_code, 404)
                self.assertEqual(client.get(
                    f"/api/conversations/{foreign_conversation}/artifacts/{artifact_id}/download"
                ).status_code, 404)
                event = [row for row in client.app.state.store.list_events(conversation_id)
                         if row["type"] == "message.completed"][-1]
                self.assertEqual(event["payload"]["artifact_ids"], [artifact_id])


if __name__ == "__main__":
    unittest.main()
