"""Opt-in real-provider behavior checks through production HTTP/runtime/tools.

PYTHONPATH=src uv run --offline --env-file .env python tests/clarification_real_acceptance.py --real
Uses only synthetic requests and a temporary workspace; never confirms/publishes a package.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from evaluation_agent.api import create_app
from evaluation_agent.model_config import model_from_environment
from evaluation_agent.prompts import REQUIREMENTS_CLARIFICATION_RULES


ROOT = Path(__file__).resolve().parents[1]
BRIEF = (
    "这是面向员工的 IT 知识库问答助手，输入自然语言 IT 问题，输出带来源链接的文字答案；"
    "本次只测答案质量和知识库无覆盖时的澄清或拒答，不测检索内部；"
    "用于上线验收，重点防止编造操作步骤和引用错误；"
    "以现有 IT 知识库为依据，由 IT 支持人员复核，历史问答可以抽样"
)
CASES = [
    {"id": "sparse", "name": "一句话缺少关键业务信息", "requests": ["为智能客服设计一套评测方案。"], "expect": "clarify"},
    {"id": "complete", "name": "一句话已提供足够信息", "requests": [BRIEF + "，请据此设计评测方案大纲。"], "expect": "outline"},
    {"id": "no-gold", "name": "没有样本或标准答案但复核依据明确", "requests": [
        "为面向售后坐席的回复建议助手设计版本回归评测方案：输入客户问题和售后政策，输出一段建议回复，"
        "只测回复的政策一致性和是否越权承诺；没有标准答案或现成样本，以现行售后政策为依据，"
        "由售后主管人工复核，样本和参考答案需要后续准备。请给出大纲。"], "expect": "outline"},
    {"id": "followup", "name": "追问后复用历史进入大纲", "requests": [
        "帮我给客服助手做一套评测方案。", "补充刚才同一个客服助手的信息：" + BRIEF + "。按这些信息出大纲。"], "expect": "followup"},
    {"id": "exploratory", "name": "用户授权按假设给探索性初版", "requests": [
        "现在只有“客服助手”这个方向，业务、用户、输入输出和验收依据都未定；"
        "请先按合理假设给一版探索性评测方案大纲，明确区分假设与待确认项，"
        "不要当成已确认的业务事实或上线门槛。"], "expect": "exploratory"},
    {"id": "attachment", "name": "先读取附件再判断是否需要追问", "requests": [
        "根据我上传的需求文档设计一套评测方案。"], "attachment": BRIEF, "expect": "attachment"},
    {"id": "greeting", "name": "普通介绍不启动需求问卷", "requests": ["你好，简单介绍一下你能做什么。"], "expect": "greeting"},
    {"id": "unknown", "name": "用户不知道时协助澄清而非擅自设定业务", "requests": [
        "为智能客服设计一套评测方案。", "这些我都还不太清楚，业务也没有定，我不知道该怎么选。"], "expect": "unknown"},
]


def question_items(reply):
    """Count logical numbered questions, not punctuation inside related clauses."""
    numbered = re.findall(r"(?m)^\s*(?:\*\*)?\d+[.、）)]", reply)
    bullets = re.findall(r"(?m)^\s*[-*] +[^\n]*[？?]", reply)
    return len(numbered) or len(bullets) or reply.count("？") + reply.count("?")


def send_and_wait(client, cid, text, attachment_ids):
    response = client.post(f"/api/conversations/{cid}/messages", json={
        "body": text, "client_key": uuid4().hex, "attachment_ids": attachment_ids,
    })
    if response.status_code != 200:
        raise RuntimeError("Message endpoint rejected the synthetic request")
    tid = response.json()["turn_id"]
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/conversations/{cid}").json()
        turn = next(t for t in snapshot["turns"] if t["turn_id"] == tid)
        if turn["status"] not in ("running", "cancel_requested"):
            reply = "\n".join(m["body"] for m in snapshot["messages"] if m["turn_id"] == tid and m["role"] == "assistant")
            outline_ids = {event["payload"]["outline_id"] for event in client.app.state.store.list_events(cid)
                           if event["turn_id"] == tid and event["type"] == "outline.ready"}
            outline = next((o["body"] for o in snapshot["outlines"] if o["outline_id"] in outline_ids), "")
            return {"turn_id": tid, "status": turn["status"], "scope": turn["scope_kind"],
                    "reply": reply, "outline": outline, "artifacts": len(snapshot["artifacts"]),
                    "products": len(snapshot["evaluations"]), "question_marks": reply.count("？") + reply.count("?"),
                    "question_items": question_items(reply)}
        time.sleep(.1)
    client.post(f"/api/conversations/{cid}/turns/{tid}/cancel")
    raise TimeoutError("Bounded real-model turn timed out")


def assess(case, turns, calls):
    last = turns[-1]
    checks = {"轮次正常完成": all(t["status"] == "completed" for t in turns),
              "确认前没有发布文件": all(t["artifacts"] == 0 for t in turns)}
    expected = case["expect"]
    if expected in ("clarify", "followup", "unknown"):
        first = turns[0]
        checks["缺口先追问且不进入大纲"] = first["scope"] in ("undecided", "discussion") and not first["outline"] and bool(first["reply"])
        checks["问题集中在一至三项"] = 1 <= question_items(first["reply"]) <= 3
        checks["追问简短无长问卷"] = len(first["reply"]) <= 400
    if expected in ("outline", "followup", "exploratory", "attachment"):
        checks["直接或补充后提交大纲"] = last["scope"] == "package_outline" and bool(last["outline"])
        checks["大纲保留必需章节"] = all(name in last["outline"] for name in ("评测目标", "评测指标", "评测数据样本", "评测执行方案", "产物清单"))
    if expected == "followup":
        checks["沿用同一个产品"] = last["products"] == 1
    if expected == "exploratory":
        checks["显式标注假设和待确认"] = "假设" in last["outline"] and "待确认" in last["outline"]
    if expected == "attachment":
        checks["选范围前读取需求附件"] = "read_file" in calls and "select_evaluation" in calls and calls.index("read_file") < calls.index("select_evaluation")
    if expected == "greeting":
        checks["普通介绍不生成大纲或访谈问卷"] = bool(last["reply"]) and not last["outline"] and last["scope"] in ("undecided", "discussion") and last["reply"].count("？") + last["reply"].count("?") <= 1
    if expected == "unknown":
        checks["未知信息仍先协助澄清"] = last["scope"] in ("undecided", "discussion") and not last["outline"] and 1 <= question_items(last["reply"]) <= 3 and len(last["reply"]) <= 400
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", required=True, help="Explicitly call the already configured model provider")
    parser.add_argument("--cases", nargs="*", choices=[c["id"] for c in CASES])
    parser.add_argument("--output", type=Path, default=ROOT / "docs/需求澄清验收-20261029/real-model-results.json")
    args = parser.parse_args()
    # Keep synthetic evals out of the user's tracing project; never print SDK errors or credentials.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    logging.disable(logging.CRITICAL)
    model = model_from_environment()
    if model is None:
        raise SystemExit("未配置模型，无法进行真实行为验收。")
    model.max_retries = 0
    model.request_timeout = 60
    report = {"model": model.model_name, "started_at": datetime.now(timezone.utc).isoformat(),
              "policy_sha256": hashlib.sha256(REQUIREMENTS_CLARIFICATION_RULES.encode()).hexdigest(),
              "rubric": "结构/范围/篇幅自动检查；业务缺口是否恰当、假设是否诚实需人工复核，不按问号数等同逻辑问题数",
              "scope": "真实配置模型，生产 HTTP/Deep Agents/工具，临时数据，仅合成需求，未确认或发布产物", "cases": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clarification-real-") as root:
        app = create_app(data_root=root, model=model, model_provider="openai")
        with TestClient(app) as client:
            app.state.executor.max_turn_seconds = 90
            for case in CASES:
                if args.cases and case["id"] not in args.cases:
                    continue
                print("开始：" + case["id"], flush=True)
                record = {"id": case["id"], "name": case["name"], "requests": case["requests"], "turns": []}
                try:
                    cid = client.post("/api/conversations", json={"title": case["name"]}).json()["conversation_id"]
                    attachments = []
                    if case.get("attachment"):
                        uploaded = client.post(f"/api/conversations/{cid}/attachments", data={"upload_key": uuid4().hex},
                            files={"file": ("需求.md", case["attachment"].encode(), "text/markdown")})
                        attachments = [uploaded.json()["attachment_id"]]
                        record["attachment"] = case["attachment"]
                    for index, text in enumerate(case["requests"]):
                        record["turns"].append(send_and_wait(client, cid, text, attachments if index == 0 else []))
                    saved = client.portal.call(app.state.executor.agent.aget_state, {"configurable": {"thread_id": cid}})
                    calls = [call["name"] for message in saved.values.get("messages", []) for call in getattr(message, "tool_calls", [])]
                    record["tool_calls"] = calls
                    record["checks"] = assess(case, record["turns"], calls)
                    record["status"] = "PASS" if all(record["checks"].values()) else "FAIL"
                except Exception as exc:
                    record.update(status="ERROR", error_type=type(exc).__name__)
                report["cases"].append(record)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(case["id"] + "：" + record["status"], flush=True)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if all(c["status"] == "PASS" for c in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
