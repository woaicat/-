"""Disposable HTTP server for browser acceptance. Never points at production storage.

Run only through functional_browser_acceptance.py. The /__test endpoints exist in
this test process, not in the product application.
"""

import argparse
import asyncio
import json
import re
from pathlib import Path

import uvicorn
from fastapi import HTTPException
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from evaluation_agent.api import create_app
from evaluation_agent.artifacts import ArtifactPublisher
from evaluation_agent.model_config import model_from_environment
from evaluation_agent.runtime import ContextBudget
from test_api import LocalApiModel, SlowAfterOutlineModel
from test_product_tools import WorkflowModel
from test_context import ContextModel, TEST_BUDGET
from test_renaming import RenameModel, CHINESE, seed_package


class MarkdownModel(LocalApiModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="## 测试回复\n\n**Markdown 已渲染**\n\n| 项目 | 结果 |\n|---|---|\n| 发送 | 成功 |"
        ))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for part in ("## 测试回复\n\n", "**Markdown 已渲染**\n\n", "| 项目 | 结果 |\n|---|---|\n| 发送 | 成功 |"):
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))
            await asyncio.sleep(0.45)


class FailureModel(LocalApiModel):
    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("Intentional isolated acceptance failure")
        yield  # Makes this an async generator for the model interface.


class EditModel(WorkflowModel):
    target_artifact_id: str = ""
    evaluation_id: str = ""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        content = str(last.content)
        if isinstance(last, HumanMessage) and "本轮文件已完成服务端校验" in content:
            response = AIMessage(content="已将指标框架的准确率阈值改为 90%，其他文件未修改。")
        elif isinstance(last, HumanMessage):
            match = re.search(r"/drafts/([0-9a-f]+)/", content)
            if match:
                self.turn_id = match.group(1)
            response = self._tool_call("select_evaluation", {
                "scope_kind": "focused_edit", "evaluation_id": self.evaluation_id,
                "target_artifact_ids": [self.target_artifact_id],
            })
        elif isinstance(last, ToolMessage) and last.name == "select_evaluation":
            response = self._tool_call("read_file", {
                "file_path": f"/artifacts/{self.evaluation_id}/{self.target_artifact_id}.md",
            })
        elif isinstance(last, ToolMessage) and last.name == "read_file":
            response = self._tool_call("write_file", {
                "file_path": f"/drafts/{self.turn_id}/metrics.md",
                "content": "# 指标框架\n\n准确率阈值调整为 90%。\n",
            })
        elif isinstance(last, ToolMessage) and last.name == "write_file":
            response = self._tool_call("publish_artifact", {
                "draft_path": f"/drafts/{self.turn_id}/metrics.md",
                "filename": "指标框架.md", "artifact_id": self.target_artifact_id,
            })
        else:
            response = AIMessage(content="目标文件已修改。")
        return ChatResult(generations=[ChatGeneration(message=response)])


class ContinueModel(WorkflowModel):
    evaluation_id: str = ""
    deliverable_id: str = ""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        content = str(last.content)
        if isinstance(last, HumanMessage) and "本轮文件已完成服务端校验" in content:
            response = AIMessage(content="已接着已有文件补齐剩余产物，两份文件都可以预览。")
        elif isinstance(last, HumanMessage):
            match = re.search(r"/drafts/([0-9a-f]+)/", content)
            if match:
                self.turn_id = match.group(1)
            response = self._tool_call("select_evaluation", {
                "scope_kind": "package_continue", "evaluation_id": self.evaluation_id,
            })
        elif isinstance(last, ToolMessage) and last.name == "select_evaluation":
            response = self._tool_call("write_file", {
                "file_path": f"/drafts/{self.turn_id}/samples.csv",
                "content": "问题,预期\n示例问题,示例答案\n",
            })
        elif isinstance(last, ToolMessage) and last.name == "write_file":
            response = self._tool_call("publish_artifact", {
                "draft_path": f"/drafts/{self.turn_id}/samples.csv",
                "filename": "样本.csv", "deliverable_id": self.deliverable_id,
            })
        else:
            response = AIMessage(content="剩余文件已提交。")
        return ChatResult(generations=[ChatGeneration(message=response)])


