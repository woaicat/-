"""LangGraph Orchestrator with Planner→Executor→Reflection→Summary pipeline."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, StateGraph

from .agent import PlanGenerationAgent, StepEntry
from .models import EvaluationRequest, Manifest, StepStatus
from .planner import PlanStep
from .tool_service import ToolService
from .context_manager import ContextManager


CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / "storage" / "checkpoints"
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = CHECKPOINT_DIR / "agent_state.sqlite"


class AgentGraphState(TypedDict, total=False):
    request: Dict[str, Any]
    context: Dict[str, str]
    plan_steps: List[Dict[str, Any]]
    plan_summary: Optional[str]
    queue: List[Dict[str, Any]]
    current_step: Optional[Dict[str, Any]]
    last_result: Optional[Dict[str, Any]]
    executed: List[Dict[str, Any]]
    reflections: List[Dict[str, Any]]
    summary: Optional[str]
    pending_tool_invocations: List[Dict[str, Any]]


def _step_entry_to_dict(entry: StepEntry) -> Dict[str, Any]:
    payload = {
        "uid": entry.uid,
        "step": entry.step.to_dict(),
        "attempts": entry.attempts,
        "max_attempts": entry.max_attempts,
        "source": entry.source,
    }
    return payload


def _plan_step_from_dict(step_dict: Dict[str, Any]) -> PlanStep:
    return PlanStep(
        step_id=step_dict.get("id") or step_dict.get("step_id"),
        title=step_dict.get("title", "步骤"),
        description=step_dict.get("description", ""),
        expected_outputs=[str(v) for v in step_dict.get("expected_outputs", [])],
        allow_retry=bool(step_dict.get("allow_retry", True)),
        success_criteria=step_dict.get("success_criteria", ""),
        considerations=[str(v) for v in step_dict.get("considerations", [])],
        pre_checks=step_dict.get("pre_checks"),
    )


def _dict_to_step_entry(data: Dict[str, Any]) -> StepEntry:
    plan_step = _plan_step_from_dict(data.get("step") or {})
    entry = StepEntry(
        uid=data.get("uid"),
        step=plan_step,
        source=data.get("source", "planner"),
        attempts=data.get("attempts", 0),
        max_attempts=data.get("max_attempts", 1),
    )
    return entry


class LangGraphAgentRunner:
    """Planner → Executor → Reflection → Summary orchestrated by LangGraph."""

    def __init__(
        self,
        request_id: str,
        emitter,
        tools: ToolService,
        context_manager: Optional[ContextManager] = None,
        callbacks: Optional[List] = None,
    ):
        self.request_id = request_id
        self.tools = tools
        self.callbacks = callbacks or []
        self.agent = PlanGenerationAgent(
            request_id=request_id,
            emitter=emitter,
            callbacks=self.callbacks,
        )
        self.enable_reflection_qa = self.agent.enable_reflection_qa
        self.context_manager = context_manager or ContextManager()
        self._graph = None
        self._graph_lock: Optional[asyncio.Lock] = None
        self._cached_plan_outline: Optional[PlanOutline] = None
        self._cached_plan_warnings: Optional[List[str]] = None
    async def _build_graph(self):
        graph = StateGraph(AgentGraphState)
        graph.add_node("planner", self._planner_node)
        graph.add_node("executor", self._executor_node)
        graph.add_node("tool_executor", self._tool_node)
        graph.add_node("reflection_qa", self._reflection_qa_node)
        graph.add_node("reflection", self._reflection_node)
        graph.add_node("summary", self._summary_node)
        graph.set_entry_point("planner")
        graph.add_edge("planner", "executor")
        graph.add_edge("executor", "tool_executor")
        graph.add_edge("tool_executor", "reflection_qa")
        graph.add_edge("reflection_qa", "reflection")
        graph.add_conditional_edges(
            "reflection",
            self._reflection_router,
            {
                "retry": "executor",
                "continue": "executor",
                "done": "summary",
            },
        )
        graph.add_edge("summary", END)
        conn = await aiosqlite.connect(str(CHECKPOINT_PATH))
        return graph.compile(checkpointer=AsyncSqliteSaver(conn))

    async def _ensure_graph(self):
        if self._graph is None:
            if self._graph_lock is None:
                self._graph_lock = asyncio.Lock()
            async with self._graph_lock:
                if self._graph is None:
                    self._graph = await self._build_graph()
        return self._graph

    def build_context(self, request: EvaluationRequest) -> Dict[str, str]:
        return self.agent.build_context(request)

    async def run(self, request: EvaluationRequest, context: Dict[str, str]) -> AgentGraphState:
        graph = await self._ensure_graph()
        plan_outline, warnings = await self.agent.planner.create_plan(request)
        self._cached_plan_outline = plan_outline
        self._cached_plan_warnings = warnings
        recursion_limit = self._estimate_recursion_limit(plan_outline)
        initial_state: AgentGraphState = {
            "request": request.model_dump(),
            "context": context,
            "plan_steps": [],
            "plan_summary": None,
            "queue": [],
            "executed": [],
            "reflections": [],
        }
        config = {
            "configurable": {"thread_id": self.request_id},
            "recursion_limit": recursion_limit,
        }
        if self.callbacks:
            config["callbacks"] = self.callbacks
        try:
            return await graph.ainvoke(initial_state, config=config)
        finally:
            self._cached_plan_outline = None
            self._cached_plan_warnings = None

    async def _planner_node(self, state: AgentGraphState) -> AgentGraphState:
        request = EvaluationRequest(**state["request"])
        context = state["context"]
        await self.agent._emit(
            "plan_generation",
            StepStatus.STARTED,
            "正在分析任务并制定定制化行动计划…",
            phase="planning",
        )
        plan_outline = self._cached_plan_outline
        warnings = list(self._cached_plan_warnings or [])
        if plan_outline is None:
            plan_outline, warnings = await self.agent.planner.create_plan(request)
        self._cached_plan_outline = None
        self._cached_plan_warnings = None
        if warnings:
            for note in warnings:
                await self.agent._emit(
                    "plan_warning",
                    StepStatus.SUCCEEDED,
                    note,
                    phase="planning",
                )
        queue = [_step_entry_to_dict(self.agent._make_step_entry(step, "planner")) for step in plan_outline.steps]
        await self.agent._emit(
            "plan_generation",
            StepStatus.SUCCEEDED,
            "智能规划器已生成定制步骤。",
            phase="planning",
            metadata={
                "summary": plan_outline.summary,
                "plan": [step.to_dict() for step in plan_outline.steps],
                "total_steps": len(plan_outline.steps),
            },
        )
        self.agent.record_plan_outline(plan_outline)
        return {
            "request": state["request"],
            "context": context,
            "plan_steps": [step.to_dict() for step in plan_outline.steps],
            "plan_summary": plan_outline.summary,
            "queue": queue,
            "executed": [],
            "reflections": [],
        }

    async def _executor_node(self, state: AgentGraphState) -> AgentGraphState:
        queue = list(state.get("queue") or [])
        if not queue:
            return state
        entry_dict = queue.pop(0)
        entry = _dict_to_step_entry(entry_dict)
        entry.attempts += 1
        metadata = {
            "attempt": entry.attempts,
            "max_attempts": entry.max_attempts,
            "step_order": len(state.get("executed", [])) + 1,
            "description": entry.step.description,
            "expected_outputs": entry.step.expected_outputs,
        }
        await self.agent._emit(
            entry.step.step_id,
            StepStatus.STARTED,
            f"{entry.step.title}（第{entry.attempts}次尝试）",
            step_id=entry.step.step_id,
            phase="execution",
            metadata=metadata,
        )
        request = EvaluationRequest(**state["request"])
        context = state.get("context", {})
        enriched_context = await self.context_manager.enrich_context(
            request=request,
            request_id=self.request_id,
            context=context,
            step=entry.step,
            history=state.get("executed", []),
        )
        context_invocations = self.context_manager.flush_invocations()
        state.update(
            queue=queue,
            current_step=_step_entry_to_dict(entry),
            last_result=None,
            context=enriched_context,
            pending_tool_invocations=context_invocations,
        )
        return state

    async def _tool_node(self, state: AgentGraphState) -> AgentGraphState:
        current = state.get("current_step")
        if not current:
            return state
        entry = _dict_to_step_entry(current)
        request = EvaluationRequest(**state["request"])
        context = state.get("context", {})
        try:
            result = await self.agent.execute_step(entry.step, request, self.tools, context)
            status = StepStatus.SUCCEEDED
            message = result.get("summary") or f"已生成 {', '.join(result.get('files') or [])}" or "步骤完成"
        except Exception as exc:  # pylint: disable=broad-except
            result = {"error": str(exc)}
            status = StepStatus.FAILED
            message = f"执行失败：{exc}"
        pending_invocations = list(state.get("pending_tool_invocations") or [])
        tool_invocations = pending_invocations + self.tools.flush_invocations()
        state["pending_tool_invocations"] = []
        if tool_invocations:
            result["tools_used"] = tool_invocations
        await self.agent._emit(
            entry.step.step_id,
            status,
            message,
            step_id=entry.step.step_id,
            phase="execution",
            metadata={k: v for k, v in {
                "files": result.get("files"),
                "error": result.get("error"),
                "insight": result.get("insight"),
                "tool_name": result.get("tool_name"),
                "tools_used": tool_invocations or None,
            }.items() if v},
        )
        await self.context_manager.record_step_result(
            request_id=self.request_id,
            step=entry.step,
            result=result,
        )
        state.update(last_result={"status": status, "data": result})
        return state

    async def _reflection_qa_node(self, state: AgentGraphState) -> AgentGraphState:
        if not self.enable_reflection_qa:
            return state
        current = state.get("current_step")
        last = state.get("last_result") or {}
        if not current or not last:
            return state
        entry = _dict_to_step_entry(current)
        request = EvaluationRequest(**state["request"])
        context = state.get("context", {})
        result = last.get("data", {}) or {}
        qa_result = await self.agent.run_step_reflection(entry.step, request, context, result)
        last["qa"] = qa_result
        state.update(last_result=last)
        if qa_result:
            metadata = {
                "decision": qa_result.get("decision"),
                "notes": qa_result.get("notes"),
                "missing_sections": qa_result.get("missing_sections"),
                "redo_adjustments": qa_result.get("redo_adjustments"),
                "additional_steps": qa_result.get("additional_steps"),
            }
            message = qa_result.get("notes") or "执行后质检完成"
            await self.agent._emit(  # pylint: disable=protected-access
                f"{entry.step.step_id}_qa",
                StepStatus.SUCCEEDED,
                message,
                step_id=entry.step.step_id,
                phase="reflection",
                metadata={k: v for k, v in metadata.items() if v},
            )
        return state

    async def _reflection_node(self, state: AgentGraphState) -> AgentGraphState:
        current = state.get("current_step")
        last = state.get("last_result") or {}
        queue = list(state.get("queue") or [])
        executed = list(state.get("executed") or [])
        reflections = list(state.get("reflections") or [])
        if not current:
            return state
        entry = _dict_to_step_entry(current)
        status = last.get("status", StepStatus.SUCCEEDED)
        result = last.get("data", {})
        qa_feedback = last.get("qa") if isinstance(last.get("qa"), dict) else {}
        decision = str(qa_feedback.get("decision") or "").lower().strip()
        redo = status == StepStatus.FAILED and entry.attempts < entry.max_attempts
        if decision == "redo" and entry.attempts < entry.max_attempts:
            redo = True
        step_title = entry.step.title
        decision_text = {
            "redo": "下一步计划：重试当前步骤并按质检建议补充。",
            "extend": "下一步计划：插入追加步骤并执行。",
            "continue": "下一步计划：继续后续步骤。",
        }
        insight_text = qa_feedback.get("notes") or result.get("insight")
        note_text = decision_text.get(decision) or ("下一步计划：重试当前步骤。" if redo else "下一步计划：继续执行。")
        reflection_payload = {
            "step_id": entry.step.step_id,
            "step_title": step_title,
            "decision": decision or ("retry" if redo else "continue"),
            "notes": note_text,
            "insight": insight_text,
            "error": result.get("error"),
            "qa": qa_feedback,
        }
        reflections.append(reflection_payload)
        await self.agent._emit(
            f"{entry.step.step_id}_reflection",
            StepStatus.SUCCEEDED,
            reflection_payload.get("notes") or "下一步计划已更新",
            step_id=entry.step.step_id,
            phase="reflection",
            metadata={"decision": reflection_payload["decision"], "redo": redo},
        )
        additional_steps = qa_feedback.get("additional_steps") if isinstance(qa_feedback, dict) else None
        if additional_steps:
            self._enqueue_additional_steps(queue, additional_steps)
        if redo:
            queue.insert(0, current)
        else:
            executed.append(
                {
                    "step_id": entry.step.step_id,
                    "title": entry.step.title,
                    "result": result,
                    "attempts": entry.attempts,
                    "qa": qa_feedback,
                }
            )
        state.update(
            queue=queue,
            executed=executed,
            reflections=reflections,
            current_step=None,
            last_result=None,
            router_action="retry"
            if redo
            else ("continue" if queue else "done"),
        )
        return state

    def _enqueue_additional_steps(self, queue: List[Dict[str, Any]], steps_data: List[Dict[str, Any]]) -> bool:
        inserted = False
        if not steps_data:
            return inserted
        for step_dict in reversed(steps_data):
            try:
                plan_step = _plan_step_from_dict(step_dict)
            except Exception:  # pragma: no cover
                continue
            if not plan_step.step_id:
                continue
            entry = self.agent._make_step_entry(plan_step, "reflection")
            queue.insert(0, _step_entry_to_dict(entry))
            inserted = True
        return inserted

    async def _summary_node(self, state: AgentGraphState) -> AgentGraphState:
        request = EvaluationRequest(**state["request"])
        context = state.get("context", {})
        files = self.tools.list_generated_files()
        manifest_stub = Manifest(
            request_id=self.request_id,
            product_name=request.product_name,
            created_at=datetime.utcnow(),
            files=files,
        )
        summary_text = ""
        try:
            summary_text = await self.agent.generate_final_summary(
                request,
                context,
                manifest_stub,
                state.get("reflections", []),
            )
        except Exception:  # pragma: no cover
            summary_text = ""
        state.update(summary=summary_text)
        return state

    def _reflection_router(self, state: AgentGraphState) -> str:
        action = state.get("router_action")
        if action in {"retry", "continue", "done"}:
            return action  # type: ignore[return-value]
        if not state.get("queue"):
            return "done"
        return "continue"

    def _estimate_recursion_limit(self, plan_outline: PlanOutline) -> int:
        step_count = max(len(plan_outline.steps), 1)
        per_step_nodes = 4  # executor, tool, reflection_qa, reflection
        buffer_factor = 3  # allow retries与追加步骤
        base_nodes = 5  # planner、summary 等基础节点
        estimate = base_nodes + per_step_nodes * step_count * buffer_factor
        return max(40, estimate)
