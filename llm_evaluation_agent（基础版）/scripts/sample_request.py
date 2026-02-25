"""
示例脚本：调用评测方案生成 API 并打印结果。

运行前请确保 FastAPI 服务已启动（默认 http://localhost:8000），
并在 llm_evaluation_agent/.env 中配置好模型相关的密钥。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import requests

API_BASE = "http://localhost:8000"
API_TOKEN = os.getenv("API_TOKEN")


def _build_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["X-API-Key"] = API_TOKEN
    return headers


def submit_request(payload: Dict[str, Any]) -> str:
    response = requests.post(
        f"{API_BASE}/generate-plan",
        json=payload,
        headers=_build_headers(),
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    print(f"任务创建成功：request_id={data['request_id']}")
    return data["request_id"]


def poll_manifest(request_id: str, interval: float = 2.0) -> Dict[str, Any]:
    query = f"?api_key={API_TOKEN}" if API_TOKEN else ""
    manifest_url = f"{API_BASE}/manifest/{request_id}{query}"
    while True:
        response = requests.get(manifest_url, headers=_build_headers(), timeout=15)
        if response.status_code == 202:
            print("任务执行中，稍后再试…")
            time.sleep(interval)
            continue
        response.raise_for_status()
        return response.json()


def download_files(manifest: Dict[str, Any], download_dir: Path) -> List[Path]:
    download_dir.mkdir(parents=True, exist_ok=True)
    saved_files: List[Path] = []
    for file_meta in manifest.get("files", []):
        query = f"?api_key={API_TOKEN}" if API_TOKEN else ""
        url = f"{API_BASE}{file_meta['download_url']}{query}"
        filename = file_meta["filename"]
        response = requests.get(url, headers=_build_headers(), timeout=30)
        response.raise_for_status()
        target_path = download_dir / filename
        target_path.write_bytes(response.content)
        saved_files.append(target_path)
        print(f"已下载：{filename}")
    return saved_files


def main():
    sample_payload = {
        "product_name": "智能客服助手",
        "application_scenario": "面向企业售后团队的多轮问答场景",
        "target_users": "客户支持工程师、客服主管",
        "business_goals": "缩短响应时间，提升解决率与满意度",
        "core_features": [
            "融合知识库的检索问答",
            "情绪识别与应答策略建议",
            "会话记录的总结与工单同步",
        ],
        "workflow": [
            {
                "name": "提问受理",
                "input_spec": "用户自然语言问题+上下文标签",
                "output_spec": "结构化问题摘要、优先级判断",
                "notes": "需识别投诉与敏感词",
            },
            {
                "name": "答案生成",
                "input_spec": "处理后的问题、企业知识库片段",
                "output_spec": "多候选答复及置信度",
            },
        ],
        "constraints": "对话内容需符合企业合规与隐私规范",
    }

    request_id = submit_request(sample_payload)
    manifest = poll_manifest(request_id)
    print("===== Manifest =====")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))

    download_dir = Path("./sample_outputs") / request_id
    download_files(manifest, download_dir)
    print(f"全部文件已保存至：{download_dir.resolve()}")


if __name__ == "__main__":
    main()
