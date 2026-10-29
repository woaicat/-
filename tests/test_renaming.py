"""Batch rename preserves bytes, permissions, approved outlines and stable IDs."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from evaluation_agent.api import create_app
from evaluation_agent.artifacts import ArtifactPublisher
from evaluation_agent.store import ConflictError, NotFoundError, WorkspaceStore
from test_api import _wait_terminal
from test_product_tools import WorkflowModel


NAMES = ["00_overview.md", "01_metrics.md", "02_samples.md", "03_methods.md", "04_manual.md", "05_scores.csv", "06_report.docx"]
CHINESE = ["00_方案总览.md", "01_评测指标.md", "02_样本设计.md", "03_评测方法.md", "04_执行手册.md", "05_评分表.csv", "06_报告模板.docx"]


def seed_package(store, publisher, *, partial=False):
    cid = store.create_conversation("批量改名验收")["conversation_id"]
    tid = store.begin_message_turn(cid, body="设计整套评测方案", client_key="plan")["turn_id"]
    eid = store.select_evaluation_scope(cid, tid, scope_kind="package_outline", product_name="研究智能体")["evaluation_id"]
    outline = store.submit_outline(cid, tid, "## 评测目标\n验证质量\n## 评测指标\n准确率\n## 评测数据样本\n覆盖关键流程\n## 评测执行方案\n离线评测\n## 产物清单\n" + "\n".join("- " + name for name in NAMES))
    store.finish_turn(cid, tid, "completed")
    confirmed = store.confirm_outline(cid, outline["outline_id"], expected_revision=1, client_key="confirm")
    for name, did in list(zip(NAMES, confirmed["deliverable_ids"]))[:3 if partial else 7]:
        publisher.publish(cid, confirmed["turn_id"], draft_path=f"/drafts/{confirmed['turn_id']}/file.md",
                          filename=name, deliverable_id=did, content="id,问题\n1,示例\n" if name.endswith(".csv") else
                          "# 评测设计\n\n[指标](01_metrics.md)\n\n保持原始正文。")
    if not partial:
        store.begin_finalization(cid, confirmed["turn_id"])
    store.finish_turn(cid, confirmed["turn_id"], "failed" if partial else "completed")
    return cid, eid


class RenameModel(WorkflowModel):
    evaluation_id: str = ""
    renames: list[dict] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        if isinstance(last, HumanMessage) and "本轮文件已完成服务端校验" in str(last.content):
            response = AIMessage(content="文件名已改为中文，正文和内容版本保持原样。")
        elif isinstance(last, HumanMessage):
            response = self._tool_call("select_evaluation", {
                "scope_kind": "focused_rename", "evaluation_id": self.evaluation_id,
                "target_artifact_ids": [item["artifact_id"] for item in self.renames],
            })
        elif isinstance(last, ToolMessage) and last.name == "select_evaluation":
            response = self._tool_call("rename_artifacts", {"renames": self.renames})
        else:
            response = AIMessage(content="改名完成。")
        return ChatResult(generations=[ChatGeneration(message=response)])


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = WorkspaceStore(self.root / "workspace.sqlite")
        self.publisher = ArtifactPublisher(self.store, self.root / "outputs")
        self.cid, self.eid = seed_package(self.store, self.publisher)
        self.before = self.store.snapshot(self.cid)
        self.renames = [{"artifact_id": item["artifact_id"], "filename": name}
                        for item, name in zip(self.before["artifacts"], CHINESE)]

    def turn(self, ids=None):
        tid = self.store.begin_message_turn(self.cid, body="把文件名都改成中文吧", client_key=str(len(self.store.snapshot(self.cid)["turns"])))["turn_id"]
        self.store.select_evaluation_scope(self.cid, tid, scope_kind="focused_rename", evaluation_id=self.eid,
                                           target_artifact_ids=ids or [item["artifact_id"] for item in self.renames])
        return tid

    def test_seven_renames_preserve_bytes_versions_ids_and_are_idempotent(self):
        hashes = {item["artifact_id"]: (hashlib.sha256(Path(item["source_path"]).read_bytes()).hexdigest(),
                                       hashlib.sha256(Path(item["export_path"]).read_bytes()).hexdigest())
                  for item in self.before["artifacts"]}
        tid = self.turn()
        result = self.store.rename_artifacts(self.cid, tid, self.renames)
        self.assertEqual(self.store.rename_artifacts(self.cid, tid, self.renames), result)
        after = self.store.snapshot(self.cid)
        self.assertEqual([item["filename"] for item in after["artifacts"]], CHINESE)
        self.assertEqual(after["outlines"], self.before["outlines"])
        self.assertEqual(after["deliverables"], self.before["deliverables"])
        for old, current in zip(self.before["artifacts"], after["artifacts"]):
            self.assertEqual(current["content_revision"], old["content_revision"])
            self.assertEqual(current["source_path"], old["source_path"])
            self.assertEqual(hashes[current["artifact_id"]], (hashlib.sha256(Path(current["source_path"]).read_bytes()).hexdigest(),
                                                             hashlib.sha256(Path(current["export_path"]).read_bytes()).hexdigest()))
        self.assertEqual(len(after["artifact_aliases"]), 7)
        self.assertEqual(self.store.validation_issues(self.cid, tid), [])
        self.store.begin_finalization(self.cid, tid)
        self.store.finish_turn(self.cid, tid, "completed")
        with TestClient(create_app(data_root=self.root)) as client:
            for item in result["artifacts"]:
                url = f"/api/conversations/{self.cid}/artifacts/{item['artifact_id']}"
                self.assertEqual(client.get(url + "/preview").status_code, 200)
                downloaded = client.get(url + "/download")
                self.assertEqual(downloaded.status_code, 200)
                from urllib.parse import unquote
                self.assertIn(item["filename"], unquote(downloaded.headers["content-disposition"]))

    def test_invalid_batch_does_not_partially_rename(self):
        tid = self.turn()
        for invalid in ("../非法.md", "改成其他类型.csv", "00_方案总览.md"):
            broken = [dict(item) for item in self.renames]
            broken[1]["filename"] = invalid
            with self.assertRaises((ValueError, ConflictError)):
                self.store.rename_artifacts(self.cid, tid, broken)
            self.assertEqual(self.store.snapshot(self.cid)["artifacts"], self.before["artifacts"])
        self.assertEqual(self.store.snapshot(self.cid)["artifact_aliases"], [])

    def test_database_failure_mid_batch_rolls_back_names_aliases_audit_and_events(self):
        tid = self.turn()
        with self.store._transaction() as conn:
            conn.execute("CREATE TRIGGER fail_rename BEFORE UPDATE OF filename ON artifacts WHEN NEW.filename='01_评测指标.md' BEGIN SELECT RAISE(ABORT,'simulated write failure'); END")
        cursor = self.store.snapshot(self.cid)["last_event_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.rename_artifacts(self.cid, tid, self.renames)
        after = self.store.snapshot(self.cid)
        self.assertEqual(after["artifacts"], self.before["artifacts"])
        self.assertEqual(after["artifact_aliases"], [])
        self.assertEqual(after["last_event_id"], cursor)

    def test_cancellation_before_or_after_batch_has_only_all_or_nothing_results(self):
        tid = self.turn()
        self.store.request_cancel(self.cid, tid)
        with self.assertRaises(ConflictError):
            self.store.rename_artifacts(self.cid, tid, self.renames)
        self.assertEqual(self.store.snapshot(self.cid)["artifacts"], self.before["artifacts"])
        self.store.finish_turn(self.cid, tid, "cancelled")
        tid = self.turn()
        self.store.rename_artifacts(self.cid, tid, self.renames)
        self.store.request_cancel(self.cid, tid)
        self.store.finish_turn(self.cid, tid, "cancelled")
        after = self.store.snapshot(self.cid)
        self.assertEqual([item["filename"] for item in after["artifacts"]], CHINESE)
        cursor = after["last_event_id"]
        with self.assertRaises(ConflictError):
            self.store.rename_artifacts(self.cid, tid, self.renames)
        self.assertEqual(self.store.snapshot(self.cid)["last_event_id"], cursor)

    def test_scope_and_conversation_boundaries_and_no_content_publish_permission(self):
        tid = self.turn()
        with self.assertRaises(ConflictError):
            self.store.rename_artifacts(self.cid, tid, self.renames[:-1])
        with self.assertRaises(ConflictError):
            self.publisher.publish(self.cid, tid, draft_path=f"/drafts/{tid}/file.md",
                                   filename=NAMES[0], content="不应重写正文", artifact_id=self.renames[0]["artifact_id"])
        other = self.store.create_conversation()["conversation_id"]
        with self.assertRaises(NotFoundError):
            self.store.rename_artifacts(other, tid, self.renames)
        self.assertEqual(self.store.snapshot(self.cid)["artifacts"], self.before["artifacts"])

    def test_filename_only_request_cannot_be_authorized_as_content_edit(self):
        tid = self.store.begin_message_turn(self.cid, body="把文件名都改成中文吧", client_key="wrong-scope")["turn_id"]
        with self.assertRaises(ConflictError):
            self.store.select_evaluation_scope(self.cid, tid, scope_kind="focused_edit", evaluation_id=self.eid,
                                               target_artifact_ids=[item["artifact_id"] for item in self.renames])

    def test_old_names_are_reserved_and_authorized_old_name_edits_still_work(self):
        tid = self.turn()
        self.store.rename_artifacts(self.cid, tid, self.renames)
        self.store.begin_finalization(self.cid, tid)
        self.store.finish_turn(self.cid, tid, "completed")
        tid = self.store.begin_message_turn(self.cid, body="创建00_overview", client_key="alias-create")["turn_id"]
        self.store.select_evaluation_scope(self.cid, tid, scope_kind="focused_create", evaluation_id=self.eid, target_names=["00_overview"])
        with self.assertRaises(ConflictError):
            self.publisher.publish(self.cid, tid, draft_path=f"/drafts/{tid}/new.md", filename=NAMES[0], content="# 不应覆盖旧名")
        self.store.finish_turn(self.cid, tid, "failed")
        tid = self.store.begin_message_turn(self.cid, body="修改00_overview.md的正文", client_key="alias-edit")["turn_id"]
        self.store.select_evaluation_scope(self.cid, tid, scope_kind="focused_edit", evaluation_id=self.eid,
                                           target_artifact_ids=[self.renames[0]["artifact_id"]])
        self.publisher.publish(self.cid, tid, draft_path=f"/drafts/{tid}/edited.md", filename=CHINESE[0],
                               content="# 已修改正文\n\n[指标](01_metrics.md)", artifact_id=self.renames[0]["artifact_id"])
        self.assertEqual(self.store.validation_issues(self.cid, tid), [])

    def test_schema_one_upgrade_preserves_existing_files_and_records(self):
        with self.store._transaction() as conn:
            conn.execute("DROP TABLE artifact_aliases")
            conn.execute("DROP TABLE artifact_rename_batches")
            conn.execute("PRAGMA user_version=1")
        upgraded = WorkspaceStore(self.root / "workspace.sqlite")
        self.assertEqual(upgraded.snapshot(self.cid)["artifacts"], self.before["artifacts"])
        self.assertEqual(upgraded.snapshot(self.cid)["outlines"], self.before["outlines"])
        tid = upgraded.begin_message_turn(self.cid, body="把文件名都改成中文吧", client_key="upgrade-rename")["turn_id"]
        upgraded.select_evaluation_scope(self.cid, tid, scope_kind="focused_rename", evaluation_id=self.eid,
                                         target_artifact_ids=[item["artifact_id"] for item in self.renames])
        self.assertEqual(len(upgraded.rename_artifacts(self.cid, tid, self.renames)["artifacts"]), 7)

    def test_partial_package_can_be_renamed_then_continued_without_changing_confirmation(self):
        cid, eid = seed_package(self.store, self.publisher, partial=True)
        before = self.store.snapshot(cid)
        tid = self.store.begin_message_turn(cid, body="把文件名都改成中文吧", client_key="rename")["turn_id"]
        pairs = [{"artifact_id": row["artifact_id"], "filename": CHINESE[index]} for index, row in enumerate(before["artifacts"])]
        self.store.select_evaluation_scope(cid, tid, scope_kind="focused_rename", evaluation_id=eid,
                                           target_artifact_ids=[row["artifact_id"] for row in pairs])
        self.store.rename_artifacts(cid, tid, pairs)
        self.store.begin_finalization(cid, tid)
        self.store.finish_turn(cid, tid, "completed")
        tid = self.store.begin_message_turn(cid, body="继续剩余工作", client_key="continue")["turn_id"]
        self.store.select_evaluation_scope(cid, tid, scope_kind="package_continue", evaluation_id=eid)
        for row in self.store.snapshot(cid)["deliverables"]:
            if row["status"] == "pending":
                self.publisher.publish(cid, tid, draft_path=f"/drafts/{tid}/file.md", filename=row["name"],
                                       deliverable_id=row["deliverable_id"], content="id,问题\n1,示例\n" if row["kind"] == "csv" else "# 手册\n\n[指标](01_metrics.md)")
        self.assertEqual(self.store.validation_issues(cid, tid), [])
        self.store.begin_finalization(cid, tid)
        self.store.finish_turn(cid, tid, "completed")
        self.assertEqual(self.store.snapshot(cid)["outlines"], before["outlines"])

    def test_model_tool_path_renames_once_without_document_reads_or_rewrites(self):
        model = RenameModel(evaluation_id=self.eid, renames=self.renames)
        with TestClient(create_app(data_root=self.root, model=model, model_provider="rename-test")) as client:
            result = client.post(f"/api/conversations/{self.cid}/messages",
                                 json={"body": "把文件名都改成中文吧", "client_key": "model-rename"})
            after, turn = _wait_terminal(client, self.cid, result.json()["turn_id"])
            self.assertEqual(turn["status"], "completed")
            self.assertEqual(model.serial, 2)  # select_evaluation + rename_artifacts, no file tools.
            self.assertEqual([row["filename"] for row in after["artifacts"]], CHINESE)
            self.assertEqual(len(after["messages"][-1]["artifact_ids"]), 7)
            self.assertTrue(all(row["content_revision"] == 1 for row in after["artifacts"]))
