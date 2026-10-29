"""Application data isolation, event ordering and execution-slot tests."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evaluation_agent.artifacts import ArtifactPublisher
from evaluation_agent.store import (
    ConflictError, NotFoundError, WorkspaceStore, _explicit_file_mentions,
    parse_outline_deliverables,
)


OUTLINE = """## 评测目标
衡量正确性。
## 评测指标
准确率。
## 评测数据样本
代表性样本。
## 评测执行方案
离线执行。
## 产物清单
- 评测方案.docx
- 样本.csv
"""


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "workspace.sqlite"
        self.store = WorkspaceStore(self.path)
        self.a = self.store.create_conversation("A")["conversation_id"]
        self.b = self.store.create_conversation("B")["conversation_id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshots_and_events_are_isolated_across_restart(self):
        product_a = self.store.create_evaluation(self.a, "产品 A")
        self.store.create_evaluation(self.b, "产品 B")
        turn = self.store.begin_message_turn(self.a, body="请讨论", client_key="a-1")
        message = self.store.begin_assistant_message(self.a, turn["turn_id"])
        first = self.store.append_message_delta(self.a, turn["turn_id"], message, "第一段")
        self.store.append_message_delta(self.a, turn["turn_id"], message, "第二段")
        self.store.complete_assistant_message(self.a, turn["turn_id"], message)
        terminal = self.store.finish_turn(self.a, turn["turn_id"], "completed")
        self.assertLess(first, terminal)

        reopened = WorkspaceStore(self.path)
        snapshot_a = reopened.snapshot(self.a)
        snapshot_b = reopened.snapshot(self.b)
        self.assertEqual(snapshot_a["messages"][-1]["body"], "第一段第二段")
        self.assertEqual(snapshot_a["messages"][-1]["status"], "completed")
        self.assertEqual(snapshot_a["evaluations"][0]["evaluation_id"], product_a["evaluation_id"])
        self.assertEqual(snapshot_b["messages"], [])
        self.assertEqual(len(snapshot_b["evaluations"]), 1)
        self.assertEqual(reopened.list_events(self.b), [])
        self.assertEqual(reopened.list_events(self.a, after=first)[-1]["type"], "turn.completed")
        self.assertEqual(snapshot_a["last_event_id"], terminal)
        with self.assertRaises(NotFoundError):
            reopened.append_message_delta(self.b, turn["turn_id"], message, "泄漏")

    def test_active_turn_and_idempotency_are_atomic(self):
        def start(key):
            return self.store.begin_message_turn(self.a, body=key, client_key=key)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start, key) for key in ("one", "two")]
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except ConflictError as exc:
                    self.assertIsNotNone(exc.turn_id)
        self.assertEqual(len(results), 1)
        winner = results[0]
        winner_key = self.store.snapshot(self.a)["messages"][0]["body"]
        self.assertEqual(start(winner_key), winner)
        with self.assertRaises(ConflictError):
            start("third")
        self.store.finish_turn(self.a, winner["turn_id"], "completed")
        self.assertNotEqual(start("third")["turn_id"], winner["turn_id"])

    def test_attachment_cannot_cross_conversation_or_upload_while_running(self):
        attachment = self.store.create_attachment(
            self.a, upload_key="upload-1", filename="需求.md", mime_type="text/markdown",
            storage_path="a/upload-1/original.md", extracted_path="a/upload-1/extracted.md",
        )
        self.assertEqual(self.store.create_attachment(
            self.a, upload_key="upload-1", filename="需求.md", mime_type="text/markdown",
            storage_path="a/upload-1/original.md", extracted_path="a/upload-1/extracted.md",
        )["attachment_id"], attachment["attachment_id"])
        with self.assertRaises(NotFoundError):
            self.store.begin_message_turn(
                self.b, body="读取这份资料", client_key="b-1",
                attachment_ids=[attachment["attachment_id"]],
            )
        turn = self.store.begin_message_turn(
            self.a, body="读取这份资料", client_key="a-1",
            attachment_ids=[attachment["attachment_id"]],
        )
        with self.assertRaises(ConflictError):
            self.store.create_attachment(
                self.a, upload_key="upload-2", filename="补充.md", mime_type="text/markdown",
                storage_path="a/upload-2/original.md", extracted_path="a/upload-2/extracted.md",
            )
        self.store.finish_turn(self.a, turn["turn_id"], "completed")
        with self.assertRaises(NotFoundError):
            self.store.begin_message_turn(
                self.a, body="重复", client_key="a-2",
                attachment_ids=[attachment["attachment_id"]],
            )

    def test_cancel_and_crash_recovery_are_terminal(self):
        turn = self.store.begin_message_turn(self.a, body="开始", client_key="run-1")
        turn_id = turn["turn_id"]
        self.assertEqual(self.store.request_cancel(self.a, turn_id), "cancel_requested")
        with self.assertRaises(ConflictError):
            self.store.finish_turn(self.a, turn_id, "completed")
        with self.assertRaises(ConflictError):
            self.store.begin_message_turn(self.a, body="插话", client_key="run-2")
        self.assertEqual(self.store.finish_turn(self.a, turn_id, "cancelled"), self.store.snapshot(self.a)["last_event_id"])
        self.assertEqual(self.store.request_cancel(self.a, turn_id), "cancelled")

        active = self.store.begin_message_turn(self.a, body="重启前", client_key="run-3")
        reopened = WorkspaceStore(self.path)
        self.assertEqual(reopened.recover_interrupted_turns(), 1)
        self.assertEqual(reopened.recover_interrupted_turns(), 0)
        self.assertEqual(reopened.snapshot(self.a)["turns"][-1]["status"], "interrupted")
        self.assertEqual(reopened.list_events(self.a)[-1]["turn_id"], active["turn_id"])
        reopened.begin_message_turn(self.a, body="继续", client_key="run-4")

    def test_outline_requires_explicit_confirmation_and_revision(self):
        attachment = self.store.create_attachment(
            self.a, upload_key="spec", filename="需求.md", mime_type="text/markdown",
            storage_path="a/spec/original.md", extracted_path="a/spec/text.md",
        )
        plan_turn = self.store.begin_message_turn(
            self.a, body="根据资料设计一整套评测方案", client_key="plan",
            attachment_ids=[attachment["attachment_id"]],
        )["turn_id"]
        scope = self.store.select_evaluation_scope(
            self.a, plan_turn, scope_kind="package_outline", product_name="产品 A",
            attachment_ids=[attachment["attachment_id"]],
        )
        self.assertEqual(self.store.snapshot(self.a)["attachments"][0]["evaluation_id"], scope["evaluation_id"])
        with self.assertRaises(ConflictError):
            self.store.select_evaluation_scope(
                self.a, plan_turn, scope_kind="focused_create", evaluation_id=scope["evaluation_id"],
                target_names=["评测方案"],
            )
        outline = self.store.submit_outline(self.a, plan_turn, OUTLINE)
        self.store.finish_turn(self.a, plan_turn, "completed")
        with self.assertRaises(ConflictError):
            self.store.confirm_outline(self.a, outline["outline_id"], expected_revision=0, client_key="confirm")
        revised = OUTLINE.replace("准确率。", "准确率与召回率。")
        revision = self.store.edit_outline(self.a, outline["outline_id"], expected_revision=1, body=revised)
        self.assertEqual(revision, 2)
        with self.assertRaises(ConflictError):
            self.store.edit_outline(self.a, outline["outline_id"], expected_revision=1, body=OUTLINE)
        started = self.store.confirm_outline(
            self.a, outline["outline_id"], expected_revision=2, client_key="confirm"
        )
        self.assertEqual(len(started["deliverable_ids"]), 2)
        self.assertEqual(self.store.confirm_outline(
            self.a, outline["outline_id"], expected_revision=2, client_key="confirm"
        )["turn_id"], started["turn_id"])
        snapshot = self.store.snapshot(self.a)
        self.assertEqual(snapshot["outlines"][0]["body"], revised)
        self.assertEqual(snapshot["outlines"][0]["status"], "confirmed")
        self.assertEqual(snapshot["turns"][-1]["scope_kind"], "package_generate")
        self.assertEqual(snapshot["evaluations"][0]["package_status"], "running")
        with self.assertRaises(NotFoundError):
            self.store.edit_outline(self.b, outline["outline_id"], expected_revision=2, body=OUTLINE)

    def test_focused_creation_cannot_disguise_full_package(self):
        turn_id = self.store.begin_message_turn(
            self.a, body="请做一整套评测方案，先生成指标框架", client_key="package"
        )["turn_id"]
        with self.assertRaises(ConflictError):
            self.store.select_evaluation_scope(
                self.a, turn_id, scope_kind="focused_create", product_name="产品 A",
                target_names=["指标框架"],
            )
        self.store.finish_turn(self.a, turn_id, "completed")
        focused = self.store.begin_message_turn(
            self.a, body="先只做指标框架", client_key="focused"
        )["turn_id"]
        scope = self.store.select_evaluation_scope(
            self.a, focused, scope_kind="focused_create", product_name="产品 A",
            target_names=["指标框架"],
        )
        self.assertEqual(scope["target_names"], ["指标框架"])

    def test_existing_file_edit_does_not_require_new_package_outline(self):
        product = self.store.create_evaluation(self.a, "产品 A")
        creating = self.store.begin_message_turn(
            self.a, body="请创建指标框架", client_key="create-framework",
        )["turn_id"]
        self.store.select_evaluation_scope(
            self.a, creating, scope_kind="focused_create",
            evaluation_id=product["evaluation_id"], target_names=["指标框架"],
        )
        publisher = ArtifactPublisher(self.store, Path(self.temp.name) / "outputs")
        artifact = publisher.publish(
            self.a, creating, draft_path=f"/drafts/{creating}/framework.md",
            filename="指标框架.md", content="# 指标框架\n\n初稿。",
        )
        self.store.finish_turn(self.a, creating, "completed")
        editing = self.store.begin_message_turn(
            self.a, body="不是生成整套方案，请修改已有的指标框架", client_key="edit-framework",
        )["turn_id"]
        scope = self.store.select_evaluation_scope(
            self.a, editing, scope_kind="focused_edit",
            evaluation_id=product["evaluation_id"],
            target_artifact_ids=[artifact["artifact_id"]],
        )
        self.assertEqual(scope["scope_kind"], "focused_edit")

    def test_focused_edit_scope_includes_all_named_files(self):
        product = self.store.create_evaluation(self.a, "产品 A")
        creating = self.store.begin_message_turn(
            self.a, body="请创建评测方案、执行手册和产品解析", client_key="create-three",
        )["turn_id"]
        self.store.select_evaluation_scope(
            self.a, creating, scope_kind="focused_create",
            evaluation_id=product["evaluation_id"],
            target_names=["评测方案", "执行手册", "产品解析"],
        )
        publisher = ArtifactPublisher(self.store, Path(self.temp.name) / "outputs")
        ids = {}
        for name in ("评测方案", "执行手册", "产品解析"):
            result = publisher.publish(
                self.a, creating, draft_path=f"/drafts/{creating}/{name}.md",
                filename=f"{name}.md", content=f"# {name}\n\n初稿。",
            )
            ids[name] = result["artifact_id"]
        self.store.finish_turn(self.a, creating, "completed")

        editing = self.store.begin_message_turn(
            self.a,
            body="请修改评测方案和执行手册，产品解析保持原样",
            client_key="edit-two",
        )["turn_id"]
        with self.assertRaisesRegex(ConflictError, "执行手册"):
            self.store.select_evaluation_scope(
                self.a, editing, scope_kind="focused_edit",
                evaluation_id=product["evaluation_id"],
                target_artifact_ids=[ids["评测方案"]],
            )
        with self.assertRaisesRegex(ConflictError, "excluded"):
            self.store.select_evaluation_scope(
                self.a, editing, scope_kind="focused_edit",
                evaluation_id=product["evaluation_id"],
                target_artifact_ids=list(ids.values()),
            )
        scope = self.store.select_evaluation_scope(
            self.a, editing, scope_kind="focused_edit",
            evaluation_id=product["evaluation_id"],
            target_artifact_ids=[ids["评测方案"], ids["执行手册"]],
        )
        self.assertEqual(set(scope["target_artifact_ids"]), {ids["评测方案"], ids["执行手册"]})
        self.store.finish_turn(self.a, editing, "completed")
        extra = self.store.begin_message_turn(
            self.a, body="请修改评测方案和执行手册", client_key="edit-extra",
        )["turn_id"]
        with self.assertRaisesRegex(ConflictError, "outside the named request"):
            self.store.select_evaluation_scope(
                self.a, extra, scope_kind="focused_edit",
                evaluation_id=product["evaluation_id"], target_artifact_ids=list(ids.values()),
            )

    def test_compound_file_exclusion_keeps_both_files_out_of_scope(self):
        request = "请修改评测方案和执行手册，产品解析和 CSV 样本保持原样。"
        self.assertEqual(
            _explicit_file_mentions(request, "01_产品解析_产品 A.md", "产品 A"),
            (False, True),
        )
        self.assertEqual(
            _explicit_file_mentions(request, "03_评测数据样本.csv", "产品 A"),
            (False, True),
        )
        self.assertEqual(
            _explicit_file_mentions(request, "02_评测方案_产品 A.md", "产品 A"),
            (True, False),
        )

    def test_cancelled_package_keeps_ready_file_and_continues_remaining_work(self):
        planning = self.store.begin_message_turn(self.a, body="请设计一整套方案", client_key="plan")["turn_id"]
        selected = self.store.select_evaluation_scope(
            self.a, planning, scope_kind="package_outline", product_name="产品 A",
        )
        outline = self.store.submit_outline(self.a, planning, OUTLINE)
        self.store.finish_turn(self.a, planning, "completed")
        confirmed = self.store.confirm_outline(
            self.a, outline["outline_id"], expected_revision=1, client_key="confirm",
        )
        publisher = ArtifactPublisher(self.store, Path(self.temp.name) / "outputs")
        first = publisher.publish(
            self.a, confirmed["turn_id"], draft_path=f"/drafts/{confirmed['turn_id']}/plan.md",
            filename="评测方案.docx", content="# 评测方案\n\n可执行的评测步骤。",
            deliverable_id=confirmed["deliverable_ids"][0],
        )
        self.store.request_cancel(self.a, confirmed["turn_id"])
        self.store.finish_turn(self.a, confirmed["turn_id"], "cancelled")
        reopened = WorkspaceStore(self.path)
        snapshot = reopened.snapshot(self.a)
        self.assertEqual(snapshot["evaluations"][0]["package_status"], "incomplete")
        self.assertEqual(snapshot["artifacts"][0]["artifact_id"], first["artifact_id"])
        self.assertTrue(Path(snapshot["artifacts"][0]["export_path"]).is_file())
        self.assertEqual([item["status"] for item in snapshot["deliverables"]], ["ready", "pending"])
        continued = reopened.begin_message_turn(
            self.a, body="接着已有文件完成剩余工作", client_key="continue",
        )["turn_id"]
        scope = reopened.select_evaluation_scope(
            self.a, continued, scope_kind="package_continue", evaluation_id=selected["evaluation_id"],
        )
        self.assertEqual(scope["deliverable_ids"], [confirmed["deliverable_ids"][1]])
        publisher.publish(
            self.a, continued, draft_path=f"/drafts/{continued}/samples.csv",
            filename="样本.csv", content="问题,预期\n样本,答案\n",
            deliverable_id=scope["deliverable_ids"][0],
        )
        reopened.begin_finalization(self.a, continued)
        reopened.finish_turn(self.a, continued, "completed")
        completed = reopened.snapshot(self.a)
        self.assertEqual(completed["evaluations"][0]["package_status"], "completed")
        self.assertEqual(len(completed["artifacts"]), 2)

    def test_continue_after_files_ready_but_reply_interrupted(self):
        one_file = OUTLINE.replace("- 样本.csv\n", "")
        planning = self.store.begin_message_turn(self.a, body="请设计整套方案", client_key="plan")["turn_id"]
        selected = self.store.select_evaluation_scope(
            self.a, planning, scope_kind="package_outline", product_name="产品 A",
        )
        outline = self.store.submit_outline(self.a, planning, one_file)
        self.store.finish_turn(self.a, planning, "completed")
        confirmed = self.store.confirm_outline(self.a, outline["outline_id"], expected_revision=1, client_key="confirm")
        publisher = ArtifactPublisher(self.store, Path(self.temp.name) / "outputs")
        published = publisher.publish(
            self.a, confirmed["turn_id"], draft_path=f"/drafts/{confirmed['turn_id']}/plan.md",
            filename="评测方案.docx", content="# 评测方案\n\n已完成。",
            deliverable_id=confirmed["deliverable_ids"][0],
        )
        self.store.request_cancel(self.a, confirmed["turn_id"])
        self.store.finish_turn(self.a, confirmed["turn_id"], "cancelled")
        resumed = self.store.begin_message_turn(self.a, body="继续完成剩余收尾", client_key="resume")["turn_id"]
        scope = self.store.select_evaluation_scope(
            self.a, resumed, scope_kind="package_continue", evaluation_id=selected["evaluation_id"],
        )
        self.assertEqual(scope["deliverable_ids"], [])
        ready = self.store.begin_finalization(self.a, resumed)
        self.assertEqual([item["artifact_id"] for item in ready], [published["artifact_id"]])
        message_id = self.store.begin_assistant_message(self.a, resumed)
        self.store.append_message_delta(self.a, resumed, message_id, "已完成，可预览。")
        self.store.complete_assistant_message(
            self.a, resumed, message_id, artifact_ids=[published["artifact_id"]],
        )
        self.store.finish_turn(self.a, resumed, "completed")
        self.assertEqual(self.store.snapshot(self.a)["evaluations"][0]["package_status"], "completed")
        completed = [event for event in self.store.list_events(self.a) if event["type"] == "message.completed"][-1]
        self.assertEqual(completed["payload"]["artifact_ids"], [published["artifact_id"]])

    def test_two_products_keep_separate_folders_in_one_conversation(self):
        publisher = ArtifactPublisher(self.store, Path(self.temp.name) / "outputs")
        for index, (product_name, target) in enumerate((
            ("客服助手", "指标说明"), ("投研助手", "样本规范"),
        )):
            turn_id = self.store.begin_message_turn(
                self.a, body=f"只为{product_name}生成{target}", client_key=f"product-{index}",
            )["turn_id"]
            scope = self.store.select_evaluation_scope(
                self.a, turn_id, scope_kind="focused_create",
                product_name=product_name, target_names=[target],
            )
            publisher.publish(
                self.a, turn_id, draft_path=f"/drafts/{turn_id}/{target}.md",
                filename=f"{target}.md", content=f"# {product_name}{target}\n\n正文。",
            )
            self.store.begin_finalization(self.a, turn_id)
            self.store.finish_turn(self.a, turn_id, "completed")
            self.assertEqual(self.store.snapshot(self.a)["turns"][-1]["evaluation_id"], scope["evaluation_id"])
        snapshot = self.store.snapshot(self.a)
        by_id = {item["evaluation_id"]: item["name"] for item in snapshot["evaluations"]}
        self.assertEqual(len(by_id), 2)
        self.assertEqual({(by_id[item["evaluation_id"]], item["filename"]) for item in snapshot["artifacts"]}, {
            ("客服助手", "指标说明.md"), ("投研助手", "样本规范.md"),
        })

    def test_outline_parser_rejects_missing_or_unsafe_files(self):
        self.assertEqual(parse_outline_deliverables(OUTLINE)[0], ("评测方案.docx", "docx"))
        with self.assertRaises(ValueError):
            parse_outline_deliverables(OUTLINE.replace("- 评测方案.docx", "- ../评测方案.docx"))
        with self.assertRaises(ValueError):
            parse_outline_deliverables(OUTLINE.replace("## 评测目标", "## 背景"))

    def test_outline_parser_accepts_model_style_lists_without_loosening_file_allowlist(self):
        prefix = OUTLINE.split("## 产物清单", 1)[0]
        variants = (
            "## 五、交付文件清单\n| 文件名 | 内容说明 |\n|---|---|\n"
            "| `评测方案.md` | 指标与执行步骤 |\n| `样本.csv` | 示例数据 |",
            "## 五、产物清单（本次交付文件）\n"
            "1. 评测方案.md —— 指标与执行步骤\n2. 样本.csv —— 示例数据",
            "## 产物清单\n- `评测方案.md`：指标与执行步骤\n- `样本.csv`：示例数据",
        )
        for variant in variants:
            with self.subTest(variant=variant.splitlines()[0]):
                self.assertEqual(parse_outline_deliverables(prefix + variant), [
                    ("评测方案.md", "md"), ("样本.csv", "csv"),
                ])
        with self.assertRaisesRegex(ValueError, "仅支持"):
            parse_outline_deliverables(prefix +
                "## 产物清单\n- `评测方案.md`：说明\n- `样本.jsonl`：说明")
        with self.assertRaises(ValueError):
            parse_outline_deliverables(prefix +
                "## 产物清单\n| `../评测方案.md` | 不能使用路径 |")
        with self.assertRaises(ValueError):
            parse_outline_deliverables(prefix +
                "## 产物清单\n1. 评测方案.md\n2. `评测方案.md`：重复")


if __name__ == "__main__":
    unittest.main()
