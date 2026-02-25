"""Utilities for extracting structured information from uploaded markdown documents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from .config import get_settings
from .models import DocumentSummary, WorkflowNode


CHUNK_CHAR_LIMIT = 8000
MAX_CHUNKS = 10


@dataclass
class DocumentExtractionResult:
    """Hold intermediate data produced during document analysis."""

    summary_notes: str
    structured: DocumentSummary


class DocumentAnalyzer:
    """Chunk markdown files and extract structured product information."""

    def __init__(self, callbacks: Optional[List] = None) -> None:
        settings = get_settings()
        self.llm = ChatOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.model_name,
            temperature=0,
            max_tokens=1400,
            callbacks=callbacks,
        )
        self.parser = StrOutputParser()

    async def analyze(self, product_name: str, markdown_path: Path) -> DocumentExtractionResult:
        raw_text = markdown_path.read_text(encoding="utf-8", errors="ignore")
        chunks = list(self._chunk_text(raw_text))
        chunk_summaries = await self._summarize_chunks(product_name, chunks)
        combined_summary = "\n\n".join(chunk_summaries)
        structured = await self._derive_structured(product_name, combined_summary)
        return DocumentExtractionResult(summary_notes=combined_summary, structured=structured)

    def _chunk_text(self, text: str) -> Iterable[str]:
        trimmed = text[: CHUNK_CHAR_LIMIT * MAX_CHUNKS]
        current: List[str] = []
        current_len = 0
        for paragraph in trimmed.split("\n\n"):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            paragraph_len = len(paragraph) + 2  # account for double newline joiners
            if current and current_len + paragraph_len > CHUNK_CHAR_LIMIT:
                yield "\n\n".join(current)
                current = [paragraph]
                current_len = paragraph_len
            else:
                current.append(paragraph)
                current_len += paragraph_len

        if current:
            yield "\n\n".join(current)

    async def _summarize_chunks(self, product_name: str, chunks: List[str]) -> List[str]:
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是资深的产品分析师，请将给定的产品文档片段总结为要点，"
                    "突出产品目标、功能、用户、流程、风险。请输出不超过8条要点，以列表形式。",
                ),
                (
                    "human",
                    "产品名称：{product_name}\n文档片段：\n{content}\n\n"
                    "请按照以下格式输出：\n"
                    "- 要点1\n- 要点2\n...",
                ),
            ]
        )
        tasks = []
        for idx, chunk in enumerate(chunks, start=1):
            chain: Runnable = prompt | self.llm | self.parser
            tasks.append(
                chain.ainvoke({"product_name": product_name, "content": chunk, "index": idx})
            )
        summaries = await asyncio.gather(*tasks)
        cleaned = [summary.strip() for summary in summaries if summary.strip()]
        return cleaned

    async def _derive_structured(
        self,
        product_name: str,
        combined_summary: str,
    ) -> DocumentSummary:
        extraction_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是一名结构化信息抽取专家，请根据要点总结输出一个 JSON 对象，"
                    "字段包括 application_scenario, target_users, business_goals, "
                    "core_features (字符串数组), workflow (数组，每项包含 name, input_spec, output_spec, notes，可为空字符串), "
                    "constraints, notes。请勿输出额外文本。",
                ),
                (
                    "human",
                    "产品名称：{product_name}\n要点总结：\n{summary}\n\n"
                    "请输出 JSON 对象。",
                ),
            ]
        )
        chain: Runnable = extraction_prompt | self.llm | self.parser
        raw_json = await chain.ainvoke({"product_name": product_name, "summary": combined_summary})
        return self._parse_structured(raw_json)

    def _parse_structured(self, raw_json: str) -> DocumentSummary:
        import json

        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            return DocumentSummary()

        workflow_nodes: List[WorkflowNode] = []
        raw_nodes = data.get("workflow") if isinstance(data, dict) else None
        if isinstance(raw_nodes, list):
            for node in raw_nodes:
                if not isinstance(node, dict):
                    continue
                try:
                    workflow_nodes.append(
                        WorkflowNode(
                            name=node.get("name") or "未命名节点",
                            input_spec=node.get("input_spec") or "",
                            output_spec=node.get("output_spec") or "",
                            notes=node.get("notes"),
                        )
                    )
                except Exception:  # broad to keep pipeline resilient
                    continue

        core_features: List[str] = []
        raw_features = data.get("core_features") if isinstance(data, dict) else None
        if isinstance(raw_features, list):
            core_features = [str(item) for item in raw_features if str(item).strip()]

        return DocumentSummary(
            application_scenario=data.get("application_scenario") if isinstance(data, dict) else None,
            target_users=data.get("target_users") if isinstance(data, dict) else None,
            business_goals=data.get("business_goals") if isinstance(data, dict) else None,
            core_features=core_features,
            workflow=workflow_nodes,
            constraints=data.get("constraints") if isinstance(data, dict) else None,
            notes=data.get("notes") if isinstance(data, dict) else None,
        )
