"""Own one active asyncio task per turn and persist public text deltas."""

import asyncio
import json
import logging
from pathlib import Path

from deepagents.backends.utils import create_file_data
from langchain_core.messages import AIMessage, HumanMessage

from evaluation_agent.observability import trace_turn
from evaluation_agent.runtime import ContextBudgetExceeded
from evaluation_agent.store import ConflictError, WorkspaceStore


logger = logging.getLogger(__name__)


class OutputValidationError(RuntimeError):
    def __init__(self, issues: list[str]):
        super().__init__("；".join(issues))
        self.issues = issues


def public_text_piece(event: dict) -> str | None:
    """Project only user-visible model text from the pinned v3 protocol.

    Deep Agents summarization invokes the model inside the same graph node.
    Its events carry internal-call metadata, unlike public model replies.
    """
    if event.get("method") != "messages":
        return None
    params = event["params"]
    if params.get("namespace") != []:
        return None
    payload, metadata = params["data"]
    if metadata.get("langgraph_node") != "model" or metadata.get("lc_internal_call"):
        return None
    if metadata.get("lc_source") == "summarization":
        return None
    if isinstance(payload, AIMessage):
        return payload.content if isinstance(payload.content, str) else None
    if isinstance(payload, dict) and payload.get("event") == "content-block-delta":
        delta = payload.get("delta", {})
        if delta.get("type") == "text-delta":
            return delta.get("text")
    return None


