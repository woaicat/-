"""Minimal LangGraph demo wiring Planner→Executor→Summary nodes.

运行方式：

```bash
python -m app.langgraph_demo --thread-id demo-001
```

脚本会构建一个 Fake 计划、执行并生成总结，过程中会把节点事件打印出来，并将状态快照写入
`storage/checkpoints/langgraph_demo.sqlite`，以验证 StateGraph / Checkpointer 的基本行为。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from .models import EvaluationRequest, StepStatus, StreamEvent


STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage" / "checkpoints"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = STORAGE_DIR / "langgraph_demo.sqlite"


class DemoState(TypedDict, total=False):
    """Shared state structure for the demo LangGraph."""

    request: Dict[str, Any]
    request_id: str
    plan: Dict[str, Any]
    steps: List[Dict[str, Any]]
    completed_steps: List[Dict[str, Any]]
    summary: str
    events: List[str]
    stream_events: List[StreamEvent]


def _ensure_events(state: DemoState) -> List[str]:
    events = list(state.get("events") or [])
    return events


def _ensure_stream_events(state: DemoState) -> List[StreamEvent]:
    return list(state.get("stream_events") or [])


def _format_sse(event: StreamEvent) -> str:
    return f"data: {event.json()}\n\n"


def planner_node(state: DemoState) -> DemoState:
    request = state["request"]
    request_id = state["request_id"]
    product = request.get("product_name", "未命名产品")
    features = request.get("core_features") or []
    steps: List[Dict[str, Any]] = []
    base_steps = [
        {
            "id": "product_analysis",
            "title": "产品解析",
            "description": "梳理产品定位、目标用户与关键价值。",
            "expected_outputs": ["产品解析.md"],
        },
        {
            "id": "plan_design",
            "title": "方案设计",
            "description": "设计评测目标与关键指标，形成整体方案。",
            "expected_outputs": ["评测方案.md"],
        },
        {
            "id": "data_delivery",
            "title": "数据与样本",
            "description": "结合核心功能生成数据规范与样本表。",
            "expected_outputs": ["数据集规范.md", "样本表.csv"],
        },
        {
            "id": "execution_manual",
            "title": "测试执行",
            "description": "整理测试执行手册及提示词/脚本。",
            "expected_outputs": ["测试执行指导手册.md", "LLM提示词.docx", "auto_runner.py"],
        },
    ]
    steps.extend(base_steps)
    plan = {
        "product": product,
        "step_count": len(steps),
        "summary": f"为 {product} 生成 {len(steps)} 个步骤的执行计划。",
    }

    events = _ensure_events(state)
    events.append(f"[planner] 已生成 {len(steps)} 个步骤。")

    stream_events = _ensure_stream_events(state)
    stream_events.append(
        StreamEvent(
            request_id=request_id,
            step="plan_generation",
            status=StepStatus.STARTED,
            message="正在生成行动计划…",
            phase="planning",
        )
    )
    stream_events.append(
        StreamEvent(
            request_id=request_id,
            step="plan_generation",
            status=StepStatus.SUCCEEDED,
            message="行动计划生成完毕。",
            metadata={
                "plan": steps,
                "summary": plan["summary"],
                "total_steps": len(steps),
            },
            phase="planning",
        )
    )

    return {
        "plan": plan,
        "steps": steps,
        "events": events,
        "stream_events": stream_events,
    }


def executor_node(state: DemoState) -> DemoState:
    steps = state.get("steps") or []
    completed = list(state.get("completed_steps") or [])
    events = _ensure_events(state)
    stream_events = _ensure_stream_events(state)
    request_id = state["request_id"]
    for idx, step in enumerate(steps, start=1):
        artefacts = [f for f in step.get("expected_outputs", [])]
        completed.append(
            {
                "id": step["id"],
                "title": step["title"],
                "outputs": artefacts,
            }
        )
        events.append(f"[executor] {step['title']} 完成，产出 {', '.join(artefacts) or '无'}。")

        stream_events.append(
            StreamEvent(
                request_id=request_id,
                step=step["id"],
                step_id=step["id"],
                status=StepStatus.STARTED,
                message=f"{step['title']}（第1次尝试）",
                phase="execution",
                metadata={
                    "step_order": idx,
                    "description": step["description"],
                    "expected_outputs": artefacts,
                },
            )
        )
        stream_events.append(
            StreamEvent(
                request_id=request_id,
                step=step["id"],
                step_id=step["id"],
                status=StepStatus.SUCCEEDED,
                message=f"已生成 {', '.join(artefacts) or '阶段记录'}",
                phase="execution",
                metadata={"files": artefacts},
            )
        )

    return {
        "completed_steps": completed,
        "events": events,
        "stream_events": stream_events,
    }


def summary_node(state: DemoState) -> DemoState:
    completed = state.get("completed_steps") or []
    product = state["plan"].get("product") if state.get("plan") else "产品"
    lines = [f"产品：{product}"]
    for item in completed:
        outputs = ", ".join(item.get("outputs") or []) or "无"
        lines.append(f"- {item['title']} → {outputs}")
    summary = "\n".join(lines)
    events = _ensure_events(state)
    events.append("[summary] 总结已生成。")

    stream_events = _ensure_stream_events(state)
    request_id = state["request_id"]
    stream_events.append(
        StreamEvent(
            request_id=request_id,
            step="final_summary",
            step_id="final_summary",
            status=StepStatus.SUCCEEDED,
            message=summary,
            metadata={"summary": summary},
            phase="finalize",
        )
    )
    stream_events.append(
        StreamEvent(
            request_id=request_id,
            step="finalize",
            status=StepStatus.SUCCEEDED,
            message="所有产物已写入输出目录（Demo）。",
            metadata={
                "files": [o for step in completed for o in step.get("outputs") or []]
            },
            phase="finalize",
        )
    )

    return {
        "summary": summary,
        "events": events,
        "stream_events": stream_events,
    }


def build_graph() -> StateGraph:
    graph = StateGraph(DemoState)
    graph.add_node("planner", planner_node)
    graph.add_node("executor", executor_node)
    graph.add_node("summary", summary_node)
    graph.set_entry_point("planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", "summary")
    graph.add_edge("summary", END)
    return graph


def _load_request(path: str | None) -> EvaluationRequest:
    if path is None:
        sample = {
            "product_name": "评测方案设计智能体",
            "application_scenario": "助力评测团队生成测试方案",
            "target_users": "评测工程师",
            "business_goals": "缩短方案产出时间并提高一致性",
            "core_features": [
                "需求解析",
                "评测方案规划",
                "样本生成",
            ],
            "workflow": [],
            "constraints": "仅在本地运行，禁止外泄数据",
        }
        return EvaluationRequest(**sample)
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
    return EvaluationRequest(**data)


def _build_initial_state(request: EvaluationRequest, request_id: str) -> DemoState:
    stream_events = [
        StreamEvent(
            request_id=request_id,
            step="accepted",
            status=StepStatus.STARTED,
            message="任务已受理，进入 LangGraph demo。",
        )
    ]
    return {
        "request": request.model_dump(),
        "request_id": request_id,
        "events": [
            f"[entry] 任务 {request.product_name} 已进入 LangGraph demo。",
        ],
        "stream_events": stream_events,
    }


async def run_demo(args: argparse.Namespace) -> None:
    request = _load_request(args.payload)
    thread_id = args.thread_id or f"demo-{uuid.uuid4().hex[:8]}"
    request_id = thread_id
    initial_state = _build_initial_state(request, request_id)
    graph = build_graph()
    conn = sqlite3.connect(CHECKPOINT_PATH, check_same_thread=False)
    graph = graph.compile(checkpointer=SqliteSaver(conn))

    config = {"configurable": {"thread_id": thread_id}}

    print(f"[demo] 使用线程 ID: {thread_id}")
    print("[demo] 开始按节点顺序执行…")

    final_state: DemoState | None = None
    for event in graph.stream(initial_state, config=config, stream_mode="values"):
        final_state = event
        events = event.get("events") if isinstance(event, dict) else None
        if events:
            print(f"  · 最新事件: {events[-1]}")

    if not final_state:
        print("[demo] 未获取到最终状态！")
        return

    print("[demo] 执行结束，最终摘要：")
    print(final_state.get("summary", "(暂无总结)"))

    stored = graph.get_state(config)
    if stored:
        checkpoint = getattr(stored, "checkpoint", None)
        checkpoint_id = None
        if checkpoint is not None:
            checkpoint_id = getattr(checkpoint, "checkpoint_id", None)
        print("[demo] Checkpointer 已存储最新状态，条目 ID:", checkpoint_id or "(未知)")
        print("[demo] 可在 storage/checkpoints/langgraph_demo.sqlite 中查看原始记录。")

    stream_events = final_state.get("stream_events") if final_state else []
    if stream_events:
        print("[demo] 以下为对齐 SSE 结构的事件示例：")
        for evt in stream_events:
            print("  ·", _format_sse(evt).strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the LangGraph planner→executor demo.")
    parser.add_argument("--payload", type=str, default=None, help="可选 JSON 请求文件路径")
    parser.add_argument("--thread-id", type=str, default=None, help="用于复现/恢复的线程 ID")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_demo(args))


if __name__ == "__main__":
    main()
