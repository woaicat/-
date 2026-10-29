"""API boundary cases omitted by the original happy-path suite."""

import tempfile
import unittest
from io import BytesIO

from fastapi.testclient import TestClient

from evaluation_agent.api import MAX_UPLOAD_BYTES, create_app
from test_api import LocalApiModel, _wait_terminal


OUTLINE = ("## 评测目标\n检验质量。\n## 评测指标\n准确率。\n"
           "## 评测数据样本\n覆盖样本。\n## 评测执行方案\n离线执行。\n"
           "## 产物清单\n- 方案.md\n")


def upload(client, conversation_id, filename, data, key="upload-1"):
    return client.post(
        f"/api/conversations/{conversation_id}/attachments",
        data={"upload_key": key}, files={"file": (filename, data, "application/octet-stream")},
    )


class FunctionalEdgeTests(unittest.TestCase):
    def test_empty_duplicate_and_foreign_message_attachments(self):
        with tempfile.TemporaryDirectory() as root:
            with TestClient(create_app(data_root=root, model=LocalApiModel(), model_provider="v2api")) as client:
                a = client.post("/api/conversations", json={}).json()["conversation_id"]
                b = client.post("/api/conversations", json={}).json()["conversation_id"]
                base = f"/api/conversations/{a}/messages"
                self.assertEqual(client.post(base, json={"body": "  ", "client_key": "empty"}).status_code, 422)
                self.assertEqual(client.post(base, json={"body": "你好", "client_key": "  "}).status_code, 422)
                attachment = upload(client, a, "需求.md", b"# Requirement")
                self.assertEqual(attachment.status_code, 200)
                attachment_id = attachment.json()["attachment_id"]
                self.assertEqual(client.post(base, json={
                    "body": "请看", "client_key": "dup-ids",
                    "attachment_ids": [attachment_id, attachment_id],
                }).status_code, 422)
                self.assertEqual(client.post(f"/api/conversations/{b}/messages", json={
                    "body": "非法引用", "client_key": "foreign", "attachment_ids": [attachment_id],
                }).status_code, 404)
                sent = client.post(base, json={
                    "body": "按资料回答", "client_key": "same-request", "attachment_ids": [attachment_id],
                })
                self.assertEqual(sent.status_code, 200)
                retry = client.post(base, json={
                    "body": "按资料回答", "client_key": "same-request", "attachment_ids": [attachment_id],
                })
                self.assertEqual(retry.status_code, 200)
                self.assertEqual(retry.json()["turn_id"], sent.json()["turn_id"])
                snapshot, _ = _wait_terminal(client, a, sent.json()["turn_id"])
                self.assertEqual(len([row for row in snapshot["messages"] if row["role"] == "user"]), 1)
                self.assertEqual(client.post(base, json={
                    "body": "重复使用", "client_key": "reuse-attachment", "attachment_ids": [attachment_id],
                }).status_code, 404)
                self.assertEqual(client.get(f"/api/conversations/{b}").json()["messages"], [])

    def test_upload_file_validation_and_idempotency(self):
        with tempfile.TemporaryDirectory() as root:
            with TestClient(create_app(data_root=root)) as client:
                conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
                self.assertEqual(upload(client, conversation_id, "empty.md", b"").status_code, 422)
                self.assertEqual(upload(client, conversation_id, "bad.md", b"\xff").status_code, 422)
                self.assertEqual(upload(client, conversation_id, "bad.docx", b"not-a-zip").status_code, 422)
                self.assertEqual(upload(client, conversation_id, "bad.pdf", b"pdf").status_code, 422)
                self.assertEqual(upload(client, conversation_id, "large.md", b"x" * (MAX_UPLOAD_BYTES + 1)).status_code, 422)
                self.assertEqual(upload(client, "missing", "source.md", b"# data").status_code, 404)
                first = upload(client, conversation_id, "source.md", b"# first", key="same-file")
                repeat = upload(client, conversation_id, "other.md", b"# second", key="same-file")
                self.assertEqual(first.status_code, 200)
                self.assertEqual(repeat.status_code, 200)
                self.assertEqual(first.json()["attachment_id"], repeat.json()["attachment_id"])
                snapshot = client.get(f"/api/conversations/{conversation_id}").json()
                self.assertEqual(len(snapshot["attachments"]), 1)
                self.assertNotIn("storage_path", snapshot["attachments"][0])
                self.assertNotIn("extracted_path", snapshot["attachments"][0])

    def test_outline_revision_conflicts_and_conversation_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            with TestClient(create_app(data_root=root, model=LocalApiModel(), model_provider="v2api")) as client:
                store = client.app.state.store
                a = store.create_conversation()["conversation_id"]
                b = store.create_conversation()["conversation_id"]
                turn = store.begin_message_turn(a, body="生成整包", client_key="plan")["turn_id"]
                store.select_evaluation_scope(a, turn, scope_kind="package_outline", product_name="A")
                outline = store.submit_outline(a, turn, OUTLINE)
                store.finish_turn(a, turn, "completed")
                outline_id = outline["outline_id"]
                a_path = f"/api/conversations/{a}/outlines/{outline_id}"
                b_path = f"/api/conversations/{b}/outlines/{outline_id}"
                self.assertEqual(client.put(b_path, json={"expected_revision": 1, "body": OUTLINE}).status_code, 404)
                self.assertEqual(client.put(a_path, json={"expected_revision": 1, "body": "缺清单"}).status_code, 422)
                updated = client.put(a_path, json={"expected_revision": 1, "body": OUTLINE + "\n补充说明。"})
                self.assertEqual(updated.status_code, 200)
                self.assertEqual(updated.json()["revision"], 2)
                self.assertEqual(client.put(a_path, json={"expected_revision": 1, "body": OUTLINE}).status_code, 409)
                self.assertEqual(client.post(a_path + "/confirm", json={
                    "expected_revision": 1, "client_key": "stale",
                }).status_code, 409)
                self.assertEqual(client.post(b_path + "/confirm", json={
                    "expected_revision": 2, "client_key": "foreign",
                }).status_code, 404)
                confirmed = client.post(a_path + "/confirm", json={
                    "expected_revision": 2, "client_key": "confirm-once",
                })
                self.assertEqual(confirmed.status_code, 200)
                self.assertEqual(client.post(a_path + "/confirm", json={
                    "expected_revision": 2, "client_key": "confirm-once",
                }).json()["turn_id"], confirmed.json()["turn_id"])
                self.assertEqual(client.post(a_path + "/confirm", json={
                    "expected_revision": 2, "client_key": "confirm-twice",
                }).status_code, 409)

    def test_sse_invalid_cursor_and_no_model_error(self):
        with tempfile.TemporaryDirectory() as root:
            with TestClient(create_app(data_root=root)) as client:
                cid = client.post("/api/conversations", json={}).json()["conversation_id"]
                self.assertEqual(client.get(f"/api/conversations/{cid}/events", headers={
                    "Last-Event-ID": "not-an-integer",
                }).status_code, 422)
                self.assertEqual(client.post(f"/api/conversations/{cid}/messages", json={
                    "body": "你好", "client_key": "no-model",
                }).status_code, 503)
                self.assertEqual(client.get(f"/api/conversations/{cid}").json()["turns"], [])


if __name__ == "__main__":
    unittest.main()