class TurnExecutor:
    def __init__(self, store: WorkspaceStore, agent, *, max_turn_seconds: int = 1200):
        self.store = store
        self.agent = agent
        self.max_turn_seconds = max_turn_seconds
        self.tasks: dict[str, asyncio.Task] = {}

    def start(self, conversation_id: str, turn_id: str, prompt: str) -> None:
        if turn_id in self.tasks:
            return
        turn = self.store.get_turn(conversation_id, turn_id)
        if turn["status"] != "running":
            raise ConflictError("Turn is not running", turn_id=turn_id)
        task = asyncio.create_task(self._run(conversation_id, turn_id, prompt), name=f"turn:{turn_id}")
        self.tasks[turn_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(turn_id, None))

    def cancel(self, turn_id: str) -> None:
        task = self.tasks.get(turn_id)
        if task is not None:
            task.cancel()

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, conversation_id: str, turn_id: str, prompt: str) -> None:
        thread_id = self.store.snapshot(conversation_id)["conversation"]["agent_thread_id"]
        async with trace_turn(
            conversation_id=conversation_id, turn_id=turn_id, thread_id=thread_id, prompt=prompt,
        ) as run:
            try:
                await self._execute(conversation_id, turn_id, prompt)
            finally:
                if run is not None:
                    snapshot = self.store.snapshot(conversation_id)
                    status = self.store.get_turn(conversation_id, turn_id)["status"]
                    replies = [item["body"] for item in snapshot["messages"]
                               if item["turn_id"] == turn_id and item["role"] == "assistant"]
                    run.end(
                        outputs={"status": status, "reply": "\n\n".join(replies)},
                        error=f"轮次状态：{status}" if status in ("failed", "interrupted", "cancelled") else None,
                    )

    async def _execute(self, conversation_id: str, turn_id: str, prompt: str) -> None:
        message_id = None
        try:
            config = {"configurable": {
                "thread_id": self.store.snapshot(conversation_id)["conversation"]["agent_thread_id"],
                "conversation_id": conversation_id,
                "turn_id": turn_id,
            }, "recursion_limit": 80}
            config["metadata"] = {
                "conversation_id": conversation_id, "turn_id": turn_id,
                "thread_id": config["configurable"]["thread_id"],
                "agent_framework": "deepagents",
            }
            def agent_input(text: str) -> dict:
                snapshot = self.store.snapshot(conversation_id)
                turn = self.store.get_turn(conversation_id, turn_id)
                scope = json.loads(turn["scope_json"])
                product_index = [
                    f"{item['evaluation_id']}：{item['name']}（{item['package_status']}）"
                    for item in snapshot["evaluations"]
                ]
                material_lines = []
                files = {}
                for attachment in snapshot["attachments"]:
                    source = Path(attachment["extracted_path"])
                    if not source.is_file():
                        continue
                    folder = attachment["evaluation_id"] or "inbox"
                    virtual_path = f"/materials/{folder}/{attachment['attachment_id']}.md"
                    files[virtual_path] = create_file_data(source.read_text(encoding="utf-8"))
                    marker = "，本轮上传" if attachment["message_id"] == turn["trigger_message_id"] else ""
                    material_lines.append(f"{attachment['attachment_id']}：{virtual_path}（{attachment['filename']}{marker}）")
                for artifact in snapshot["artifacts"]:
                    source = Path(artifact["source_path"])
                    if source.is_file():
                        virtual_path = f"/artifacts/{artifact['evaluation_id']}/{artifact['artifact_id']}.md"
                        files[virtual_path] = create_file_data(source.read_text(encoding="utf-8"))
                allowed = set(scope.get("deliverable_ids", []))
                deliverable_lines = [
                    f"{item['deliverable_id']}：{item['name']}（{item['status']}）"
                    for item in snapshot["deliverables"] if item["deliverable_id"] in allowed
                ]
                artifact_lines = []
                for item in snapshot["artifacts"]:
                    line = f"{item['artifact_id']}：{item['filename']}（产品 {item['evaluation_id']}，版本 {item['content_revision']}）"
                    aliases = [alias["filename"] for alias in snapshot["artifact_aliases"]
                               if alias["artifact_id"] == item["artifact_id"]]
                    if aliases:
                        line += "；历史文件名：" + "、".join(aliases)
                    artifact_lines.append(line)
                context = "\n".join([
                    "【服务端工作上下文】",
                    f"本轮草稿目录：/drafts/{turn_id}/",
                    "产品索引：" + ("；".join(product_index) or "暂无"),
                    "资料索引：" + ("；".join(material_lines) or "暂无"),
                    "现有产物索引：" + ("；".join(artifact_lines) or "暂无"),
                    "本轮交付清单：" + ("；".join(deliverable_lines) or "暂无"),
                    "【本轮请求】", text,
                ])
                return {"messages": [HumanMessage(content=context)], "files": files}

            def outline_ready() -> bool:
                return any(
                    event["turn_id"] == turn_id and event["type"] == "outline.ready"
                    for event in self.store.list_events(conversation_id)
                )

            async def consume(text: str, *, final: bool = False) -> None:
                nonlocal message_id
                call_config = {**config, "run_name": "评测方案智能体 · 最终回复" if final else "评测方案智能体 · 执行"}
                async with await self.agent.astream_events(
                    agent_input(text), call_config, version="v3"
                ) as run_stream:
                    async for event in run_stream:
                        # A committed graph value follows the successful tool call.
                        # The outline card is the response; no extra model reply is needed.
                        if (not final and event.get("method") == "values" and
                                self.store.get_turn(conversation_id, turn_id)["scope_kind"] == "package_outline" and
                                outline_ready()):
                            break
                        piece = public_text_piece(event)
                        if not piece:
                            continue
                        scope = self.store.get_turn(conversation_id, turn_id)["scope_kind"]
                        if not final and scope in (
                            "package_generate", "package_continue", "focused_create", "focused_edit", "focused_rename"
                        ):
                            continue
                        if scope == "package_outline" and not outline_ready():
                            continue
                        if message_id is None:
                            message_id = self.store.begin_assistant_message(conversation_id, turn_id)
                        self.store.append_message_delta(conversation_id, turn_id, message_id, piece)

            artifact_ids = []
            async with asyncio.timeout(self.max_turn_seconds):
                await consume(prompt)
                scope = self.store.get_turn(conversation_id, turn_id)["scope_kind"]
                if scope == "package_outline":
                    if not outline_ready():
                        await consume(
                            "服务端尚未收到可确认的大纲卡片。请在本轮通过 submit_outline 提交完整大纲；"
                            "这是本轮唯一一次补交机会。"
                        )
                    if not outline_ready():
                        raise OutputValidationError(["可确认的大纲尚未提交"])
                if scope in ("package_generate", "package_continue", "focused_create", "focused_edit", "focused_rename"):
                    issues = self.store.validation_issues(conversation_id, turn_id)
                    if issues:
                        await consume(
                            "服务端验收发现以下具体问题，请只在本轮授权范围内修订，然后再结束。"
                            "这是本轮唯一一次修订机会；不要声称已经通过验收。问题："
                            + "；".join(issues)
                        )
                        issues = self.store.validation_issues(conversation_id, turn_id)
                    if issues:
                        raise OutputValidationError(issues)
                    artifacts = self.store.begin_finalization(conversation_id, turn_id)
                    artifact_ids = [event["artifact_id"] for event in artifacts]
                    names = "、".join(event["filename"] for event in artifacts)
                    await consume(
                        "本轮文件已完成服务端校验，已就绪产物：" + names +
                        "。请简短告知用户完成情况；不要再调用写入工具。", final=True,
                    )
                    if message_id is None:
                        message_id = self.store.begin_assistant_message(conversation_id, turn_id)
                        self.store.append_message_delta(conversation_id, turn_id, message_id, "已完成，产物已可预览。")
            if message_id is None:
                has_outline = scope == "package_outline" and outline_ready()
                if not has_outline:
                    raise RuntimeError("Agent did not provide a user-visible reply or outline")
            if message_id is not None:
                self.store.complete_assistant_message(
                    conversation_id, turn_id, message_id, artifact_ids=artifact_ids,
                )
            self.store.finish_turn(conversation_id, turn_id, "completed")
        except asyncio.CancelledError:
            status = self.store.get_turn(conversation_id, turn_id)["status"]
            self.store.finish_turn(
                conversation_id, turn_id,
                "cancelled" if status == "cancel_requested" else "interrupted",
            )
            raise
        except Exception as exc:
            logger.exception("Turn failed: %s", turn_id)
            try:
                status = self.store.get_turn(conversation_id, turn_id)["status"]
                if status == "running":
                    compression = self.store.snapshot(conversation_id)["context"]["compression"] or {}
                    if isinstance(exc, OutputValidationError):
                        description = "本轮验收未通过，任务尚未完成：" + "；".join(exc.issues[:8])
                    elif compression.get("status") == "failed" and compression.get("turn_id") == turn_id:
                        description = "本轮整理对话上下文失败，任务尚未完成。历史消息和已生成文件仍保留，请重试或缩小本轮任务范围。"
                    elif isinstance(exc, ContextBudgetExceeded):
                        description = "本轮上下文超过工作窗口，已停止模型调用。请缩小本轮材料或任务范围后重试。"
                    elif isinstance(exc, RuntimeError) and str(exc) == "Agent did not provide a user-visible reply or outline":
                        description = "本轮没有得到可显示的回复，请重试或补充要求。"
                    else:
                        description = "本轮执行遇到异常，未能完成。请重试或继续处理。"
                    turn = self.store.get_turn(conversation_id, turn_id)
                    if turn["scope_kind"] == "focused_rename":
                        completed_rename = any(event["turn_id"] == turn_id and event["type"] == "artifact.renamed"
                                               for event in self.store.list_events(conversation_id))
                        description += " 文件名称已整批更新，正文和内容版本未改动。" if completed_rename else " 本轮未提交改名，原文件名和正文保持原样。"
                    ready = [item["filename"] for item in self.store.snapshot(conversation_id)["artifacts"]
                             if item["evaluation_id"] == self.store.get_turn(conversation_id, turn_id)["evaluation_id"]]
                    if ready:
                        description += " 已生成的文件仍保留在产物抽屉：" + "、".join(ready) + "。"
                    separator = "\n\n" if message_id is not None else ""
                    if message_id is None:
                        message_id = self.store.begin_assistant_message(conversation_id, turn_id)
                    self.store.append_message_delta(conversation_id, turn_id, message_id, separator + description)
                    self.store.complete_assistant_message(conversation_id, turn_id, message_id)
                self.store.finish_turn(
                    conversation_id, turn_id,
                    "cancelled" if status == "cancel_requested" else "failed",
                )
            except Exception:
                logger.exception("Could not persist failed turn: %s", turn_id)
