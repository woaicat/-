"""Optional LangSmith tracing using the SDK's native environment settings."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import langsmith as ls
from langsmith.run_trees import get_cached_client


logger = logging.getLogger(__name__)


def tracing_enabled() -> bool:
    return os.environ.get("LANGSMITH_TRACING", "false").strip().lower() == "true"


def configure_observability() -> None:
    """Validate opt-in configuration without making any network requests."""
    if not tracing_enabled():
        return
    if not os.environ.get("LANGSMITH_API_KEY", "").strip():
        raise RuntimeError("请在本项目 .env 中填写 LANGSMITH_API_KEY，或设置 LANGSMITH_TRACING=false")
    agent_id = os.environ.get("LANGSMITH_AGENT_ID", "").strip()
    environment = os.environ.get("LANGSMITH_AGENT_ENVIRONMENT", "").strip()
    if bool(agent_id) != bool(environment):
        raise RuntimeError("LANGSMITH_AGENT_ID 和 LANGSMITH_AGENT_ENVIRONMENT 必须一起填写")
    if agent_id:
        ls.agent_address(agent_id, environment)
        if any(os.environ.get(name, "").strip() for name in (
            "LANGSMITH_PROJECT", "LANGCHAIN_PROJECT", "LANGCHAIN_SESSION",
        )):
            raise RuntimeError("使用 LANGSMITH_AGENT_ID 时，请移除 LANGSMITH_PROJECT 等旧版目标配置")
    else:
        os.environ.setdefault("LANGSMITH_PROJECT", "evaluation-agent-v2")


@asynccontextmanager
async def trace_turn(*, conversation_id: str, turn_id: str, thread_id: str, prompt: str):
    """Group all graph calls, repairs, and tool calls in a turn under one root."""
    if not tracing_enabled():
        yield None
        return
    async with ls.trace(
        "评测方案智能体 · 对话轮次",
        inputs={"message": prompt},
        tags=["evaluation-agent-v2", "deepagents"],
        metadata={
            "conversation_id": conversation_id, "turn_id": turn_id,
            "thread_id": thread_id, "agent_framework": "deepagents",
        },
    ) as run:
        yield run


async def flush_traces() -> None:
    if not tracing_enabled():
        return
    try:
        await asyncio.to_thread(get_cached_client().flush, timeout=5)
    except Exception as exc:
        # Never log configuration or credentials when the telemetry endpoint fails.
        logger.warning("LangSmith 收尾上传未完成（%s）", type(exc).__name__)
