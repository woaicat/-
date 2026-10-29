"""Publication authorization, incremental visibility and cancellation races."""

import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from evaluation_agent.artifacts import ArtifactPublisher
from evaluation_agent.store import ConflictError, NotFoundError, WorkspaceStore


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
- 样本.csv
"""


class ArtifactPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WorkspaceStore(root / "workspace.sqlite")
        self.publisher = ArtifactPublisher(self.store, root / "outputs")
        self.conversation_id = self.store.create_conversation()["conversation_id"]

    def tearDown(self):
        self.temp.cleanup()

    def _confirmed_package(self):
        conversation_id = self.conversation_id
        plan_turn = self.store.begin_message_turn(
            conversation_id, body="请设计一整套评测方案", client_key="plan"
        )["turn_id"]
        evaluation = self.store.select_evaluation_scope(
            conversation_id, plan_turn, scope_kind="package_outline", product_name="产品 A"
        )["evaluation_id"]
        outline = self.store.submit_outline(conversation_id, plan_turn, OUTLINE)
        self.store.finish_turn(conversation_id, plan_turn, "completed")
        generate = self.store.confirm_outline(
            conversation_id, outline["outline_id"], expected_revision=1, client_key="confirm"
        )
        return evaluation, generate

    def test_first_file_visible_before_turn_ends_and_continue_remaining(self):
        evaluation, generate = self._confirmed_package()
        turn_id = generate["turn_id"]
        deliverable_docx, deliverable_csv = generate["deliverable_ids"]
        document = self.publisher.publish(
            self.conversation_id, turn_id,
            draft_path=f"/drafts/{turn_id}/plan.md",
            filename="评测方案.docx",
            content="# 评测方案\n\n## 指标\n- 准确率\n",
            deliverable_id=deliverable_docx,
        )
        snapshot = self.store.snapshot(self.conversation_id)
        self.assertEqual(snapshot["turns"][-1]["status"], "running")
        self.assertEqual(len(snapshot["artifacts"]), 1)
        self.assertEqual(snapshot["artifacts"][0]["artifact_id"], document["artifact_id"])
        self.assertIn("评测方案", Document(snapshot["artifacts"][0]["export_path"]).paragraphs[0].text)
        self.assertEqual(self.store.list_events(self.conversation_id)[-1]["type"], "artifact.ready")
        with self.assertRaises(ConflictError):
            self.store.finish_turn(self.conversation_id, turn_id, "completed")

        self.store.request_cancel(self.conversation_id, turn_id)
        with self.assertRaises(ConflictError):
            self.publisher.publish(
                self.conversation_id, turn_id,
                draft_path=f"/drafts/{turn_id}/sample.csv",
                filename="样本.csv", content="id,answer\n1,yes\n",
                deliverable_id=deliverable_csv,
            )
        self.store.finish_turn(self.conversation_id, turn_id, "cancelled")
        snapshot = self.store.snapshot(self.conversation_id)
        self.assertEqual(snapshot["evaluations"][0]["package_status"], "incomplete")
        self.assertEqual(snapshot["deliverables"][0]["status"], "ready")
        self.assertEqual(snapshot["deliverables"][1]["status"], "pending")
        self.assertEqual(len(snapshot["artifacts"]), 1)

        continued = self.store.begin_message_turn(
            self.conversation_id, body="继续完成剩余工作", client_key="continue"
        )["turn_id"]
        scope = self.store.select_evaluation_scope(
            self.conversation_id, continued, scope_kind="package_continue", evaluation_id=evaluation
        )
        self.assertEqual(scope["deliverable_ids"], [deliverable_csv])
        sample = self.publisher.publish(
            self.conversation_id, continued,
            draft_path=f"/drafts/{continued}/sample.csv",
            filename="样本.csv", content="id,answer\n1,yes\n",
            deliverable_id=deliverable_csv,
        )
        self.assertNotEqual(sample["artifact_id"], document["artifact_id"])
        self.store.finish_turn(self.conversation_id, continued, "completed")
        final = self.store.snapshot(self.conversation_id)
        self.assertEqual(final["evaluations"][0]["package_status"], "completed")
        self.assertEqual(len(final["artifacts"]), 2)
        self.assertEqual([row["status"] for row in final["deliverables"]], ["ready", "ready"])

    def test_wrong_scope_path_product_and_invalid_csv_are_rejected(self):
        _, generate = self._confirmed_package()
        turn_id = generate["turn_id"]
        other_conversation = self.store.create_conversation()["conversation_id"]
        arguments = dict(
            draft_path=f"/drafts/{turn_id}/sample.csv", filename="样本.csv",
            content="id,answer\n1,yes\n", deliverable_id=generate["deliverable_ids"][1],
        )
        with self.assertRaises(ValueError):
            self.publisher.publish(self.conversation_id, turn_id, **{**arguments, "draft_path": "/drafts/other/sample.csv"})
        with self.assertRaises(NotFoundError):
            self.publisher.publish(other_conversation, turn_id, **arguments)
        with self.assertRaises(ConflictError):
            self.publisher.publish(self.conversation_id, turn_id, **{**arguments, "deliverable_id": generate["deliverable_ids"][0]})
        with self.assertRaises(ValueError):
            self.publisher.publish(self.conversation_id, turn_id, **{**arguments, "content": "id,answer\n1\n"})
        self.assertEqual(self.store.snapshot(self.conversation_id)["artifacts"], [])

    def test_package_reference_resolves_against_confirmed_file_list(self):
        _, generate = self._confirmed_package()
        turn_id = generate["turn_id"]
        self.publisher.publish(
            self.conversation_id, turn_id,
            draft_path=f"/drafts/{turn_id}/plan.md",
            filename="评测方案.docx",
            content="# 评测方案\n\n样本见[样本表](样本.csv)。",
            deliverable_id=generate["deliverable_ids"][0],
        )
        self.assertIn("尚未生成：样本.csv", self.store.validation_issues(self.conversation_id, turn_id))
        self.publisher.publish(
            self.conversation_id, turn_id,
            draft_path=f"/drafts/{turn_id}/samples.csv",
            filename="样本.csv", content="id,answer\n1,yes\n",
            deliverable_id=generate["deliverable_ids"][1],
        )
        self.assertEqual(self.store.validation_issues(self.conversation_id, turn_id), [])
        self.store.begin_finalization(self.conversation_id, turn_id)

    def test_focused_edit_keeps_stable_artifact_identity(self):
        create_turn = self.store.begin_message_turn(
            self.conversation_id, body="先只做指标框架", client_key="focused-create"
        )["turn_id"]
        product_id = self.store.select_evaluation_scope(
            self.conversation_id, create_turn, scope_kind="focused_create",
            product_name="产品 A", target_names=["指标框架"],
        )["evaluation_id"]
        created = self.publisher.publish(
            self.conversation_id, create_turn,
            draft_path=f"/drafts/{create_turn}/metrics.md", filename="指标框架.md",
            content="# 指标框架\n\n准确率。",
        )
        self.store.finish_turn(self.conversation_id, create_turn, "completed")
        edit_turn = self.store.begin_message_turn(
            self.conversation_id, body="把指标框架里的准确率阈值改掉", client_key="focused-edit"
        )["turn_id"]
        self.store.select_evaluation_scope(
            self.conversation_id, edit_turn, scope_kind="focused_edit",
            evaluation_id=product_id, target_artifact_ids=[created["artifact_id"]],
        )
        updated = self.publisher.publish(
            self.conversation_id, edit_turn,
            draft_path=f"/drafts/{edit_turn}/metrics.md", filename="指标框架.md",
            content="# 指标框架\n\n准确率阈值 0.9。", artifact_id=created["artifact_id"],
        )
        self.assertEqual(created["artifact_id"], updated["artifact_id"])
        self.assertEqual(updated["content_revision"], 2)
        snapshot = self.store.snapshot(self.conversation_id)
        self.assertEqual(len(snapshot["artifacts"]), 1)
        self.assertIn("0.9", Path(snapshot["artifacts"][0]["source_path"]).read_text())
        self.assertEqual(self.store.list_events(self.conversation_id)[-1]["type"], "artifact.updated")

    def test_partial_focused_edit_persists_updated_and_remaining_targets(self):
        create_turn = self.store.begin_message_turn(
            self.conversation_id, body="请生成指标框架和样本说明", client_key="create-two",
        )["turn_id"]
        evaluation_id = self.store.select_evaluation_scope(
            self.conversation_id, create_turn, scope_kind="focused_create",
            product_name="产品 A", target_names=["指标框架", "样本说明"],
        )["evaluation_id"]
        created = []
        for name in ("指标框架", "样本说明"):
            created.append(self.publisher.publish(
                self.conversation_id, create_turn,
                draft_path=f"/drafts/{create_turn}/{name}.md",
                filename=f"{name}.md", content=f"# {name}\n\n初版。",
            )["artifact_id"])
        self.store.finish_turn(self.conversation_id, create_turn, "completed")
        edit_turn = self.store.begin_message_turn(
            self.conversation_id, body="修改这两个文件", client_key="edit-two",
        )["turn_id"]
        self.store.select_evaluation_scope(
            self.conversation_id, edit_turn, scope_kind="focused_edit",
            evaluation_id=evaluation_id, target_artifact_ids=created,
        )
        self.publisher.publish(
            self.conversation_id, edit_turn,
            draft_path=f"/drafts/{edit_turn}/metrics.md",
            filename="指标框架.md", content="# 指标框架\n\n已更新。",
            artifact_id=created[0],
        )
        self.store.request_cancel(self.conversation_id, edit_turn)
        self.store.finish_turn(self.conversation_id, edit_turn, "cancelled")
        snapshot = self.store.snapshot(self.conversation_id)
        partial = json.loads(snapshot["turns"][-1]["scope_json"])["partial_edit"]
        self.assertEqual(partial["updated_artifact_ids"], [created[0]])
        self.assertEqual(partial["remaining_artifact_ids"], [created[1]])
        revisions = {item["artifact_id"]: item["content_revision"] for item in snapshot["artifacts"]}
        self.assertEqual(revisions, {created[0]: 2, created[1]: 1})


if __name__ == "__main__":
    unittest.main()