class CancelContinueModel(WorkflowModel):
    phase: str = "generation"
    evaluation_id: str = ""
    first_deliverable_id: str = ""
    second_deliverable_id: str = ""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        if isinstance(last, ToolMessage) and last.name == "publish_artifact" and self.phase == "generation":
            await asyncio.sleep(120)
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        content = str(last.content)
        if isinstance(last, HumanMessage) and "本轮文件已完成服务端校验" in content:
            response = AIMessage(content="剩余文件已补齐，全部产物可以预览。")
        elif isinstance(last, HumanMessage) and "接着已有文件" in content:
            self.phase = "continue"
            match = re.search(r"/drafts/([0-9a-f]+)/", content)
            if match:
                self.turn_id = match.group(1)
            response = self._tool_call("select_evaluation", {
                "scope_kind": "package_continue", "evaluation_id": self.evaluation_id,
            })
        elif isinstance(last, HumanMessage):
            self.phase = "generation"
            match = re.search(r"/drafts/([0-9a-f]+)/", content)
            first = re.search(r"([0-9a-f]{32})：评测方案.md", content)
            second = re.search(r"([0-9a-f]{32})：样本.csv", content)
            if match:
                self.turn_id = match.group(1)
            if first:
                self.first_deliverable_id = first.group(1)
            if second:
                self.second_deliverable_id = second.group(1)
            response = self._tool_call("write_file", {
                "file_path": f"/drafts/{self.turn_id}/plan.md",
                "content": "# 评测方案\n\n第一份文件已完成。\n",
            })
        elif isinstance(last, ToolMessage) and last.name == "select_evaluation":
            response = self._tool_call("write_file", {
                "file_path": f"/drafts/{self.turn_id}/samples.csv",
                "content": "问题,预期\n示例问题,示例答案\n",
            })
        elif isinstance(last, ToolMessage) and last.name == "write_file":
            first = self.phase == "generation"
            response = self._tool_call("publish_artifact", {
                "draft_path": f"/drafts/{self.turn_id}/{'plan.md' if first else 'samples.csv'}",
                "filename": "评测方案.md" if first else "样本.csv",
                "deliverable_id": self.first_deliverable_id if first else self.second_deliverable_id,
            })
        else:
            response = AIMessage(content="本轮文件已提交。")
        return ChatResult(generations=[ChatGeneration(message=response)])


def seed_pending_outline(app, model):
    store = app.state.store
    conversation_id = store.create_conversation("已终止任务续做验收")["conversation_id"]
    planning = store.begin_message_turn(
        conversation_id, body="为产品 A 设计完整评测方案", client_key="seed-plan",
    )["turn_id"]
    selected = store.select_evaluation_scope(
        conversation_id, planning, scope_kind="package_outline", product_name="产品 A",
    )
    outline = store.submit_outline(conversation_id, planning,
        "## 评测目标\n检验产品质量。\n## 评测指标\n准确率。\n"
        "## 评测数据样本\n覆盖典型问题。\n## 评测执行方案\n离线执行。\n"
        "## 产物清单\n- 评测方案.md\n- 样本.csv\n")
    store.finish_turn(conversation_id, planning, "completed")
    model.evaluation_id = selected["evaluation_id"]
    return {"conversation_id": conversation_id, "outline_id": outline["outline_id"]}


def seed_cancelled_package(app, root, model, *, running=False):
    store = app.state.store
    publisher = ArtifactPublisher(store, root / "outputs")
    pending = seed_pending_outline(app, model)
    conversation_id = pending["conversation_id"]
    confirmed = store.confirm_outline(
        conversation_id, pending["outline_id"], expected_revision=1,
        client_key="seed-confirm",
    )
    first = publisher.publish(conversation_id, confirmed["turn_id"],
        draft_path=f"/drafts/{confirmed['turn_id']}/plan.md",
        filename="评测方案.md", content="# 评测方案\n\n已完成的设计思路。\n",
        deliverable_id=confirmed["deliverable_ids"][0])
    if not running:
        store.request_cancel(conversation_id, confirmed["turn_id"])
        store.finish_turn(conversation_id, confirmed["turn_id"], "cancelled")
    model.deliverable_id = confirmed["deliverable_ids"][1]
    return {"conversation_id": conversation_id, "first_artifact_id": first["artifact_id"]}


