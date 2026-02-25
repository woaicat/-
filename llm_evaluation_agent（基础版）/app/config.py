"""Application configuration settings."""

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    openai_api_key: Optional[str] = Field(
        default=None,
        env="OPENAI_API_KEY",
        description="API key used to authenticate with OpenAI compatible endpoints.",
    )
    openai_base_url: Optional[str] = Field(
        default=None,
        env="OPENAI_BASE_URL",
        description="Optional override for the OpenAI compatible API base URL.",
    )
    model_name: str = Field(
        default="gpt-4o-mini",
        env="MODEL_NAME",
        description="Default chat model used for plan generation.",
    )
    outputs_dir: Path = Field(
        default=Path("./outputs"),
        env="OUTPUTS_DIR",
        description="Base directory where generated artefacts are stored.",
    )
    uploads_dir: Path = Field(
        default=Path("./uploads"),
        env="UPLOADS_DIR",
        description="Directory used to store temporarily uploaded product documents.",
    )
    max_output_age_hours: int = Field(
        default=6,
        env="MAX_OUTPUT_AGE_HOURS",
        description="Retention window for generated artefacts before cleanup.",
    )
    api_token: Optional[str] = Field(
        default=None,
        env="API_TOKEN",
        description="Optional API token required for calling protected endpoints.",
    )
    llm_max_retries: int = Field(
        default=2,
        env="LLM_MAX_RETRIES",
        description="Number of times to retry failed LLM calls.",
    )
    llm_retry_delay: float = Field(
        default=2.0,
        env="LLM_RETRY_DELAY",
        description="Delay (seconds) between LLM retry attempts.",
    )
    embedding_api_key: Optional[str] = Field(
        default=None,
        env="EMBEDDING_API_KEY",
        description="API key for external embedding provider (e.g. Doubao).",
    )
    embedding_base_url: Optional[str] = Field(
        default=None,
        env="EMBEDDING_BASE_URL",
        description="Embedding API base URL.",
    )
    embedding_model: str = Field(
        default="doubao-embedding-text-240715",
        env="EMBEDDING_MODEL",
        description="Embedding model identifier for vector service.",
    )
    enable_reflection_qa: bool = Field(
        default=True,
        env="ENABLE_REFLECTION_QA",
        description="Whether to run the post-step quality review (REFLECTION_PROMPT).",
    )
    langsmith_api_key: Optional[str] = Field(
        default=None,
        env="LANGSMITH_API_KEY",
        description="API key for LangSmith tracing (optional).",
    )
    langsmith_project: Optional[str] = Field(
        default=None,
        env="LANGSMITH_PROJECT",
        description="LangSmith project name used for tracing sessions.",
    )
    langsmith_endpoint: Optional[str] = Field(
        default=None,
        env="LANGSMITH_ENDPOINT",
        description="Optional LangSmith custom endpoint.",
    )

    class Config:
        case_sensitive = False
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    """Return cached settings instance."""

    settings = Settings()
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    return settings
