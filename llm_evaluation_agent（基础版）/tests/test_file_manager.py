import os
from pathlib import Path

import pytest

import app.config as config
from app.file_manager import build_manifest, create_request_directory, write_markdown
from app.models import GeneratedFile


@pytest.fixture(autouse=True)
def reset_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUTS_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_build_manifest_persists_metadata():
    directory = create_request_directory("req1", "测试产品")
    doc_path = write_markdown(directory, "结果.md", "# 测试内容")
    assert doc_path.suffix == ".docx"
    files = [
        GeneratedFile(
            filename=doc_path.name,
            path=doc_path,
            download_url=f"/download/req1/{doc_path.name}",
            description="样例文档",
        )
    ]

    manifest = build_manifest("req1", "测试产品", directory, files)
    manifest_path = Path(directory) / "manifest.json"

    assert manifest_path.exists(), "manifest.json 应该被写入磁盘"
    assert manifest.product_name == "测试产品"
    assert len(manifest.files) == 1
    assert manifest.files[0].filename.endswith("结果.docx")
