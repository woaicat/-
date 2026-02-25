import time
import pytest

from fastapi.testclient import TestClient

from app import config as config_module
from app import main as main_module
from app.models import EvaluationRequest


class DummyRunner:
    def __init__(self, request_id: str, emitter, tools, context_manager=None, callbacks=None):  # pylint: disable=unused-argument
        self.request_id = request_id
        self.tools = tools

    def build_context(self, payload: EvaluationRequest):
        return {"product_name": payload.product_name}

    async def run(self, payload: EvaluationRequest, _context):
        artefact = self.tools.write_text("结果.txt", f"demo for {payload.product_name}")
        return {
            "plan_steps": [],
            "executed": [{"step_id": "demo"}],
            "reflections": [{"step_title": "demo", "decision": "continue", "notes": artefact.filename}],
            "summary": "完成",
        }


@pytest.fixture(autouse=True)
def reset_settings(tmp_path, monkeypatch):
    outputs_dir = tmp_path / "outputs"
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setenv("OUTPUTS_DIR", str(outputs_dir))
    monkeypatch.setenv("UPLOADS_DIR", str(uploads_dir))
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def test_generate_plan_creates_sanitized_manifest(monkeypatch):
    monkeypatch.setattr(main_module, "LangGraphAgentRunner", DummyRunner)
    manifest = None

    with TestClient(main_module.app) as client:
        response = client.post("/generate-plan", json={"product_name": "../危险产品"})
        assert response.status_code == 200
        request_id = response.json()["request_id"]

        for _ in range(20):
            manifest_resp = client.get(f"/manifest/{request_id}")
            if manifest_resp.status_code == 200:
                manifest = manifest_resp.json()
                break
            time.sleep(0.1)

    assert manifest, "manifest should be ready"
    filenames = [item["filename"] for item in manifest["files"]]
    assert all("/" not in name and ".." not in name for name in filenames)
