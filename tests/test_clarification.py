"""Clarification transport/scope contracts, not a substitute for real-model evals."""

import json
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from evaluation_agent.api import create_app
from test_product_tools import OUTLINE, WorkflowModel


QUESTION = "请补充三个信息：\n1. 客服面向什么业务，典型问题和期望结果是什么？\n2. 这次评测支持什么决策，最担心哪类错误？\n3. 根据什么资料或人工复核判断回答好坏？"


class ClarificationFlowModel(WorkflowModel):
    bind_discussion: bool = False
    evaluation_id: str = ""
    system_prompts: list[str] = Field(default_factory=list)
    seen_user_messages: list[str] = Field(default_factory=list)
    tool_descriptions: dict[str, str] = Field(default_factory=dict)

    def bind_tools(self, tools, **kwargs):
        super().bind_tools(tools, **kwargs)
        self.tool_descriptions = {item.name: item.description for item in tools if hasattr(item, "name")}
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.system_prompts.extend(str(m.content) for m in messages if isinstance(m, SystemMessage))
        self.seen_user_messages = [str(m.content) for m in messages if isinstance(m, HumanMessage)]
        last = messages[-1]
        if isinstance(last, HumanMessage):
            if "已补充需求" in str(last.content):
                reply = self._tool_call("select_evaluation", {
                    "scope_kind": "package_outline", "evaluation_id": self.evaluation_id,
                })
            elif self.bind_discussion:
                reply = self._tool_call("select_evaluation", {
                    "scope_kind": "discussion", "product_name": "客服助手",
                })
            else:
                reply = AIMessage(content=QUESTION)
        elif isinstance(last, ToolMessage) and last.name == "select_evaluation":
            selected = json.loads(last.content)["result"]
            self.evaluation_id = selected["evaluation_id"]
            if selected["scope_kind"] == "discussion":
                reply = AIMessage(content=QUESTION)
            else:
                reply = self._tool_call("submit_outline", {"body": OUTLINE})
        else:
            reply = AIMessage(content="请在大纲卡片中确认。")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def finish(client, cid, tid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/conversations/{cid}").json()
        turn = next(t for t in snapshot["turns"] if t["turn_id"] == tid)
        if turn["status"] not in ("running", "cancel_requested"):
            return snapshot, turn
        time.sleep(.01)
    raise AssertionError("Controlled clarification turn timed out")


class ClarificationContractTests(unittest.TestCase):
    def test_plain_questions_finish_without_outline_mode_or_files(self):
        model = ClarificationFlowModel()
        with tempfile.TemporaryDirectory() as root, TestClient(create_app(
            data_root=root, model=model, model_provider="clarification-local",
        )) as client:
            cid = client.post("/api/conversations", json={}).json()["conversation_id"]
            tid = client.post(f"/api/conversations/{cid}/messages", json={
                "body": "为智能客服设计一套评测方案", "client_key": "ask",
            }).json()["turn_id"]
            snapshot, turn = finish(client, cid, tid)
            self.assertEqual(turn["status"], "completed")
            self.assertEqual(turn["scope_kind"], "undecided")
            self.assertEqual(snapshot["messages"][-1]["body"], QUESTION)
            self.assertFalse(snapshot["outlines"] or snapshot["artifacts"] or snapshot["evaluations"])
            self.assertTrue(any("【需求澄清" in text for text in model.system_prompts))
            self.assertFalse(any("【需求澄清" in text for text in model.seen_user_messages))
            self.assertIn("BEFORE choosing package_outline", model.tool_descriptions["select_evaluation"])
            self.assertIn("not use this tool as a substitute for clarification", model.tool_descriptions["submit_outline"])

    def test_discussion_reuses_product_and_context_when_next_turn_can_plan(self):
        model = ClarificationFlowModel(bind_discussion=True)
        with tempfile.TemporaryDirectory() as root, TestClient(create_app(
            data_root=root, model=model, model_provider="clarification-local",
        )) as client:
            cid = client.post("/api/conversations", json={}).json()["conversation_id"]
            tid = client.post(f"/api/conversations/{cid}/messages", json={
                "body": "为客服助手设计评测方案", "client_key": "ask",
            }).json()["turn_id"]
            snapshot, turn = finish(client, cid, tid)
            self.assertEqual(turn["status"], "completed")
            self.assertEqual(turn["scope_kind"], "discussion")
            self.assertFalse(snapshot["outlines"] or snapshot["artifacts"])
            tid = client.post(f"/api/conversations/{cid}/messages", json={
                "body": "已补充需求：员工 IT 问答，上线验收，基于知识库并由 IT 人员复核。", "client_key": "plan",
            }).json()["turn_id"]
            snapshot, turn = finish(client, cid, tid)
            self.assertEqual(turn["status"], "completed")
            self.assertEqual(turn["scope_kind"], "package_outline")
            self.assertEqual(len(snapshot["evaluations"]), 1)
            self.assertEqual(len(snapshot["outlines"]), 1)
            self.assertEqual(snapshot["outlines"][0]["status"], "pending")
            self.assertFalse(snapshot["artifacts"])
            self.assertTrue(any("为客服助手设计评测方案" in text for text in model.seen_user_messages))
            self.assertTrue(any("已补充需求" in text for text in model.seen_user_messages))


if __name__ == "__main__":
    unittest.main()
