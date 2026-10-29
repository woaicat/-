"""Framework contract checks using a local model; no provider API is called."""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.memory import MemorySaver

from evaluation_agent.runtime import (
    BudgetedSummaryModel, ContextBudget, ContextBudgetExceeded, ContextBudgetMiddleware,
    MODEL_TOOLS, create_runtime, estimate_request_tokens,
)


@tool
def select_evaluation(name: str) -> str:
    """Select an evaluation scope for a product."""
    return name


@tool
def submit_outline(text: str) -> str:
    """Submit a proposed outline for user confirmation."""
    return text


@tool
def publish_artifact(path: str) -> str:
    """Publish one authorized draft as an artifact."""
    return path


@tool
def rename_artifacts(renames: list[dict]) -> str:
    """Rename an authorized batch without changing content."""
    return str(renames)


PRODUCT_TOOLS = [select_evaluation, submit_outline, publish_artifact, rename_artifacts]


class LocalScriptedModel(BaseChatModel):
    bound_names: list[str] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "v2-scripted"

    def _get_ls_params(self, **kwargs):
        return {"ls_provider": "v2scripted", "ls_model_type": "chat"}

    def bind_tools(self, tools, **kwargs):
        self.bound_names = [t.name if hasattr(t, "name") else t["function"]["name"] for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        last = messages[-1]
        if isinstance(last, ToolMessage):
            reply = AIMessage(content=str(last.content))
        elif isinstance(last, HumanMessage) and str(last.content).startswith("write:"):
            reply = AIMessage(
                content="",
                tool_calls=[{
                    "name": "write_file",
                    "args": {"file_path": "/drafts/turn-a/note.md", "content": str(last.content)[6:]},
                    "id": f"call-{self.calls}",
                }],
            )
        elif isinstance(last, HumanMessage) and str(last.content) == "read":
            reply = AIMessage(
                content="",
                tool_calls=[{
                    "name": "read_file",
                    "args": {"file_path": "/drafts/turn-a/note.md"},
                    "id": f"call-{self.calls}",
                }],
            )
        elif isinstance(last, HumanMessage) and str(last.content) == "overwrite-old-turn":
            reply = AIMessage(
                content="",
                tool_calls=[{
                    "name": "write_file",
                    "args": {"file_path": "/drafts/turn-a/note.md", "content": "overwritten"},
                    "id": f"call-{self.calls}",
                }],
            )
        elif isinstance(last, HumanMessage) and str(last.content) == "traverse":
            reply = AIMessage(
                content="",
                tool_calls=[{
                    "name": "write_file",
                    "args": {"file_path": "/drafts/turn-b/../turn-a/note.md", "content": "overwritten"},
                    "id": f"call-{self.calls}",
                }],
            )
        elif isinstance(last, HumanMessage) and str(last.content) == "forbidden":
            reply = AIMessage(
                content="",
                tool_calls=[{
                    "name": "write_file",
                    "args": {"file_path": "/materials/leak.md", "content": "no"},
                    "id": f"call-{self.calls}",
                }],
            )
        else:
            reply = AIMessage(content="你好，已收到。")
        return ChatResult(generations=[ChatGeneration(message=reply)])


class LocalStreamingModel(LocalScriptedModel):
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for piece in ("你", "好", "。"):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))


