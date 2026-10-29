"""Submit one synthetic trace without accessing the model or business data."""

import sys
from datetime import datetime, timezone
from uuid import uuid4

from langsmith import Client
from urllib3.util import Retry

from evaluation_agent.observability import configure_observability, tracing_enabled


def main() -> int:
    try:
        configure_observability()
        if not tracing_enabled():
            print("尚未开启：请将 .env 中 LANGSMITH_TRACING 改为 true。")
            return 1
        run_id = uuid4()
        client = Client(auto_batch_tracing=False, timeout_ms=10_000, retry_config=Retry(total=0))
        try:
            client.create_run(
                name="LangSmith 连接测试", run_type="chain", id=run_id,
                inputs={"message": "这是一条固定文本的连接测试，不包含业务资料。"},
                tags=["evaluation-agent-v2", "connection-check"],
                extra={"metadata": {"source": "check_langsmith", "synthetic": True}},
            )
            client.update_run(run_id, outputs={"status": "connected"}, end_time=datetime.now(timezone.utc))
        finally:
            client.close()
        print(f"测试记录已提交，ID：{run_id}")
        print("请在 LangSmith 的 evaluation-agent-v2 → Local → Tracing 查看‘LangSmith 连接测试’。")
        return 0
    except Exception as exc:
        # SDK exception text may include server details; never echo credentials.
        print(f"连接检查失败（{type(exc).__name__}）。请检查密钥、工作区、Agent ID 和区域配置。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
