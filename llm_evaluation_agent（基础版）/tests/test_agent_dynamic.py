import pytest

from app import agent as agent_module
from app import config as config_module
from app import file_manager as file_manager_module
from app import langgraph_runner as runner_module
from app import planner as planner_module
from app.context_manager import ContextManager
from app.langgraph_runner import LangGraphAgentRunner
from app.models import EvaluationRequest
from app.planner import PlanOutline, PlanStep
from app.tool_service import ToolService


class _DummyLLM:
    def __init__(self, *args, **kwargs):
        pass

    async def ainvoke(self, *_args, **_kwargs):
        return "{}"


class _VectorStub:
    available = False

    async def search(self, **kwargs):  # pylint: disable=unused-argument
        return []

    async def add_document(self, **kwargs):  # pylint: disable=unused-argument
        return None


@pytest.mark.asyncio
async def test_langgraph_runner_executes_plan(monkeypatch, tmp_path):
    fake_settings = config_module.Settings(
        openai_api_key="sk-test",
        outputs_dir=tmp_path,
        uploads_dir=tmp_path,
        enable_reflection_qa=False,
    )

    monkeypatch.setattr(agent_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(file_manager_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(planner_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(agent_module, "ChatOpenAI", _DummyLLM)
    monkeypatch.setattr(planner_module, "ChatOpenAI", _DummyLLM)

    steps = [
        PlanStep(
            step_id="product_analysis",
            title="产品解析",
            description="梳理产品价值",
            expected_outputs=["产品解析.md"],
            success_criteria="识别价值",
        ),
        PlanStep(
            step_id="plan_design",
            title="方案设计",
            description="输出完整方案",
            expected_outputs=["方案.md"],
            success_criteria="覆盖指标",
        ),
    ]

    async def fake_create_plan(self, _request):
        return PlanOutline(summary="LangGraph计划", steps=steps), []

    monkeypatch.setattr(planner_module.PlanGenerator, "create_plan", fake_create_plan, raising=False)

    async def fake_execute(self, step, request, tools, _context):
        artefact = tools.write_text(f"{step.step_id}.txt", f"{step.title} for {request.product_name}")
        return {"files": [artefact.filename], "insight": f"{step.title}完成"}

    monkeypatch.setattr(agent_module.PlanGenerationAgent, "execute_step", fake_execute, raising=False)

    async def fake_summary(self, request, _context, manifest, _reflections):
        return f"{request.product_name} 完成 {len(manifest.files)} 个产物"

    monkeypatch.setattr(
        agent_module.PlanGenerationAgent,
        "generate_final_summary",
        fake_summary,
        raising=False,
    )

    request = EvaluationRequest(product_name="Demo", core_features=["聊天"], workflow=[])
    tools = ToolService(request_id="req-1", product_name="Demo")
    context_manager = ContextManager(vector_service=_VectorStub())
    runner = LangGraphAgentRunner(
        request_id="req-1",
        emitter=lambda event: None,
        tools=tools,
        context_manager=context_manager,
    )

    context = runner.build_context(request)
    final_state = await runner.run(request, context)

    assert len(final_state["executed"]) == len(steps)
    reflections = final_state.get("reflections") or []
    assert len(reflections) == len(steps)
    assert all(reflection.get("step_title") for reflection in reflections)

    files = tools.list_generated_files()
    assert {file.filename for file in files} == {"product_analysis.txt", "plan_design.txt"}
    assert final_state.get("summary") == "Demo 完成 2 个产物"