class RuntimeContractTests(unittest.TestCase):
    def test_ten_tools_and_isolated_persistent_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "checkpoints.sqlite"
            model = LocalScriptedModel()
            conn = sqlite3.connect(db_path, check_same_thread=False)
            try:
                agent = create_runtime(
                    model=model,
                    model_provider="v2scripted",
                    product_tools=PRODUCT_TOOLS,
                    checkpointer=SqliteSaver(conn),
                )
                a = {"configurable": {"thread_id": "conversation-a", "turn_id": "turn-a"}}
                next_a = {"configurable": {"thread_id": "conversation-a", "turn_id": "turn-b"}}
                b = {"configurable": {"thread_id": "conversation-b", "turn_id": "turn-c"}}
                agent.invoke({"messages": [HumanMessage(content="write:private-a")]}, a)
                self.assertEqual(set(model.bound_names), MODEL_TOOLS)
                self.assertEqual(len(model.bound_names), 10)
                self.assertIn("private-a", agent.invoke({"messages": [HumanMessage(content="read")]}, next_a)["messages"][-1].content)
                other = agent.invoke({"messages": [HumanMessage(content="read")]}, b)["messages"][-1].content
                self.assertNotIn("private-a", other)
                denied_old = agent.invoke({"messages": [HumanMessage(content="overwrite-old-turn")]}, next_a)["messages"][-1].content
                self.assertIn("denied", denied_old.lower())
                denied_traversal = agent.invoke({"messages": [HumanMessage(content="traverse")]}, next_a)["messages"][-1].content
                self.assertIn("path traversal not allowed", denied_traversal.lower())
                self.assertIn("private-a", agent.invoke({"messages": [HumanMessage(content="read")]}, next_a)["messages"][-1].content)
                denied = agent.invoke({"messages": [HumanMessage(content="forbidden")]}, a)["messages"][-1].content
                self.assertIn("denied", denied.lower())
            finally:
                conn.close()
            reopened = sqlite3.connect(db_path, check_same_thread=False)
            try:
                restored = create_runtime(
                    model=LocalScriptedModel(),
                    model_provider="v2scripted",
                    product_tools=PRODUCT_TOOLS,
                    checkpointer=SqliteSaver(reopened),
                )
                result = restored.invoke({"messages": [HumanMessage(content="read")]}, next_a)
                self.assertIn("private-a", result["messages"][-1].content)
            finally:
                reopened.close()

    def test_budget_configuration_rejects_missing_reserve(self):
        with self.assertRaises(ValueError):
            ContextBudget(window_tokens=100, summarize_at_tokens=90, max_output_tokens=20)

    def test_full_request_preflight_counts_system_tools_messages_and_output_reserve(self):
        from types import SimpleNamespace
        from langchain_core.messages import SystemMessage

        request = SimpleNamespace(
            system_message=SystemMessage(content="system" * 20),
            messages=[HumanMessage(content="request" * 20)],
            tools=[select_evaluation],
        )
        measured = estimate_request_tokens(request)
        without_system = estimate_request_tokens(SimpleNamespace(
            system_message=None, messages=request.messages, tools=request.tools,
        ))
        without_tools = estimate_request_tokens(SimpleNamespace(
            system_message=request.system_message, messages=request.messages, tools=[],
        ))
        self.assertGreater(measured, without_system)
        self.assertGreater(measured, without_tools)
        budget = ContextBudget(window_tokens=measured + 9, summarize_at_tokens=10,
                               keep_recent_tokens=1, max_output_tokens=10)
        gate = ContextBudgetMiddleware(budget)
        with self.assertRaises(ContextBudgetExceeded) as failure:
            gate._check(request)
        self.assertEqual(failure.exception.estimated_tokens, measured)
        self.assertEqual(failure.exception.limit_tokens, measured - 1)

    def test_irreducible_user_request_stops_before_model_call(self):
        model = LocalScriptedModel()
        agent = create_runtime(
            model=model, model_provider="v2scripted", product_tools=PRODUCT_TOOLS,
            checkpointer=MemorySaver(),
            budget=ContextBudget(window_tokens=3000, summarize_at_tokens=1800,
                                 keep_recent_tokens=400, max_output_tokens=200),
        )
        with self.assertRaises(ContextBudgetExceeded):
            agent.invoke({"messages": [HumanMessage(content="x" * 20000)]},
                         {"configurable": {"thread_id": "oversized-request"}})
        self.assertEqual(model.calls, 0)

    def test_internal_summary_call_is_preflighted_too(self):
        model = LocalScriptedModel()
        summary_model = BudgetedSummaryModel(
            delegate=model,
            budget=ContextBudget(window_tokens=100, summarize_at_tokens=40,
                                 keep_recent_tokens=10, max_output_tokens=20),
        )
        with self.assertRaises(ContextBudgetExceeded):
            summary_model.invoke([HumanMessage(content="x" * 500)])
        self.assertEqual(model.calls, 0)

    def test_summary_triggers_at_configured_absolute_budget(self):
        model = LocalScriptedModel()
        agent = create_runtime(
            model=model,
            model_provider="v2scripted",
            product_tools=PRODUCT_TOOLS,
            checkpointer=MemorySaver(),
            budget=ContextBudget(
                window_tokens=5000,
                summarize_at_tokens=120,
                keep_recent_tokens=25,
                max_output_tokens=30,
            ),
        )
        config = {"configurable": {"thread_id": "long-chat"}}
        agent.invoke({"messages": [HumanMessage(content="x" * 400)]}, config)
        self.assertEqual(model.calls, 1)
        agent.invoke({"messages": [HumanMessage(content="y" * 400)]}, config)
        self.assertEqual(model.calls, 3)  # one summary call plus the public reply
        self.assertTrue(any(
            path.startswith("/conversation_history/")
            for path in agent.get_state(config).values["files"]
        ))

    def test_async_text_events(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                async with AsyncSqliteSaver.from_conn_string(str(Path(directory) / "checkpoints.sqlite")) as saver:
                    model = LocalStreamingModel()
                    agent = create_runtime(
                        model=model,
                        model_provider="v2scripted",
                        product_tools=PRODUCT_TOOLS,
                        checkpointer=saver,
                    )
                    pieces = []
                    async with await agent.astream_events(
                        {"messages": [HumanMessage(content="hello")]},
                        {"configurable": {"thread_id": "stream-a"}},
                        version="v3",
                    ) as run_stream:
                        async for message_stream in run_stream.messages:
                            async for text_piece in message_stream.text:
                                pieces.append(text_piece)
                    self.assertEqual("".join(pieces), "你好。")
                    self.assertGreater(len(pieces), 1)
                    self.assertEqual(set(model.bound_names), MODEL_TOOLS)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
