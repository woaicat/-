"""Optional OpenAI-compatible model adapter; no secret or API call at import time."""

import os

from langchain_openai import ChatOpenAI

from evaluation_agent.runtime import ContextBudget


def model_from_environment(budget: ContextBudget = ContextBudget()) -> ChatOpenAI | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    model_name = os.environ.get("MODEL_NAME", "").strip()
    if not model_name:
        raise RuntimeError("MODEL_NAME is required when OPENAI_API_KEY is configured")
    configured_context = os.environ.get("MODEL_CONTEXT_TOKENS", "").strip()
    if not configured_context:
        raise RuntimeError("MODEL_CONTEXT_TOKENS is required when a model is configured")
    try:
        physical_window = int(configured_context)
    except ValueError as exc:
        raise RuntimeError("MODEL_CONTEXT_TOKENS must be an integer") from exc
    if physical_window < budget.window_tokens:
        raise RuntimeError("Configured model context is smaller than the 128000-token work window")
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        streaming=True,
        max_completion_tokens=budget.max_output_tokens,
        max_retries=2,
        timeout=90,
    )