def seed_edit_workspace(app, root, model):
    store = app.state.store
    publisher = ArtifactPublisher(store, root / "outputs")
    conversation_id = store.create_conversation("多产品修改验收")["conversation_id"]
    files = {}
    for product, entries in (
        ("甲产品", [("指标框架.md", "# 指标框架\n\n准确率阈值 80%。\n"),
                 ("执行说明.md", "# 执行说明\n\n人工复核。\n")]),
        ("乙产品", [("需求摘要.md", "# 需求摘要\n\n只属于乙产品。\n")]),
    ):
        target_names = [Path(name).stem for name, _ in entries]
        turn = store.begin_message_turn(conversation_id,
            body=f"为{product}创建" + "和".join(target_names), client_key=f"seed-{product}")["turn_id"]
        selected = store.select_evaluation_scope(
            conversation_id, turn, scope_kind="focused_create", product_name=product,
            target_names=target_names,
        )
        for name, content in entries:
            artifact = publisher.publish(conversation_id, turn,
                draft_path=f"/drafts/{turn}/{name}", filename=name, content=content)
            files[name] = artifact["artifact_id"]
        store.finish_turn(conversation_id, turn, "completed")
        if product == "甲产品":
            model.evaluation_id = selected["evaluation_id"]
    model.target_artifact_id = files["指标框架.md"]
    return {"conversation_id": conversation_id, "artifacts": files}


def seed_previews(app, root):
    store = app.state.store
    publisher = ArtifactPublisher(store, root / "outputs")
    conversation_id = store.create_conversation("预览验收")['conversation_id']
    plan = store.begin_message_turn(conversation_id, body="测试产物预览", client_key="seed-plan")['turn_id']
    store.select_evaluation_scope(conversation_id, plan, scope_kind="package_outline", product_name="预览产品")
    outline = store.submit_outline(conversation_id, plan,
        "## 评测目标\n测试预览。\n## 评测指标\n准确性。\n## 评测数据样本\n两条。\n"
        "## 评测执行方案\n本地。\n## 产物清单\n- 方案.md\n- 样本.csv\n")
    store.finish_turn(conversation_id, plan, "completed")
    confirmed = store.confirm_outline(conversation_id, outline['outline_id'], expected_revision=1, client_key="seed-confirm")
    turn_id = confirmed['turn_id']
    publisher.publish(conversation_id, turn_id, draft_path=f"/drafts/{turn_id}/plan.md",
        filename="方案.md", deliverable_id=confirmed['deliverable_ids'][0],
        content="# 评测方案\n\n**关键指标**\n\n| 指标 | 阈值 |\n|---|---:|\n| 准确率 | 90% |\n\n<img src=x onerror=alert(1)>\n" +
        "\n\n".join(f"## 验收章节 {index}\n\n覆盖评测目标、数据样本、评分标准与执行流程，逐项记录预期和实际结果。" for index in range(1, 25)))
    publisher.publish(conversation_id, turn_id, draft_path=f"/drafts/{turn_id}/sample.csv",
        filename="样本.csv", deliverable_id=confirmed['deliverable_ids'][1],
        content='id,问题,备注\n1,"电压,多少","第一行\n第二行"\n2,价格,<script>alert(1)</script>\n')
    artifacts = store.begin_finalization(conversation_id, turn_id)
    message_id = store.begin_assistant_message(conversation_id, turn_id)
    store.append_message_delta(conversation_id, turn_id, message_id, "文件已就绪，可点击预览。")
    store.complete_assistant_message(conversation_id, turn_id, message_id,
                                     artifact_ids=[item["artifact_id"] for item in artifacts])
    store.finish_turn(conversation_id, turn_id, "completed")
    return conversation_id


