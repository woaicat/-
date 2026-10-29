"""ASGI entry point for the independent local workbench."""

from evaluation_agent.api import create_app
from evaluation_agent.model_config import model_from_environment

model = model_from_environment()
app = create_app(model=model, model_provider="openai" if model else None)
