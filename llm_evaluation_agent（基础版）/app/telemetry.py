"""LangSmith / tracing helpers for optional observability hooks."""

from __future__ import annotations

import logging
import os
from typing import List

from .config import get_settings

try:  # pragma: no cover - optional dependency
    from langchain.callbacks.tracers.langchain import LangChainTracer
except Exception:  # pylint: disable=broad-except
    LangChainTracer = None  # type: ignore


logger = logging.getLogger(__name__)


def _configure_langsmith_env() -> bool:
    """Ensure required environment variables are present before tracing."""

    settings = get_settings()
    if not settings.langsmith_api_key or not settings.langsmith_project:
        return False

    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
    if settings.langsmith_endpoint:
        os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)
    return True


def build_langsmith_callbacks(request_id: str, product_name: str) -> List:
    """Create LangSmith callback handlers for the given request."""

    if LangChainTracer is None:
        return []

    if not _configure_langsmith_env():
        return []

    tracer = LangChainTracer(project_name=get_settings().langsmith_project)
    try:
        tracer.tags = [tag for tag in ("llm_evaluator", request_id, product_name) if tag]
        tracer.metadata = {"request_id": request_id, "product_name": product_name}
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("Failed to attach LangSmith metadata: %s", exc)
    return [tracer]