def seed_desktop(app, root):
    """Representative real workspace for desktop UI acceptance, never production data."""
    store = app.state.store
    publisher = ArtifactPublisher(store, root / "outputs")
    cid = store.create_conversation("为产品问答助手设计评测方案")["conversation_id"]
    names = ["产品问答助手评测方案.md", "评测指标定义表.csv", "评测样本设计表.csv",
             "人工评分与裁判校准细则.md", "评测执行方案与排期.md", "评测结果记录表模板.csv"]
    plan = store.begin_message_turn(cid, body="为产品问答助手设计评测方案", client_key="desktop-plan")["turn_id"]
    store.select_evaluation_scope(cid, plan, scope_kind="package_outline", product_name="产品问答助手")
    outline = store.submit_outline(cid, plan,
        "## 评测目标\n验证知识库问答的可靠性。\n## 评测指标\n检索有效性、回答准确性、边界行为。\n"
        "## 评测数据样本\n覆盖检索、生成、澄清与拒答。\n## 评测执行方案\n逐条记录证据，再进行人工复核。\n"
        "## 产物清单\n" + "\n".join(f"- {name}" for name in names))
    store.finish_turn(cid, plan, "completed")
    confirmation = store.confirm_outline(cid, outline["outline_id"], expected_revision=1, client_key="desktop-confirm")
    tid = confirmation["turn_id"]
    for name, did in zip(names, confirmation["deliverable_ids"]):
        content = ("指标,定义,记录方式\n准确性,回答与依据一致,人工核对\n" if name.endswith(".csv") else
                   f"# {Path(name).stem}\n\n用于产品问答助手的评测设计。\n\n## 范围\n覆盖检索、生成与边界行为，逐条保留判断依据。\n\n"
                   "## 执行说明\n先准备真实样本并校准评分标准，再开始评测。当前文件为设计交付，尚未执行评测。")
        publisher.publish(cid, tid, draft_path=f"/drafts/{tid}/{name}", filename=name, content=content, deliverable_id=did)
    artifacts = store.begin_finalization(cid, tid)
    mid = store.begin_assistant_message(cid, tid)
    store.append_message_delta(cid, tid, mid, "## 评测方案与配套文件已准备好\n\n已按确认的大纲完成 **6 份文件**，覆盖评测目标、指标、样本、评分规则和执行安排。\n\n当前交付的是评测设计文件，尚未执行评测；文档中的阈值需要结合真实样本验证。")
    store.complete_assistant_message(cid, tid, mid, artifact_ids=[a["artifact_id"] for a in artifacts])
    store.finish_turn(cid, tid, "completed")
    # Keep versions deliberately different; the UI must never invent a package-wide revision.
    artifact = artifacts[0]
    edit = store.begin_message_turn(cid, body="补充方案的执行说明", client_key="desktop-edit")["turn_id"]
    store.select_evaluation_scope(cid, edit, scope_kind="focused_edit", evaluation_id=artifact["evaluation_id"], target_artifact_ids=[artifact["artifact_id"]])
    publisher.publish(cid, edit, draft_path=f"/drafts/{edit}/revision.md", filename=artifact["filename"],
                      content="# 产品问答助手评测方案\n\n## 执行说明\n补充人工复核与评分校准，其他文件保持原版本。", artifact_id=artifact["artifact_id"])
    store.finish_turn(cid, edit, "completed")
    return cid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--mode", choices=("chat", "slow", "failure", "workflow", "outline-slow-final", "edit", "continue", "cancel-continue", "real", "real-small", "context", "context-failure", "context-slow", "rename"), required=True)
    args = parser.parse_args()
    root = Path(args.data_root).resolve()
    budget = ContextBudget()
    if args.mode == "rename":
        model, provider = RenameModel(), "v2acceptance"
    elif args.mode.startswith("context"):
        budget = TEST_BUDGET
        model, provider = ContextModel(summary_delay=10 if args.mode == "context-slow" else 3,
                                       summary_fails=args.mode == "context-failure"), "v2context"
    elif args.mode == "chat":
        model, provider = MarkdownModel(), "v2acceptance"
    elif args.mode == "slow":
        model, provider = LocalApiModel(slow=True), "v2acceptance"
    elif args.mode == "failure":
        model, provider = FailureModel(), "v2acceptance"
    elif args.mode == "workflow":
        model, provider = WorkflowModel(), "v2acceptance"
    elif args.mode == "outline-slow-final":
        model, provider = SlowAfterOutlineModel(), "v2acceptance"
    elif args.mode == "edit":
        model, provider = EditModel(), "v2acceptance"
    elif args.mode == "continue":
        model, provider = ContinueModel(), "v2acceptance"
    elif args.mode == "cancel-continue":
        model, provider = CancelContinueModel(), "v2acceptance"
    else:
        if args.mode == "real-small":
            budget = ContextBudget(window_tokens=12_000, summarize_at_tokens=2_200,
                                   keep_recent_tokens=500, max_output_tokens=1_000)
        model, provider = model_from_environment(budget), "openai"
        if model is None:
            raise RuntimeError("Real mode requires configured model environment")
    app = create_app(data_root=root, model=model, model_provider=provider, budget=budget)

    if args.mode == "rename":
        @app.post("/__test/seed-rename")
        async def make_rename_workspace():
            cid, eid = seed_package(app.state.store, ArtifactPublisher(app.state.store, root / "outputs"))
            model.evaluation_id = eid
            model.renames = [{"artifact_id": row["artifact_id"], "filename": name}
                             for row, name in zip(app.state.store.snapshot(cid)["artifacts"], CHINESE)]
            return {"conversation_id": cid}

    if args.mode == "real":
        @app.get("/__test/outline-attempts/{conversation_id}")
        async def outline_attempts(conversation_id: str):
            snapshot = app.state.store.snapshot(conversation_id)
            state = await app.state.executor.agent.aget_state({"configurable": {
                "thread_id": snapshot["conversation"]["agent_thread_id"],
            }})
            messages = state.values.get("messages", []) if state.values else []
            attempts = sum(
                call.get("name") == "submit_outline"
                for message in messages if isinstance(message, AIMessage)
                for call in message.tool_calls
            )
            rejected = sum(
                isinstance(message, ToolMessage) and message.name == "submit_outline" and
                isinstance(message.content, str) and
                json.loads(message.content).get("ok") is False
                for message in messages
            )
            return {"submit_attempts": attempts, "rejected_attempts": rejected}

    if args.mode == "real-small":
        @app.get("/__test/token-sample")
        async def token_sample():
            messages = [HumanMessage(content="请仅回复收到。" +
                                     "准确引用、拒答边界、样本覆盖与可复核执行。" * 30)]
            reply = await model.ainvoke(messages, stream=False)
            usage = reply.usage_metadata or {}
            return {"approx_input_tokens": count_tokens_approximately(messages),
                    "provider_input_tokens": usage.get("input_tokens")}

        @app.get("/__test/context/{conversation_id}")
        async def context_state(conversation_id: str):
            snapshot = app.state.store.snapshot(conversation_id)
            agent = app.state.executor.agent
            state = await agent.aget_state({"configurable": {
                "thread_id": snapshot["conversation"]["agent_thread_id"],
            }})
            values = state.values or {}
            summary_paths = sorted(path for path in values.get("files", {})
                                   if path.startswith("/conversation_history/"))
            return {"summary_count": len(summary_paths),
                    "message_count": len(values.get("messages", [])),
                    "approx_message_tokens": count_tokens_approximately(values.get("messages", [])),
                    "window_tokens": budget.window_tokens,
                    "summarize_at_tokens": budget.summarize_at_tokens,
                    "keep_recent_tokens": budget.keep_recent_tokens,
                    "max_output_tokens": budget.max_output_tokens}

    if args.mode in ("workflow", "outline-slow-final"):
        @app.post("/__test/phase/{phase}")
        async def set_phase(phase: str):
            if phase not in ("planning", "generation"):
                raise HTTPException(422, "Invalid test phase")
            model.phase = phase
            return {"phase": phase}

    if args.mode == "chat":
        @app.post("/__test/seed-desktop")
        async def make_desktop():
            return {"conversation_id": seed_desktop(app, root)}

        @app.post("/__test/seed-previews")
        async def make_previews():
            return {"conversation_id": seed_previews(app, root)}

    if args.mode == "edit":
        @app.post("/__test/seed-edit")
        async def make_edit_workspace():
            return seed_edit_workspace(app, root, model)

    if args.mode == "continue":
        @app.post("/__test/seed-cancelled")
        async def make_cancelled_package():
            return seed_cancelled_package(app, root, model)

        @app.post("/__test/seed-running")
        async def make_running_package():
            return seed_cancelled_package(app, root, model, running=True)

        @app.post("/__test/publish-remaining/{conversation_id}")
        async def publish_remaining(conversation_id: str):
            snapshot = app.state.store.snapshot(conversation_id)
            turn = snapshot["turns"][-1]
            pending = next(item for item in snapshot["deliverables"] if item["status"] == "pending")
            return ArtifactPublisher(app.state.store, root / "outputs").publish(
                conversation_id, turn["turn_id"],
                draft_path=f"/drafts/{turn['turn_id']}/samples.csv",
                filename=pending["name"], deliverable_id=pending["deliverable_id"],
                content="问题,预期\n示例问题,示例答案\n",
            )

        @app.post("/__test/finish-running/{conversation_id}")
        async def finish_running(conversation_id: str):
            turn_id = app.state.store.snapshot(conversation_id)["turns"][-1]["turn_id"]
            app.state.store.begin_finalization(conversation_id, turn_id)
            app.state.store.finish_turn(conversation_id, turn_id, "completed")
            return {"turn_id": turn_id}

    if args.mode == "cancel-continue":
        @app.post("/__test/seed-pending")
        async def make_pending_outline():
            return seed_pending_outline(app, model)

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
