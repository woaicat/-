"""Same-turn repair and honest terminal replies without any model API."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from evaluation_agent.artifacts import ArtifactPublisher
from evaluation_agent.executor import TurnExecutor
from evaluation_agent.runtime import ContextBudgetExceeded
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


class _EventStream:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def __aiter__(self):
        async def pieces():
            for event in self.events:
                yield event
        return pieces()


class RepairAgent:
    def __init__(self, publisher, conversation_id, turn_id, deliverable_id, *, repair=True):
        self.publisher = publisher
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.deliverable_id = deliverable_id
        self.repair = repair
        self.calls = 0
        self.artifact_id = None

    async def astream_events(self, payload, config, version):
        self.calls += 1
        text = str(payload["messages"][-1].content)
        if "本轮文件已完成服务端校验" in text:
            return _EventStream([{
                "method": "messages", "params": {"namespace": [], "data": (
                    AIMessage(content="产物已完成，可打开预览。"), {"langgraph_node": "model"},
                )},
            }])
        if self.calls == 1:
            published = self.publisher.publish(
                self.conversation_id, self.turn_id,
                draft_path=f"/drafts/{self.turn_id}/plan.md",
                filename="评测方案.docx",
                content="# 评测方案\n\n详见[遗漏文件](遗漏文件.md)。",
                deliverable_id=self.deliverable_id,
            )
            self.artifact_id = published["artifact_id"]
        elif "服务端验收发现" in text and self.repair:
            self.publisher.publish(
                self.conversation_id, self.turn_id,
                draft_path=f"/drafts/{self.turn_id}/plan.md",
                filename="评测方案.docx",
                content="# 评测方案\n\n评测执行步骤已列明。",
                deliverable_id=self.deliverable_id,
                artifact_id=self.artifact_id,
            )
        return _EventStream([])


class EmptyAgent:
    async def astream_events(self, payload, config, version):
        return _EventStream([])


class TextOnlyAgent:
    def __init__(self):
        self.calls = 0

    async def astream_events(self, payload, config, version):
        self.calls += 1
        return _EventStream([{
            "method": "messages", "params": {"namespace": [], "data": (
                AIMessage(content="已提交大纲。"), {"langgraph_node": "model"},
            )},
        }])


class FailingAgent:
    def __init__(self, error):
        self.error = error

    async def astream_events(self, payload, config, version):
        raise self.error


class ContinueAgent:
    def __init__(self, publisher, conversation_id, turn_id, deliverable_id):
        self.publisher = publisher
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.deliverable_id = deliverable_id
        self.final_prompt = ""

    async def astream_events(self, payload, config, version):
        text = str(payload["messages"][-1].content)
        if "本轮文件已完成服务端校验" in text:
            self.final_prompt = text
            return _EventStream([{
                "method": "messages", "params": {"namespace": [], "data": (
                    AIMessage(content="整套方案已完成。"), {"langgraph_node": "model"},
                )},
            }])
        self.publisher.publish(
            self.conversation_id, self.turn_id,
            draft_path=f"/drafts/{self.turn_id}/samples.csv",
            filename="样本.csv", content="id,answer\n1,yes\n",
            deliverable_id=self.deliverable_id,
        )
        return _EventStream([])


class ExecutorValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WorkspaceStore(root / "workspace.sqlite")
        self.publisher = ArtifactPublisher(self.store, root / "outputs")
        self.conversation_id = self.store.create_conversation()["conversation_id"]

    def tearDown(self):
        self.temp.cleanup()

    def _confirmed_turn(self, outline_body=OUTLINE):
        plan = self.store.begin_message_turn(
            self.conversation_id, body="设计整套评测方案", client_key="plan",
        )["turn_id"]
        self.store.select_evaluation_scope(
            self.conversation_id, plan, scope_kind="package_outline", product_name="产品 A",
        )
        outline = self.store.submit_outline(self.conversation_id, plan, outline_body)
        self.store.finish_turn(self.conversation_id, plan, "completed")
        return self.store.confirm_outline(
            self.conversation_id, outline["outline_id"], expected_revision=1,
            client_key="confirmed",
        )

    def test_broken_reference_gets_one_repair_and_completed_file_link(self):
        confirmed = self._confirmed_turn()
        agent = RepairAgent(self.publisher, self.conversation_id,
                            confirmed["turn_id"], confirmed["deliverable_ids"][0])
        asyncio.run(TurnExecutor(self.store, agent)._run(
            self.conversation_id, confirmed["turn_id"], "开始",
        ))
        snapshot = self.store.snapshot(self.conversation_id)
        self.assertEqual(agent.calls, 3)
        self.assertEqual(snapshot["turns"][-1]["status"], "completed")
        self.assertEqual(snapshot["evaluations"][0]["package_status"], "completed")
        self.assertEqual(snapshot["artifacts"][0]["content_revision"], 2)
        completed = [e for e in self.store.list_events(self.conversation_id)
                     if e["type"] == "message.completed"][-1]
        self.assertEqual(completed["payload"]["artifact_ids"], [agent.artifact_id])

    def test_continued_package_final_reply_links_all_prior_and_new_files(self):
        confirmed = self._confirmed_turn(OUTLINE + "- 样本.csv\n")
        first_turn = confirmed["turn_id"]
        first = self.publisher.publish(
            self.conversation_id, first_turn,
            draft_path=f"/drafts/{first_turn}/plan.md",
            filename="评测方案.docx", content="# 评测方案\n\n样本见[样本表](样本.csv)。",
            deliverable_id=confirmed["deliverable_ids"][0],
        )
        self.store.request_cancel(self.conversation_id, first_turn)
        self.store.finish_turn(self.conversation_id, first_turn, "cancelled")
        resumed = self.store.begin_message_turn(
            self.conversation_id, body="继续完成剩余工作", client_key="continue-after-cancel",
        )["turn_id"]
        evaluation_id = self.store.snapshot(self.conversation_id)["evaluations"][0]["evaluation_id"]
        scope = self.store.select_evaluation_scope(
            self.conversation_id, resumed, scope_kind="package_continue", evaluation_id=evaluation_id,
        )
        self.assertEqual(scope["deliverable_ids"], [confirmed["deliverable_ids"][1]])
        agent = ContinueAgent(self.publisher, self.conversation_id, resumed, scope["deliverable_ids"][0])
        asyncio.run(TurnExecutor(self.store, agent)._run(
            self.conversation_id, resumed, "继续完成剩余工作",
        ))
        snapshot = self.store.snapshot(self.conversation_id)
        self.assertEqual(snapshot["turns"][-1]["status"], "completed")
        self.assertEqual(snapshot["evaluations"][0]["package_status"], "completed")
        self.assertIn("评测方案.docx", agent.final_prompt)
        self.assertIn("样本.csv", agent.final_prompt)
        completed = [e for e in self.store.list_events(self.conversation_id)
                     if e["turn_id"] == resumed and e["type"] == "message.completed"][-1]
        self.assertEqual(set(completed["payload"]["artifact_ids"]), {
            first["artifact_id"], next(item["artifact_id"] for item in snapshot["artifacts"]
                                       if item["filename"] == "样本.csv"),
        })

    def test_repair_exhausted_keeps_file_and_explains_incomplete_state(self):
        confirmed = self._confirmed_turn()
        agent = RepairAgent(self.publisher, self.conversation_id,
                            confirmed["turn_id"], confirmed["deliverable_ids"][0], repair=False)
        asyncio.run(TurnExecutor(self.store, agent)._run(
            self.conversation_id, confirmed["turn_id"], "开始",
        ))
        snapshot = self.store.snapshot(self.conversation_id)
        self.assertEqual(agent.calls, 2)
        self.assertEqual(snapshot["turns"][-1]["status"], "failed")
        self.assertEqual(snapshot["evaluations"][0]["package_status"], "incomplete")
        self.assertIn("遗漏文件.md", snapshot["messages"][-1]["body"])
        self.assertIn("产物抽屉", snapshot["messages"][-1]["body"])
        self.assertEqual(snapshot["messages"][-1]["status"], "completed")
        self.assertTrue(Path(snapshot["artifacts"][0]["source_path"]).exists())
        resumed = self.store.begin_message_turn(
            self.conversation_id, body="继续修复未完成的方案", client_key="continue",
        )["turn_id"]
        scope = self.store.select_evaluation_scope(
            self.conversation_id, resumed, scope_kind="package_continue",
            evaluation_id=snapshot["evaluations"][0]["evaluation_id"],
        )
        self.assertEqual(scope["deliverable_ids"], confirmed["deliverable_ids"])
        self.publisher.publish(
            self.conversation_id, resumed, draft_path=f"/drafts/{resumed}/plan.md",
            filename="评测方案.docx", content="# 评测方案\n\n已修复。",
            deliverable_id=confirmed["deliverable_ids"][0], artifact_id=agent.artifact_id,
        )
        self.assertEqual(self.store.validation_issues(self.conversation_id, resumed), [])
        self.store.begin_finalization(self.conversation_id, resumed)
        self.store.finish_turn(self.conversation_id, resumed, "completed")
        self.assertEqual(self.store.snapshot(self.conversation_id)["evaluations"][0]["package_status"], "completed")

    def test_empty_conversation_reply_has_visible_failure_text(self):
        turn = self.store.begin_message_turn(
            self.conversation_id, body="你好", client_key="hello",
        )["turn_id"]
        asyncio.run(TurnExecutor(self.store, EmptyAgent())._run(
            self.conversation_id, turn, "你好",
        ))
        snapshot = self.store.snapshot(self.conversation_id)
        self.assertEqual(snapshot["turns"][-1]["status"], "failed")
        self.assertIn("没有得到可显示的回复", snapshot["messages"][-1]["body"])
        self.assertEqual(snapshot["messages"][-1]["status"], "completed")

    def test_outline_claim_without_card_gets_one_retry_then_visible_failure(self):
        turn = self.store.begin_message_turn(
            self.conversation_id, body="设计整套评测方案", client_key="missing-outline",
        )["turn_id"]
        self.store.select_evaluation_scope(
            self.conversation_id, turn, scope_kind="package_outline", product_name="产品 A",
        )
        agent = TextOnlyAgent()
        asyncio.run(TurnExecutor(self.store, agent)._run(
            self.conversation_id, turn, "设计整套评测方案",
        ))
        snapshot = self.store.snapshot(self.conversation_id)
        self.assertEqual(agent.calls, 2)
        self.assertEqual(snapshot["turns"][-1]["status"], "failed")
        self.assertIn("可确认的大纲尚未提交", snapshot["messages"][-1]["body"])
        self.assertNotIn("已提交大纲。", snapshot["messages"][-1]["body"])

    def test_unexpected_and_budget_failures_have_visible_feedback(self):
        for index, (error, expected) in enumerate((
            (RuntimeError("private server detail"), "执行遇到异常"),
            (ContextBudgetExceeded(130000, 119808), "上下文超过工作窗口"),
        )):
            with self.subTest(index=index):
                turn = self.store.begin_message_turn(
                    self.conversation_id, body="请分析", client_key=f"fail-{index}",
                )["turn_id"]
                asyncio.run(TurnExecutor(self.store, FailingAgent(error))._run(
                    self.conversation_id, turn, "请分析",
                ))
                snapshot = self.store.snapshot(self.conversation_id)
                self.assertEqual(snapshot["turns"][-1]["status"], "failed")
                self.assertIn(expected, snapshot["messages"][-1]["body"])
                self.assertNotIn("private server detail", snapshot["messages"][-1]["body"])


if __name__ == "__main__":
    unittest.main()
