"""Generate and manage execution plans for the evaluation workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from .config import get_settings
from .prompts import PLAN_PROMPT, PLAN_REPAIR_PROMPT, SYSTEM_PROMPT
from .models import EvaluationRequest
from langchain_openai import ChatOpenAI


ALLOWED_STEP_IDS = {
    "product_analysis",
    "plan_design",
    "data_delivery",
    "execution_manual",
}


@dataclass
class PlanStep:
    """Represents a single executable step in the plan."""

    step_id: str
    title: str
    description: str
    expected_outputs: List[str]
    allow_retry: bool = True
    success_criteria: str = ""
    considerations: List[str] = field(default_factory=list)
    pre_checks: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.step_id,
            "title": self.title,
            "description": self.description,
            "expected_outputs": self.expected_outputs,
            "allow_retry": self.allow_retry,
            "success_criteria": self.success_criteria,
            "considerations": self.considerations,
            "pre_checks": self.pre_checks,
        }


@dataclass
class PlanOutline:
    """Structured plan returned by the planner."""

    summary: str
    steps: List[PlanStep]


class PlanGenerator:
    """Create structured plans using the LLM."""

    def __init__(self, callbacks: Optional[List] = None) -> None:
        settings = get_settings()
        self.llm = ChatOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.model_name,
            temperature=0.1,
            max_tokens=6000,
            callbacks=callbacks,
        )
        self.parser = StrOutputParser()

    async def create_plan(self, request: EvaluationRequest) -> Tuple[PlanOutline, List[str]]:
        context = {
            "product_name": request.product_name,
            "scenario": request.application_scenario or "（暂无场景信息）",
            "target_users": request.target_users or "（暂无用户信息）",
            "business_goals": request.business_goals or "（暂无业务目标信息）",
            "core_features": "\n".join(f"- {feature}" for feature in request.core_features)
            or "（暂无核心功能描述）",
            "workflow": "\n".join(
                f"- {node.name}: 输入 {node.input_spec} -> 输出 {node.output_spec}"
                for node in request.workflow
            )
            or "（暂无工作流信息）",
            "constraints": request.constraints or "（暂无额外约束）",
            "document_notes": request.document_notes or "（暂无文档补充信息）",
        }

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    SYSTEM_PROMPT
                    + "\n\n"
                    + PLAN_PROMPT,
                ),
                (
                    "human",
                    "请为以下产品生成行动计划。产品信息：\n"
                    "产品名称：{product_name}\n应用场景：{scenario}\n目标用户：{target_users}\n"
                    "业务目标：{business_goals}\n核心功能：\n{core_features}\n工作流：\n{workflow}\n"
                    "其他约束：{constraints}\n文档补充信息：{document_notes}",
                ),
            ]
        )

        chain: Runnable = prompt | self.llm | self.parser
        raw_plan = await chain.ainvoke(context)
        return await self._ensure_plan_outline(raw_plan, request)

    def _parse_plan(self, raw_plan: str) -> Tuple[Optional[PlanOutline], List[str]]:
        warnings: List[str] = []
        cleaned = raw_plan.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()[1:]
            while lines and lines[-1].strip() == "```":
                lines.pop()
            cleaned = "\n".join(lines)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            warnings.append("模型返回的行动计划不是有效 JSON，准备尝试修复。")
            return None, warnings

        steps: List[PlanStep] = []
        summary = ""
        if isinstance(data, dict):
            summary = str(data.get("plan_summary") or "")
            raw_steps = data.get("steps")
        else:
            raw_steps = None
        if isinstance(raw_steps, list):
            for index, item in enumerate(raw_steps, start=1):
                if not isinstance(item, dict):
                    continue
                step_id_raw = str(item.get("id") or f"step-{index:02}")
                step_id = step_id_raw if step_id_raw in ALLOWED_STEP_IDS else None
                title = item.get("title") or f"步骤 {index}"
                description = item.get("description") or ""
                expected_outputs = item.get("expected_outputs")
                if not isinstance(expected_outputs, list):
                    expected_outputs = []
                allow_retry = bool(item.get("allow_retry", True))
                success_criteria = str(item.get("success_criteria") or "")
                considerations_raw = item.get("considerations") or []
                if isinstance(considerations_raw, list):
                    considerations = [str(v) for v in considerations_raw if str(v).strip()]
                elif isinstance(considerations_raw, str):
                    considerations = [considerations_raw]
                else:
                    considerations = []
                pre_checks = item.get("pre_checks")
                if pre_checks is not None:
                    pre_checks = str(pre_checks)
                if not step_id:
                    continue
                steps.append(
                    PlanStep(
                        step_id=step_id,
                        title=title,
                        description=description,
                        expected_outputs=[str(v) for v in expected_outputs],
                        allow_retry=allow_retry,
                        success_criteria=success_criteria,
                        considerations=considerations,
                        pre_checks=pre_checks,
                    )
                )

        if not steps:
            warnings.append("行动计划缺少有效步骤，准备尝试修复。")
            return None, warnings

        return PlanOutline(summary=summary, steps=steps), warnings

    async def _ensure_plan_outline(
        self, raw_plan: str, request: EvaluationRequest
    ) -> Tuple[PlanOutline, List[str]]:
        outline, warnings = self._parse_plan(raw_plan)
        if outline:
            return outline, warnings

        snippet = self._extract_json_block(raw_plan)
        if snippet and snippet != raw_plan:
            candidate, snippet_warnings = self._parse_plan(snippet)
            if candidate:
                warnings.append("检测到可能的 JSON 片段并成功解析。")
                warnings.extend(snippet_warnings)
                return candidate, warnings

        repaired = await self._repair_plan_output(raw_plan, request)
        if repaired:
            candidate, repaired_warnings = self._parse_plan(repaired)
            if candidate:
                warnings.append("已通过格式修复器重写行动计划。")
                warnings.extend(repaired_warnings)
                return candidate, warnings

        details = "；".join(warnings) if warnings else "模型未返回合法计划格式。"
        raise ValueError(f"无法生成有效行动计划，请重试：{details}")

    @staticmethod
    def _extract_json_block(text: str) -> Optional[str]:
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return None
        return text[first : last + 1]

    async def _repair_plan_output(
        self, raw_plan: str, request: EvaluationRequest
    ) -> Optional[str]:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", PLAN_REPAIR_PROMPT),
                (
                    "human",
                    "产品名称：{product_name}\n原始输出如下：\n```\n{original}\n```\n请将其转换为合法 JSON。",
                ),
            ]
        )
        chain: Runnable = prompt | self.llm | self.parser
        try:
            return await chain.ainvoke(
                {
                    "product_name": request.product_name,
                    "original": raw_plan,
                }
            )
        except Exception:
            return None
