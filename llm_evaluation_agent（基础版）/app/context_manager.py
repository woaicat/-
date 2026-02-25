"""Lightweight context manager integrating vector search for LangGraph state."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import EvaluationRequest
from .planner import PlanStep
from .vector_service import VectorService


class ContextManager:
    """Enrich execution context with vector hits and historical signals."""

    def __init__(self, vector_service: Optional[VectorService] = None):
        self.vector_service = vector_service or VectorService()
        self._invocations: List[Dict[str, str]] = []

    async def enrich_context(
        self,
        *,
        request_id: str,
        request: EvaluationRequest,
        context: Dict[str, str],
        step: PlanStep,
        history: List[Dict[str, str]] | None,
    ) -> Dict[str, str]:
        new_context = dict(context)
        query = f"{request.product_name} {step.title} {step.description}".strip()
        vector_hits = await self.vector_service.search(
            query=query,
            request_id=request_id,
            limit=3,
        )
        if self.vector_service.available:
            hit_count = len(vector_hits)
            self._log_invocation(
                "vector_search",
                step.title,
                details=f"命中 {hit_count} 条" if hit_count else "无命中",
            )
        if vector_hits:
            formatted = "\n".join(f"- {hit}" for hit in vector_hits)
            new_context["vector_notes"] = formatted
        if history:
            recent = history[-1]
            summary = recent.get("result", {}).get("insight")
            if summary:
                new_context["recent_insight"] = summary
        new_context["current_step_title"] = step.title
        return new_context

    async def record_step_result(
        self,
        *,
        request_id: str,
        step: PlanStep,
        result: Dict[str, Any],
    ) -> None:
        text = result.get("insight") or result.get("summary")
        if not text:
            files = result.get("files") or []
            if files:
                text = f"产物：{', '.join(files)}"
        if not text:
            return
        await self.vector_service.add_document(
            request_id=request_id,
            step_id=step.step_id,
            content=text,
            metadata={"step_title": step.title},
        )

    async def add_source_document(
        self,
        *,
        request_id: str,
        source_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write uploaded requirement docs or summaries into vector store."""
        if not content.strip():
            return
        await self.vector_service.add_document(
            request_id=request_id,
            step_id=source_id,
            content=content,
            metadata=metadata or {"step_title": "源文档"},
        )

    def _log_invocation(self, name: str, target: str, details: Optional[str] = None) -> None:
        entry: Dict[str, str] = {"name": name, "target": target}
        if details:
            entry["details"] = details
        self._invocations.append(entry)

    def flush_invocations(self) -> List[Dict[str, str]]:
        invocations = list(self._invocations)
        self._invocations.clear()
        return invocations
