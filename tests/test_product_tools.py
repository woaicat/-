"""The real product tools work through Deep Agents and server runtime IDs."""

import sqlite3
import re
import tempfile
import unittest
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite import SqliteSaver

from evaluation_agent.artifacts import ArtifactPublisher
from evaluation_agent.product_tools import create_product_tools
from evaluation_agent.runtime import MODEL_TOOLS, create_runtime
from evaluation_agent.scoped_backend import ScopedStateBackend
from evaluation_agent.store import WorkspaceStore


OUTLINE = """## 评测目标
检验质量。
## 评测指标
准确率。
## 评测数据样本
覆盖样本。
## 评测执行方案
离线执行。
## 产物清单
- 评测方案.docx
"""


class WorkflowModel(BaseChatModel):
    phase: str = "planning"
    turn_id: str = ""
    deliverable_id: str = ""
    bound_names: list[str] = []
    serial: int = 0

    @property
    def _llm_type(self):
        return "workflow-local"

    def _get_ls_params(self, **kwargs):
        return {"ls_provider": "v2workflow", "ls_model_type": "chat"}

    def bind_tools(self, tools, **kwargs):
        self.bound_names = [item.name if hasattr(item, "name") else item["function"]["name"] for item in tools]
        return self

    def _tool_call(self, name, arguments):
        self.serial += 1
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": arguments, "id": f"call-{self.serial}",
        }])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        if self.phase == "planning":
            if isinstance(last, HumanMessage):
                response = self._tool_call("select_evaluation", {
                    "scope_kind": "package_outline", "product_name": "产品 A",
                })
            elif isinstance(last, ToolMessage) and last.name == "select_evaluation":
                response = self._tool_call("submit_outline", {"body": OUTLINE})
            else:
                response = AIMessage(content="大纲已提交，请确认。")
        elif isinstance(last, HumanMessage) and "本轮文件已完成服务端校验" in str(last.content):
            response = AIMessage(content="评测方案已完成，请打开产物预览。")
        elif isinstance(last, HumanMessage):
            path_match = re.search(r"/drafts/([0-9a-f]+)/", str(last.content))
            delivery_match = re.search(r"本轮交付清单：([0-9a-f]+)：", str(last.content))
            if path_match:
                self.turn_id = path_match.group(1)
            if delivery_match:
                self.deliverable_id = delivery_match.group(1)
            response = self._tool_call("write_file", {
                "file_path": f"/drafts/{self.turn_id}/plan.md",
                "content": "# 评测方案\n\n## 指标\n- 准确率\n",
            })
        elif isinstance(last, ToolMessage) and last.name == "write_file":
            response = self._tool_call("publish_artifact", {
                "draft_path": f"/drafts/{self.turn_id}/plan.md",
                "filename": "评测方案.docx",
                "deliverable_id": self.deliverable_id,
            })
        else:
            response = AIMessage(content=str(last.content))
        return ChatResult(generations=[ChatGeneration(message=response)])


class ProductToolIntegrationTests(unittest.TestCase):
    def test_select_evaluation_exposes_exact_scope_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkspaceStore(root / "workspace.sqlite")
            tool = create_product_tools(
                store, ArtifactPublisher(store, root / "outputs"), ScopedStateBackend()
            )[0]
            scope_schema = tool.tool_call_schema.model_json_schema()["properties"]["scope_kind"]
            self.assertEqual(scope_schema["enum"], [
                "discussion", "package_outline", "package_continue", "focused_create", "focused_edit", "focused_rename",
            ])
            self.assertIn("focused_edit for edits to existing artifact IDs", tool.description)

    def test_planning_then_user_confirmation_then_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkspaceStore(root / "workspace.sqlite")
            publisher = ArtifactPublisher(store, root / "outputs")
            backend = ScopedStateBackend()
            tools = create_product_tools(store, publisher, backend)
            model = WorkflowModel()
            conversation_id = store.create_conversation()["conversation_id"]
            plan_turn = store.begin_message_turn(
                conversation_id, body="请设计一整套评测方案", client_key="plan"
            )["turn_id"]
            connection = sqlite3.connect(root / "checkpoint.sqlite", check_same_thread=False)
            try:
                agent = create_runtime(
                    model=model, model_provider="v2workflow", product_tools=tools,
                    checkpointer=SqliteSaver(connection), backend=backend,
                )
                agent.invoke({"messages": [HumanMessage(content="请设计一整套评测方案")]}, {
                    "configurable": {"thread_id": conversation_id, "conversation_id": conversation_id, "turn_id": plan_turn}
                })
                self.assertEqual(set(model.bound_names), MODEL_TOOLS)
                snapshot = store.snapshot(conversation_id)
                self.assertEqual(snapshot["outlines"][0]["status"], "pending")
                self.assertEqual(snapshot["artifacts"], [])
                store.finish_turn(conversation_id, plan_turn, "completed")
                confirmation = store.confirm_outline(
                    conversation_id, snapshot["outlines"][0]["outline_id"],
                    expected_revision=1, client_key="confirmed",
                )
                model.phase = "generation"
                model.turn_id = confirmation["turn_id"]
                model.deliverable_id = confirmation["deliverable_ids"][0]
                result = agent.invoke({"messages": [HumanMessage(content="已确认大纲，开始生成")]}, {
                    "configurable": {"thread_id": conversation_id, "conversation_id": conversation_id,
                                     "turn_id": confirmation["turn_id"]}
                })
                self.assertIn('"ok": true', result["messages"][-1].content)
                snapshot = store.snapshot(conversation_id)
                self.assertEqual(snapshot["artifacts"][0]["filename"], "评测方案.docx")
                self.assertTrue(Path(snapshot["artifacts"][0]["export_path"]).is_file())
                self.assertEqual(snapshot["deliverables"][0]["status"], "ready")
                store.finish_turn(conversation_id, confirmation["turn_id"], "completed")
                self.assertEqual(store.snapshot(conversation_id)["evaluations"][0]["package_status"], "completed")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
