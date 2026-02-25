"""LangChain powered generator that builds evaluation plans."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from .config import get_settings
from .models import (
    EvaluationRequest,
    Manifest,
    StepStatus,
    StreamEvent,
    WorkflowNode,
)
from .file_manager import sanitize_filename
from .planner import ALLOWED_STEP_IDS, PlanGenerator, PlanOutline, PlanStep
from .prompts import (
    DATA_DELIVERY_PROMPT,
    EXECUTION_MANUAL_PROMPT,
    MATERIAL_PROMPT,
    PLAN_DESIGN_PROMPT,
    PRODUCT_ANALYSIS_PROMPT,
    REFLECTION_PROMPT,
    SYSTEM_PROMPT,
)
from .tool_service import ToolService

EventEmitter = Callable[[StreamEvent], Awaitable[None] | None]


STEP_QA_CRITERIA = {
    "product_analysis": (
        "- 文档需包含产品概况、关键流程、风险与评测关注点。\n"
        "- 若信息不足需显式标注“待补充/假设”，并指出下一步验证思路。"
    ),
    "plan_design": (
        "- 方案需覆盖背景、范围假设、维度与指标表、数据指导、方法/流程、角色分工及风险应对。\n"
        "- 指标必须可量化或可核验，需说明每阶段的产出。"
    ),
    "data_delivery": (
        "- 数据集规范要描述目标、分类、字段约束、质量/合规要求。\n"
        "- 样本表需提供可读表头与 5-10 条示例，并解释用途、扩展方式。"
    ),
    "execution_manual": (
        "- 测试手册需包含方法矩阵、角色职责、步骤/输入/质控要点。\n"
        "- 如涉及大模型/自动化，需给出提示词、脚本或说明不可用的替代方案。"
    ),
    "default": (
        "- 结构完整，覆盖该步骤承诺的关键章节与 artefact。\n"
        "- 输出可执行且标注需补充的信息。"
    ),
}


def _format_workflow(nodes: List[WorkflowNode]) -> str:
    if not nodes:
        return "用户未提供详细的工作流节点。"

    lines = []
    for idx, node in enumerate(nodes, start=1):
        note_section = f"\n 额外说明：{node.notes}" if node.notes else ""
        lines.append(
            f"{idx}. 节点名称：{node.name}\n   输入要求：{node.input_spec}\n"
            f"   输出要求：{node.output_spec}{note_section}"
        )
    return "\n".join(lines)


@dataclass
class StepEntry:
    """Queue entry describing a planned action."""

    uid: str
    step: PlanStep
    source: str
    attempts: int = 0
    max_attempts: int = 1




class PlanGenerationAgent:
    """Generate evaluation plans using a multi-step prompt workflow."""

    def __init__(self, request_id: str, emitter: EventEmitter, callbacks: Optional[List] = None):
        self.request_id = request_id
        self.emitter = emitter
        settings = get_settings()
        self.enable_reflection_qa = bool(getattr(settings, "enable_reflection_qa", False))
        self.llm = ChatOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.model_name,
            temperature=0.3,
            max_tokens=10000,
            callbacks=callbacks,
        )
        self.parser = StrOutputParser()
        self.max_retries = max(0, settings.llm_max_retries)
        self.retry_delay = max(0.0, settings.llm_retry_delay)
        self._step_counter = 0
        self.planner = PlanGenerator(callbacks=callbacks)
        self._last_plan_outline: Optional[PlanOutline] = None
        self._handlers: Dict[str, Callable[[PlanStep, EvaluationRequest, ToolService, Dict[str, str]], Any]] = {
            "product_analysis": self._handle_product_analysis,
            "plan_design": self._handle_plan_design,
            "data_delivery": self._handle_data_delivery,
            "execution_manual": self._handle_execution_manual,
        }

    @staticmethod
    def _summarize_insight(text: str, fallback: str, limit: int = 180) -> str:
        collapsed = re.sub(r"\s+", " ", text or "").strip()
        candidate = ""
        if collapsed:
            for segment in re.split(r"[。.!?]\s*", collapsed):
                cleaned = segment.strip().strip("#*-•· ")
                if cleaned:
                    candidate = cleaned
                    break
            if not candidate:
                candidate = collapsed
        if not candidate:
            candidate = fallback
        candidate = candidate.strip()
        if len(candidate) > limit:
            candidate = candidate[: limit - 3].strip() + "..."
        return candidate or fallback

    async def _run_prompt(
        self,
        prompt: ChatPromptTemplate,
        *,
        include_prompt: bool = False,
        **kwargs,
    ) -> str | tuple[str, str | None]:
        prompt_preview: Optional[str] = None
        if include_prompt:
            prompt_preview = self._render_prompt_preview(prompt, kwargs)
        chain: Runnable = prompt | self.llm | self.parser
        attempt = 0
        while True:
            attempt += 1
            try:
                output = await chain.ainvoke(kwargs)
                if include_prompt:
                    return output, prompt_preview
                return output
            except Exception as exc:  # pylint: disable=broad-except
                if attempt > self.max_retries + 1:
                    raise
                await self._emit(
                    "llm_retry",
                    StepStatus.STARTED,
                    f"模型调用失败（第{attempt}次尝试），准备重试：{exc}",
                )
                if self.retry_delay:
                    await asyncio.sleep(self.retry_delay)

    def _render_prompt_preview(self, prompt: ChatPromptTemplate, variables: Dict[str, Any]) -> str:
        try:
            messages = prompt.format_messages(**variables)
        except Exception:  # pragma: no cover - prompt 解析失败不影响主流程
            return ""
        preview_lines = []
        for message in messages:
            role = getattr(message, "type", getattr(message, "role", ""))
            content = getattr(message, "content", "")
            preview_lines.append(f"[{role or 'message'}] {content}")
        return "\n\n".join(preview_lines)

    async def _emit(
        self,
        step: str,
        status: StepStatus,
        message: str,
        *,
        step_id: Optional[str] = None,
        phase: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = StreamEvent(
            request_id=self.request_id,
            step=step,
            step_id=step_id,
            status=status,
            message=message,
            phase=phase,
            metadata=metadata,
        )
        if self.emitter:
            maybe_awaitable = self.emitter(event)
            if maybe_awaitable:
                await maybe_awaitable  # type: ignore[func-returns-value]

    async def _emit_warning(
        self,
        message: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        step_id: Optional[str] = None,
    ) -> None:
        await self._emit(
            "warning",
            StepStatus.STARTED,
            message,
            step_id=step_id,
            phase="warning",
            metadata=metadata,
        )

    def build_context(self, request: EvaluationRequest) -> Dict[str, str]:
        return {
            "product_name": request.product_name,
            "scenario": request.application_scenario or "（暂无场景信息）",
            "target_users": request.target_users or "（暂无用户信息）",
            "business_goals": request.business_goals or "（暂无业务目标信息）",
            "core_features": "\n".join(f"- {feature}" for feature in request.core_features)
            or "（暂无核心功能描述）",
            "workflow": _format_workflow(request.workflow),
            "constraints": request.constraints or "（无额外约束）",
            "document_notes": request.document_notes or "（暂无文档补充信息）",
            "product_analysis_doc": "（尚未生成）",
            "plan_document": "（尚未生成）",
            "dataset_spec": "（尚未生成）",
            "sample_overview": "（暂无样本结构）",
            "sample_coverage_notes": "（暂无样本覆盖说明）",
            "sample_columns": "（暂无样本字段）",
            "execution_manual": "（尚未生成）",
        }

    def _make_step_entry(self, step: PlanStep, source: str) -> StepEntry:
        self._step_counter += 1
        max_attempts = 1 + (3 if step.allow_retry else 0)
        return StepEntry(
            uid=f"{source}-{self._step_counter:02}",
            step=step,
            source=source,
            attempts=0,
            max_attempts=max_attempts,
        )

    def _safe_json_loads(self, raw: str) -> Dict[str, Any]:
        cleaned = self._strip_code_fences(raw).strip()
        if not cleaned:
            return {}
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return data
        return {}

    def _plan_step_from_payload(self, payload: Dict[str, Any], index: int) -> Optional[PlanStep]:
        if not isinstance(payload, dict):
            return None
        step_id = str(payload.get("id") or payload.get("step_id") or "").strip()
        if step_id not in ALLOWED_STEP_IDS:
            return None
        title = payload.get("title") or f"追加步骤 {index}"
        description = payload.get("description") or "补充执行指引"
        expected_outputs = payload.get("expected_outputs")
        if isinstance(expected_outputs, list):
            outputs = [str(item).strip() for item in expected_outputs if str(item).strip()]
        else:
            outputs = []
        considerations_raw = payload.get("considerations")
        if isinstance(considerations_raw, list):
            considerations = [str(item).strip() for item in considerations_raw if str(item).strip()]
        elif isinstance(considerations_raw, str) and considerations_raw.strip():
            considerations = [considerations_raw.strip()]
        else:
            considerations = []
        pre_checks = payload.get("pre_checks")
        if isinstance(pre_checks, str):
            pre_check_text = pre_checks
        else:
            pre_check_text = None
        return PlanStep(
            step_id=step_id,
            title=title,
            description=description,
            expected_outputs=outputs,
            allow_retry=bool(payload.get("allow_retry", True)),
            success_criteria=str(payload.get("success_criteria") or ""),
            considerations=considerations,
            pre_checks=pre_check_text,
        )

    async def run_step_reflection(
        self,
        step: PlanStep,
        request: EvaluationRequest,
        context: Dict[str, str],
        result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not self.enable_reflection_qa:
            return None
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "human",
                    "当前节点负责“执行后质检”。请根据给定的验收标准与执行提示词对当前步骤产物做 continue / redo / extend 决策。\n"
                    "任务名称：{step_title}\n"
                    "执行提示词：\n{step_prompt}\n\n"
                    "验收标准：\n{acceptance_criteria}\n\n"
                    "当前步骤产物：\n{artefact_content}\n\n"
                    "产出文件：{files}\n错误：{errors}\n\n"
                    f"{REFLECTION_PROMPT}",
                )
            ]
        )
        files = result.get("files") or []
        files_text = ", ".join(files) if isinstance(files, list) else str(files)
        errors = result.get("error") or ""
        step_result = result.get("insight") or result.get("summary") or result.get("actions") or ""
        artefact_content = result.get("content") or ""
        if isinstance(artefact_content, list):
            artefact_content = "\n".join(str(item) for item in artefact_content)
        artefact_content = self._shorten_for_reflection(artefact_content or "（产物内容未提供）")
        criteria = STEP_QA_CRITERIA.get(step.step_id, STEP_QA_CRITERIA["default"])
        step_prompt = result.get("prompt_text") or step.description or "（无执行提示词记录）"
        step_prompt = self._shorten_for_reflection(step_prompt, limit=2000)
        raw_response = await self._run_prompt(
            prompt,
            product_name=request.product_name,
            step_title=step.title,
            step_description=step.description,
            expected_outputs="；".join(step.expected_outputs) or "（未指定）",
            step_result=step_result or "（未提供执行结果描述）",
            files=files_text or "（无产物）",
            errors=errors or "（无错误）",
            artefact_content=artefact_content,
            step_prompt=step_prompt,
            acceptance_criteria=criteria,
        )
        data = self._safe_json_loads(raw_response)
        decision = str(data.get("decision") or "continue").lower().strip()
        if decision not in {"continue", "redo", "extend"}:
            decision = "continue"
        redo_adjustments = []
        if isinstance(data.get("redo_adjustments"), list):
            redo_adjustments = [str(item).strip() for item in data["redo_adjustments"] if str(item).strip()]
        additional_steps_data = data.get("additional_steps")
        sanitized_steps: List[Dict[str, Any]] = []
        if isinstance(additional_steps_data, list):
            for idx, item in enumerate(additional_steps_data, start=1):
                try:
                    plan_step = self._plan_step_from_payload(item, idx)
                except Exception:  # pragma: no cover
                    plan_step = None
                if not plan_step:
                    continue
                if plan_step.step_id not in self._handlers:
                    continue
                sanitized_steps.append(plan_step.to_dict())
        missing_sections = []
        if isinstance(data.get("missing_sections"), list):
            missing_sections = [str(item).strip() for item in data["missing_sections"] if str(item).strip()]
        notes = data.get("notes")
        if isinstance(notes, list):
            notes_text = "；".join(str(item).strip() for item in notes if str(item).strip())
        else:
            notes_text = str(notes or "").strip()
        return {
            "decision": decision,
            "notes": notes_text,
            "redo_adjustments": redo_adjustments,
            "missing_sections": missing_sections,
            "additional_steps": sanitized_steps,
            "raw": data,
        }


    async def execute_step(
        self,
        step: PlanStep,
        request: EvaluationRequest,
        tools: ToolService,
        context: Dict[str, str],
    ) -> Dict[str, Any]:
        handler = self._handlers.get(step.step_id)
        if not handler:
            return {"skipped": True, "reason": f"未识别的步骤 {step.step_id}"}
        result = await handler(step, request, tools, context)
        return result

    @property
    def plan_outline(self) -> Optional[PlanOutline]:
        return self._last_plan_outline

    def record_plan_outline(self, outline: PlanOutline) -> None:
        self._last_plan_outline = outline

    # ------------------------------------------------------------------
    async def _handle_product_analysis(
        self,
        step: PlanStep,
        request: EvaluationRequest,
        tools: ToolService,
        context: Dict[str, str],
    ) -> Dict[str, Any]:
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    SYSTEM_PROMPT + "\n\n" + PRODUCT_ANALYSIS_PROMPT,
                ),
                (
                    "human",
                    "产品名称：{product_name}\n应用场景：{scenario}\n目标用户：{target_users}\n"
                    "业务目标：{business_goals}\n核心功能：\n{core_features}\n工作流：\n{workflow}\n"
                    "其他约束：{constraints}\n文档补充信息：{document_notes}",
                ),
            ]
        )
        content, prompt_text = await self._run_prompt(prompt, include_prompt=True, **context)
        artefact = tools.write_markdown(
            "产品解析.md",
            content,
            description="对产品价值、用户任务与评测风险的解析。",
        )
        self._update_context_fragment(context, "product_analysis_doc", content)
        insight = self._summarize_insight(
            content,
            "完成产品解析，梳理关键价值、用户任务与评测风险。",
        )
        return {
            "files": [artefact.filename],
            "insight": insight,
            "actions": [f"生成文档：{artefact.filename}"],
            "content": content,
            "prompt_text": prompt_text,
        }

    async def _handle_plan_design(
        self,
        step: PlanStep,
        request: EvaluationRequest,
        tools: ToolService,
        context: Dict[str, str],
    ) -> Dict[str, Any]:
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    SYSTEM_PROMPT + "\n\n" + PLAN_DESIGN_PROMPT,
                ),
                (
                    "human",
                    "产品上下文：\n产品名称：{product_name}\n应用场景：{scenario}\n目标用户：{target_users}\n"
                    "业务目标：{business_goals}\n核心功能：\n{core_features}\n工作流：\n{workflow}\n其他约束：{constraints}\n"
                    "文档补充信息：{document_notes}",
                ),
            ]
        )
        content, prompt_text = await self._run_prompt(prompt, include_prompt=True, **context)
        filename = sanitize_filename(
            f"{request.product_name}评测方案.md",
            fallback="评测方案.md",
        )
        artefact = tools.write_markdown(
            filename,
            content,
            description="完整的评测方案Markdown文档。",
        )
        self._update_context_fragment(context, "plan_document", content)
        insight = self._summarize_insight(
            content,
            "完成评测方案设计，明确目标、指标与执行计划。",
        )
        return {
            "files": [artefact.filename],
            "insight": insight,
            "actions": [f"生成评测方案：{artefact.filename}"],
            "content": content,
            "prompt_text": prompt_text,
        }

    async def _handle_data_delivery(
        self,
        step: PlanStep,
        request: EvaluationRequest,
        tools: ToolService,
        context: Dict[str, str],
    ) -> Dict[str, Any]:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", SYSTEM_PROMPT + "\n\n" + DATA_DELIVERY_PROMPT),
                (
                    "human",
                    "产品名称：{product_name}\n应用场景：{scenario}\n目标用户：{target_users}\n"
                    "业务目标：{business_goals}\n核心功能：\n{core_features}\n工作流：\n{workflow}\n"
                    "评测方案摘要：{plan_document}\n其他约束：{constraints}\n文档补充信息：{document_notes}",
                ),
            ]
        )
        raw, prompt_text = await self._run_prompt(prompt, include_prompt=True, **context)
        payload = self._safe_json_loads(raw)
        dataset_markdown = str(payload.get("dataset_spec_markdown") or "").strip()
        tables_data = payload.get("tables") if isinstance(payload.get("tables"), list) else []
        notes = payload.get("notes") if isinstance(payload.get("notes"), list) else []

        files: List[str] = []
        descriptions: List[str] = []
        content_sections: List[str] = []
        if dataset_markdown:
            spec = tools.write_markdown(
                "数据集规范.md",
                dataset_markdown,
                description="评测数据集准备与标注说明。",
            )
            files.append(spec.filename)
            descriptions.append("数据集规范")
            self._update_context_fragment(context, "dataset_spec", dataset_markdown)
            content_sections.append("# 数据集规范\n" + dataset_markdown)

        csv_summaries: List[str] = []
        sample_headers: List[str] = []
        table_markdowns: List[str] = []
        for idx, table in enumerate(tables_data, start=1):
            filename = table.get("filename") or f"样本表_{idx:02}.csv"
            rows = self._normalize_table_rows(table.get("rows"))
            if not rows:
                continue
            description = table.get("description") or f"样本数据表 #{idx}"
            csv_file = tools.write_csv(
                filename,
                rows,
                description=description,
            )
            files.append(csv_file.filename)
            descriptions.append(description)
            header = rows[0]
            if header:
                sample_headers.extend(header)
            row_count = max(len(rows) - 1, 0)
            csv_summaries.append(f"{csv_file.filename}（{row_count} 条，{description}）")
            table_markdown = self._render_table_markdown(csv_file.filename, rows)
            if table_markdown:
                table_markdowns.append(table_markdown)

        sample_notes_text = "\n".join(f"- {note}" for note in notes if note)
        if sample_notes_text:
            context["sample_coverage_notes"] = sample_notes_text

        if csv_summaries:
            context["sample_overview"] = "\n".join(csv_summaries)
        if sample_headers:
            unique_headers = []
            for header in sample_headers:
                if header not in unique_headers:
                    unique_headers.append(header)
            context["sample_columns"] = ", ".join(unique_headers[:20])

        insight = "完成数据集规范与样本准备" if files else "暂未生成数据相关产物"
        if csv_summaries:
            insight += f"；样本表覆盖：{'; '.join(csv_summaries)}"
        if table_markdowns:
            content_sections.extend(table_markdowns)

        result: Dict[str, Any] = {
            "files": files,
            "insight": insight,
            "notes": notes or None,
            "actions": [f"写入数据相关产物：{', '.join(files)}"] if files else [],
            "prompt_text": prompt_text,
        }
        if content_sections:
            result["content"] = "\n\n".join(content_sections)
        if notes:
            note_text = "; ".join(notes)
            result["notes"] = notes
            await self._emit_warning(
                "数据样本存在需确认的说明。",
                metadata={"details": note_text},
                step_id=step.step_id,
            )
        return result

    async def _handle_execution_manual(
        self,
        step: PlanStep,
        request: EvaluationRequest,
        tools: ToolService,
        context: Dict[str, str],
    ) -> Dict[str, Any]:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", SYSTEM_PROMPT + "\n\n" + EXECUTION_MANUAL_PROMPT),
                (
                    "human",
                    "产品名称：{product_name}\n应用场景：{scenario}\n目标用户：{target_users}\n"
                    "业务目标：{business_goals}\n核心功能：\n{core_features}\n工作流：\n{workflow}\n"
                    "评测方案：{plan_document}\n数据集规范：{dataset_spec}\n样本概览：{sample_overview}\n"
                    "样本说明：{sample_coverage_notes}\n"
                    "其他约束：{constraints}\n文档补充信息：{document_notes}",
                ),
            ]
        )
        manual_content, prompt_text = await self._run_prompt(prompt, include_prompt=True, **context)
        manual_file = tools.write_markdown(
            "测试执行指导手册.md",
            manual_content,
            description="跨方法的测试执行指引。",
        )
        self._update_context_fragment(context, "execution_manual", manual_content)
        insight = self._summarize_insight(
            manual_content,
            "完成测试执行指导手册，覆盖人工/自动化/大模型方法。",
        )

        material_payload = await self._generate_material_payload(request, context, manual_content)
        material_text = self._render_material_sections(material_payload)
        material_files = self._persist_material_files(material_payload, tools)
        result_files = [manual_file.filename] + material_files

        if material_payload.get("notes"):
            await self._emit_warning(
                "派生产物存在注意事项。",
                metadata={"details": material_payload.get("notes")},
                step_id=step.step_id,
            )

        content_sections = [manual_content]
        if material_text:
            content_sections.append(material_text)

        return {
            "files": result_files,
            "insight": insight,
            "actions": [f"输出测试执行手册及物料：{', '.join(result_files)}"],
            "content": "\n\n".join(content_sections),
            "prompt_text": prompt_text,
        }

    async def generate_final_summary(
        self,
        request: EvaluationRequest,
        context: Dict[str, str],
        manifest: Manifest,
        reflections: List[Dict[str, Any]],
    ) -> str:
        file_overview = "\n".join(
            f"- {file.filename}：{file.description or '无描述'}" for file in manifest.files
        )
        if not file_overview:
            file_overview = "（尚无产物）"

        plan_summary = self.plan_outline.summary if self.plan_outline else "（尚无计划总结）"
        decision_lines: List[str] = []
        insight_lines: List[str] = []
        for item in reflections:
            step_label = item.get("step_title") or item.get("title") or item.get("step_id")
            decision = item.get("decision") or "continue"
            note = item.get("notes") or item.get("insight") or item.get("error")
            if step_label:
                decision_lines.append(f"- {step_label}：{decision}")
                if note:
                    insight_lines.append(f"- {step_label}：{note}")
        decision_overview = "\n".join(decision_lines) if decision_lines else "（暂无决策记录）"
        insight_overview = "\n".join(insight_lines) if insight_lines else "（暂无思考记录）"

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    SYSTEM_PROMPT
                    + "\n\n根据任务过程，总结评测方案生成的关键成果与后续建议。要求输出 2-3 段中文总结，语气专业精炼。",
                ),
                (
                    "human",
                    "产品名称：{product_name}\n应用场景：{scenario}\n目标用户：{target_users}\n"
                    "业务目标：{business_goals}\n计划概述：{plan_summary}\n\n产物清单：\n{file_overview}\n\n"
                    "执行亮点：\n{insight_overview}\n\n"
                    "关键决策与调整：\n{decision_overview}\n\n"
                    "请输出：\n1. 本次任务的总体完成情况与亮点。\n2. 对评测执行或后续落地的建议。\n"
                    "若存在不足或需后续补充的部分，请简要提醒。",
                ),
            ]
        )
        summary = await self._run_prompt(
            prompt,
            product_name=request.product_name,
            scenario=context.get("scenario", ""),
            target_users=context.get("target_users", ""),
            business_goals=context.get("business_goals", ""),
            plan_summary=plan_summary,
            file_overview=file_overview,
            insight_overview=insight_overview,
            decision_overview=decision_overview,
        )
        return summary.strip()

    # ------------------------------------------------------------------
    @staticmethod
    def _update_context_fragment(context: Dict[str, str], key: str, content: str, limit: int = 4000) -> None:
        if not content:
            return
        fragment = content.strip()
        if len(fragment) > limit:
            fragment = f"{fragment[:limit]}...\n（内容过长，已截断，完整内容请查看输出文件）"
        context[key] = fragment

    @staticmethod
    def _shorten_for_reflection(text: str, limit: int = 4000) -> str:
        snippet = (text or "").strip()
        if not snippet:
            return ""
        if len(snippet) > limit:
            return f"{snippet[:limit]}...\n（内容过长，已截断，完整内容请查看输出文件）"
        return snippet

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            lines = lines[1:] if len(lines) > 1 else []
            while lines and lines[-1].strip() == "```":
                lines.pop()
            stripped = "\n".join(lines).strip()
        return stripped

    def _normalize_table_rows(self, raw_rows: Any) -> List[List[str]]:
        rows: List[List[str]] = []
        if not isinstance(raw_rows, list):
            return rows
        for raw in raw_rows:
            if isinstance(raw, list):
                rows.append([self._stringify(cell) for cell in raw])
            elif isinstance(raw, dict):
                ordered = [self._stringify(value) for _, value in raw.items()]
                rows.append(ordered)
            else:
                rows.append([self._stringify(raw)])
        return rows

    def _render_table_markdown(self, title: str, rows: List[List[str]]) -> str:
        if not rows:
            return ""
        max_width = max(len(row) for row in rows)
        normalized = [row + [""] * (max_width - len(row)) for row in rows]
        header = normalized[0]
        body = normalized[1:] or [[""] * max_width]
        header_line = " | ".join(header)
        separator_line = " | ".join(["---"] * max_width)
        body_lines = [" | ".join(row) for row in body]
        lines = [f"## {title}", f"| {header_line} |", f"| {separator_line} |"]
        lines.extend(f"| {line} |" for line in body_lines)
        return "\n".join(lines)

    async def _generate_material_payload(
        self,
        request: EvaluationRequest,
        context: Dict[str, str],
        manual_content: str,
    ) -> Dict[str, Any]:
        sample_columns = context.get("sample_columns") or context.get("sample_overview") or "（暂无样本字段）"
        manual_excerpt = manual_content[:40000]
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", SYSTEM_PROMPT + "\n\n" + MATERIAL_PROMPT),
                (
                    "human",
                    "产品名称：{product_name}\n样本字段：{sample_columns}\n执行手册摘录：{manual_excerpt}\n"
                    "评测指标摘要：{plan_summary}\n若某类方法不适用，请在 notes 中说明原因。",
                ),
            ]
        )
        raw = await self._run_prompt(
            prompt,
            product_name=request.product_name,
            sample_columns=sample_columns,
            manual_excerpt=manual_excerpt,
            plan_summary=self.plan_outline.summary if self.plan_outline else "",
        )
        return self._safe_json_loads(raw)

    def _persist_material_files(self, payload: Dict[str, Any], tools: ToolService) -> List[str]:
        files: List[str] = []

        llm_prompts = payload.get("llm_prompts") if isinstance(payload.get("llm_prompts"), list) else []
        for idx, prompt_entry in enumerate(llm_prompts, start=1):
            name = prompt_entry.get("name") or f"LLM提示词_{idx}"
            usage = prompt_entry.get("usage") or "（未提供使用说明）"
            prompt_text = prompt_entry.get("prompt_text") or ""
            content = f"# {name}\n\n## 使用场景\n{usage}\n\n## 提示词\n{prompt_text}"
            filename = sanitize_filename(f"{name}.md", fallback=f"llm_prompt_{idx}.md")
            artefact = tools.write_markdown(
                filename,
                content,
                description=prompt_entry.get("description") or "LLM 提示词模板",
            )
            files.append(artefact.filename)

        automation_scripts = (
            payload.get("automation_scripts") if isinstance(payload.get("automation_scripts"), list) else []
        )
        for idx, script in enumerate(automation_scripts, start=1):
            filename = script.get("filename") or f"automation_script_{idx}.py"
            description = script.get("description") or "自动化脚本"
            content = script.get("content") or "# TODO: 填写自动化脚本逻辑\n"
            artefact = tools.write_text(filename, content, description=description)
            files.append(artefact.filename)

        manual_templates = (
            payload.get("manual_templates") if isinstance(payload.get("manual_templates"), list) else []
        )
        for idx, template in enumerate(manual_templates, start=1):
            filename = template.get("filename") or f"记录模板_{idx}.md"
            description = template.get("description") or "人工记录模板"
            content = template.get("content") or "（待补充记录模板内容）"
            artefact = tools.write_markdown(filename, content, description=description)
            files.append(artefact.filename)

        return files

    def _render_material_sections(self, payload: Dict[str, Any]) -> str:
        sections: List[str] = []
        llm_prompts = payload.get("llm_prompts") if isinstance(payload.get("llm_prompts"), list) else []
        for prompt_entry in llm_prompts:
            name = prompt_entry.get("name") or "LLM提示词"
            usage = prompt_entry.get("usage") or "（未提供使用说明）"
            prompt_text = prompt_entry.get("prompt_text") or "（未提供提示词内容）"
            sections.append(f"# {name}\n\n## 使用场景\n{usage}\n\n## 提示词\n{prompt_text}")
        automation_scripts = (
            payload.get("automation_scripts") if isinstance(payload.get("automation_scripts"), list) else []
        )
        for script in automation_scripts:
            filename = script.get("filename") or "automation_script.py"
            description = script.get("description") or "自动化脚本"
            content = script.get("content") or "# TODO"
            sections.append(f"# {filename}\n\n## 脚本说明\n{description}\n\n## 内容\n{content}")
        manual_templates = (
            payload.get("manual_templates") if isinstance(payload.get("manual_templates"), list) else []
        )
        for template in manual_templates:
            filename = template.get("filename") or "记录模板.md"
            description = template.get("description") or "人工记录模板"
            content = template.get("content") or "（待补充内容）"
            sections.append(f"# {filename}\n\n## 模板说明\n{description}\n\n## 模板内容\n{content}")
        return "\n\n".join(section for section in sections if section.strip())

    @staticmethod
    def _stringify(value: Any) -> str:
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
