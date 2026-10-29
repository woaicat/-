"""Isolated browser acceptance for cancellation, continuation and crash recovery.

Run: python3 tests/special_acceptance.py
The test server uses a temporary data directory and a random local port.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from functional_browser_acceptance import ROOT, REPORT, free_port, new_page, request, server, shot


RESULTS = []


def record(case_id, title, expected, actual, screenshot):
    category = ("真实浏览器＋真实 HTTP＋真实模型"
                if case_id in {"SP-01", "SP-02A", "SP-02B"}
                else "真实浏览器＋真实 HTTP＋可控智能体")
    RESULTS.append({
        "id": case_id, "category": category,
        "title": title, "expected": expected, "actual": actual,
        "screenshot": screenshot, "status": "PASS",
    })
    print(f"{case_id} PASS: {actual}", flush=True)


def cancel_while_generating_and_continue(browser):
    with server("cancel-continue") as base:
        fixture = request(base, "/__test/seed-pending", method="POST")
        cid = fixture["conversation_id"]
        page, errors = new_page(browser)
        try:
            page.goto(f"{base}/?conversation={cid}", wait_until="domcontentloaded")
            page.get_by_role("button", name="确认并开始 →").click()
            page.wait_for_function(
                "document.querySelector('#artifactCount')?.textContent === '1' && "
                "document.querySelector('#topStatus')?.textContent === '运行中'",
                timeout=30000,
            )
            running = request(base, f"/api/conversations/{cid}")
            assert running["turns"][-1]["status"] == "running"
            assert [item["status"] for item in running["deliverables"]] == ["ready", "pending"]
            first_id = running["artifacts"][0]["artifact_id"]
            page.get_by_role("button", name="终止任务", exact=True).click()
            page.wait_for_function("document.querySelector('#topStatus')?.textContent === '已终止'", timeout=15000)
            cancelled = request(base, f"/api/conversations/{cid}")
            assert cancelled["turns"][-1]["status"] == "cancelled"
            assert cancelled["evaluations"][0]["package_status"] == "incomplete"
            assert [item["status"] for item in cancelled["deliverables"]] == ["ready", "pending"]
            assert page.locator("#messageInput").is_enabled()
            page.locator("#toggleDrawer").click()
            assert "未完成的任务产物" in page.locator(".folder-state").inner_text()
            page.locator(".artifact-row").click()
            page.locator(".preview-page").wait_for()
            assert "第一份文件已完成" in page.locator(".preview-page").inner_text()
            record("SP-03A", "首个文件发布后从浏览器终止整包",
                   "取消只终止本轮，已就绪文件保留且可预览，待办文件不误标完成",
                   "运行中点击停止后状态 cancelled；1 ready/1 pending，已有文件可预览",
                   shot(page, "24-real-click-cancel-with-first-file.png"))

            page.locator("#closePreview").click()
            page.locator("#closeDrawer").click()
            page.locator("#messageInput").fill("接着已有文件完成剩余工作，只补样本.csv。")
            page.locator("#sendButton").click()
            page.wait_for_function(
                "document.querySelector('#topStatus')?.textContent === '本轮已完成' && "
                "document.querySelectorAll('.deliverable-row').length === 2",
                timeout=30000,
            )
            final = request(base, f"/api/conversations/{cid}")
            assert final["turns"][-1]["status"] == "completed"
            assert final["turns"][-1]["scope_kind"] == "package_continue"
            assert final["evaluations"][0]["package_status"] == "completed"
            assert [item["status"] for item in final["deliverables"]] == ["ready", "ready"]
            assert len(final["artifacts"]) == 2
            assert final["artifacts"][0]["artifact_id"] == first_id
            assert final["artifacts"][0]["content_revision"] == 1
            assert set(final["messages"][-1]["artifact_ids"]) == {item["artifact_id"] for item in final["artifacts"]}
            assert not errors, errors
            record("SP-03B", "终止后在同一浏览器会话续做剩余文件",
                   "只补待办文件；旧文件身份与版本不变，整包恢复完成并列出全部产物",
                   "续做轮次完成；原文件仍为第 1 版，样本.csv 新增，回复可点两个文件",
                   shot(page, "25-real-click-cancel-then-continue.png"))
        finally:
            page.close()


def start_server(directory: Path, port: int):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT / "tests")
    log = (directory / "crash-server.log").open("a")
    process = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "tests/functional_server.py",
         "--data-root", str(directory), "--port", str(port), "--mode", "continue"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.close()
            raise RuntimeError(f"Test server exited with {process.returncode}")
        try:
            request(base, "/")
            return process, log, base
        except OSError:
            time.sleep(0.1)
    process.kill()
    process.wait()
    log.close()
    raise TimeoutError("Crash test server did not start")


def crash_and_recover(browser):
    with tempfile.TemporaryDirectory(prefix="eval-v2-crash-") as directory_name:
        directory = Path(directory_name)
        port = free_port()
        process, log, base = start_server(directory, port)
        page, errors = new_page(browser)
        try:
            fixture = request(base, "/__test/seed-running", method="POST")
            cid = fixture["conversation_id"]
            page.goto(f"{base}/?conversation={cid}", wait_until="domcontentloaded")
            page.wait_for_function("document.querySelector('#topStatus')?.textContent === '运行中'")
            assert page.locator("#messageInput").is_disabled()
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=10)
            log.close()
            process, log, base = start_server(directory, port)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("document.querySelector('#topStatus')?.textContent === '未完成'")
            recovered = request(base, f"/api/conversations/{cid}")
            assert recovered["turns"][-1]["status"] == "interrupted"
            assert recovered["evaluations"][0]["package_status"] == "incomplete"
            assert [item["status"] for item in recovered["deliverables"]] == ["ready", "pending"]
            assert recovered["artifacts"][0]["artifact_id"] == fixture["first_artifact_id"]
            assert page.locator("#messageInput").is_enabled()
            page.locator("#toggleDrawer").click()
            page.locator(".artifact-row").click()
            page.locator(".preview-page").wait_for()
            assert "已完成的设计思路" in page.locator(".preview-page").inner_text()
            assert "未完成的任务产物" in page.locator(".folder-state").inner_text()
            assert not errors, errors
            record("SP-04", "服务进程被强制结束后的浏览器恢复",
                   "重启后运行中轮次变 interrupted；已完成文件可预览，待办仍待办，输入恢复",
                   "SIGKILL 后同数据目录重启，页面显示未完成、1 ready/1 pending，文件预览和输入可用",
                   shot(page, "26-process-crash-recovered.png"))
        finally:
            page.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            log.close()


def sse_reconnect_without_reload(browser):
    with server("chat") as base:
        page, errors = new_page(browser)
        requests = []

        def intercept_stream(route):
            requests.append(route.request.url)
            if len(requests) == 1:
                route.fulfill(status=200, content_type="text/event-stream", body="")
            else:
                route.continue_()

        page.route("**/events?*", intercept_stream)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#messageInput").wait_for(state="visible")
            page.wait_for_function("document.querySelector('#messageInput')?.disabled === false")
            deadline = time.monotonic() + 10
            while not requests and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert requests, "Initial EventSource request was not made"
            page.locator("#messageInput").fill("请测试连接中断后的消息恢复")
            page.locator("#sendButton").click()
            page.wait_for_function(
                "document.querySelector('#topStatus')?.textContent === '本轮已完成' && "
                "document.querySelector('.message.assistant strong')?.textContent === 'Markdown 已渲染'",
                timeout=20000,
            )
            cid = page.url.split("conversation=")[-1]
            snapshot = request(base, f"/api/conversations/{cid}")
            assert snapshot["turns"][-1]["status"] == "completed"
            assert len(requests) >= 2, requests
            assert page.locator(".message.assistant table").count() == 1
            assert not errors, errors
            record("SP-02C", "事件流断开后原页面自动重连并补齐消息",
                   "首次 SSE 连接关闭后自动重新请求，无需刷新；错过的消息最终完整显示",
                   f"首个 SSE 响应被测试层关闭，浏览器共发起 {len(requests)} 次连接；"
                   "最终回复和表格完整显示",
                   shot(page, "31-sse-reconnect-without-reload.png"))
        finally:
            page.close()


def wait_turn(base, conversation_id, turn_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = request(base, f"/api/conversations/{conversation_id}")
        turn = next(item for item in snapshot["turns"] if item["turn_id"] == turn_id)
        if turn["status"] not in ("running", "cancel_requested"):
            return turn, snapshot
        time.sleep(0.4)
    raise TimeoutError(f"Turn did not finish in {timeout} seconds")


def real_model_context_compaction(browser):
    with server("real-small") as base:
        token_sample = request(base, "/__test/token-sample")
        assert isinstance(token_sample["provider_input_tokens"], int)
        assert token_sample["provider_input_tokens"] > 0
        cid = request(base, "/api/conversations", method="POST", body={
            "title": "真实模型上下文压缩测试",
        })["conversation_id"]

        def send(body):
            started = request(base, f"/api/conversations/{cid}/messages", method="POST", body={
                "body": body, "client_key": str(uuid.uuid4()),
            })
            return wait_turn(base, cid, started["turn_id"], timeout=120)

        turn, _ = send("请记住测试项目代号是松果-47。只回复已记住，不创建文件。")
        assert turn["status"] == "completed"
        compressed = None
        for _ in range(6):
            turn, _ = send("以下是一般背景，仅回复收到，不创建文件：" +
                           "准确引用、拒答边界、样本覆盖与可复核执行。" * 45)
            assert turn["status"] == "completed", turn["status"]
            compressed = request(base, f"/__test/context/{cid}")
            if compressed["summary_count"]:
                break
        assert compressed and compressed["summary_count"] >= 1, compressed
        turn, snapshot = send("请只回答我最早告诉你的测试项目代号是什么？")
        assert turn["status"] == "completed"
        assert "松果-47" in snapshot["messages"][-1]["body"]
        page, errors = new_page(browser)
        try:
            page.goto(f"{base}/?conversation={cid}", wait_until="domcontentloaded")
            page.wait_for_function("""() => [...document.querySelectorAll('.message.assistant .text')]
                .some(node => node.textContent.includes('松果-47'))""")
            assert not errors, errors
            record("SP-01", "真实模型触发压缩后找回早期信息",
                   "在缩小的测试窗口达到压缩阈值后生成摘要；后续轮次完成，早期代号仍可找回",
                   f"测试窗口 {compressed['window_tokens']} tokens、触发值 {compressed['summarize_at_tokens']}；"
                   f"摘要文件 {compressed['summary_count']} 个，真实模型答对松果-47；"
                   f"同一直接请求估算 {token_sample['approx_input_tokens']}、供应商报告输入 "
                   f"{token_sample['provider_input_tokens']} tokens。生产 128k 阈值未实际填满",
                   shot(page, "27-real-model-context-compaction.png"))
        finally:
            page.close()


def real_model_cancel_and_reconnect(browser):
    with server("real") as base:
        page, errors = new_page(browser)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#history button").first.wait_for()
            cid = page.url.split("conversation=")[-1]
            page.locator("#messageInput").fill(
                "这是普通交流，不创建文件。请用纯文本编号列出 300 条不同的评测样本设计注意事项，"
                "每条至少 30 个汉字，逐条输出。"
            )
            page.locator("#sendButton").click()
            page.wait_for_function(
                "document.querySelector('#topStatus')?.textContent === '运行中' && "
                "(document.querySelector('.message.assistant .text')?.textContent?.length || 0) > 20",
                timeout=90000,
            )
            started = time.monotonic()
            page.get_by_role("button", name="终止任务", exact=True).click()
            page.wait_for_function("document.querySelector('#topStatus')?.textContent === '已终止'", timeout=15000)
            elapsed = round(time.monotonic() - started, 2)
            snapshot = request(base, f"/api/conversations/{cid}")
            assert snapshot["turns"][-1]["status"] == "cancelled"
            before = snapshot["messages"][-1]["body"]
            page.wait_for_timeout(2000)
            after = request(base, f"/api/conversations/{cid}")
            assert after["messages"][-1]["body"] == before
            assert page.locator("#messageInput").is_enabled()
            record("SP-02A", "真实模型流式输出途中手动终止",
                   "远端请求开始后停止可在 15 秒内落到 cancelled，后续不追加文本，输入恢复",
                   f"收到真实模型增量后点击停止，{elapsed} 秒终止；2 秒后无迟到文本",
                   shot(page, "28-real-model-cancelled.png"))

            page.locator("#newChat").click()
            page.wait_for_function("previous => new URL(location.href).searchParams.get('conversation') !== previous", arg=cid)
            page.wait_for_function("document.querySelector('#messageInput')?.disabled === false")
            next_cid = page.url.split("conversation=")[-1]
            page.locator("#messageInput").fill(
                "普通交流，不创建文件。请用纯文本编号列出 120 条互不重复的测试样本质量检查点，"
                "每条不少于 25 个汉字，逐条输出。"
            )
            page.locator("#sendButton").click()
            page.wait_for_function(
                "document.querySelector('#topStatus')?.textContent === '运行中' && "
                "(document.querySelector('.message.assistant .text')?.textContent?.length || 0) > 20",
                timeout=90000,
            )
            running = request(base, f"/api/conversations/{next_cid}")
            running_turn_id = running["turns"][-1]["turn_id"]
            page.context.set_offline(True)
            offline_probe = page.evaluate("""async () => {
                try { await fetch('/api/conversations'); return 'reachable'; }
                catch { return 'blocked'; }
            }""")
            assert offline_probe == "blocked"
            page.wait_for_timeout(2500)
            shot(page, "29-browser-offline-during-model-stream.png")
            page.context.set_offline(False)
            page.reload(wait_until="domcontentloaded")
            turn, final = wait_turn(base, next_cid, running_turn_id, timeout=120)
            assert turn["status"] == "completed", (turn["status"], final["messages"][-1]["body"][:200])
            page.wait_for_function(
                "document.querySelector('#topStatus')?.textContent === '本轮已完成' && "
                "document.querySelector('.message.assistant .text')?.textContent?.length > 300",
                timeout=30000,
            )
            body = final["messages"][-1]["body"]
            visible = page.locator(".message.assistant .text").last.inner_text()
            assert len(body) > 300 and len(visible) > 300
            normalize = lambda value: re.sub(r"[^\w\u4e00-\u9fff]", "", value)
            assert normalize(body)[-24:] in normalize(visible)
            assert page.locator("#messageInput").is_enabled()
            assert not errors, errors
            record("SP-02B", "真实模型回复期间浏览器断网并刷新恢复",
                   "离线时新请求被阻断；恢复网络并刷新后能读取完整回复、完成状态，输入恢复",
                   f"离线新请求被阻断；恢复网络并刷新后页面显示 {len(visible)} 字，"
                   f"服务端保存 {len(body)} 字。自动 SSE 重连不由此用例证明",
                   shot(page, "30-browser-reconnected-final-reply.png"))
        finally:
            page.context.set_offline(False)
            page.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="Run configured real-model checks")
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        browser = playwright.chromium.launch(
            headless=True, **({"executable_path": str(chrome)} if chrome.exists() else {}),
        )
        try:
            if args.real:
                real_model_context_compaction(browser)
                real_model_cancel_and_reconnect(browser)
            else:
                cancel_while_generating_and_continue(browser)
                crash_and_recover(browser)
                sse_reconnect_without_reload(browser)
        finally:
            browser.close()
    destination = REPORT / ("special-real-results.json" if args.real else "special-results.json")
    destination.write_text(json.dumps({
        "run_at": datetime.now(timezone.utc).isoformat(), "cases": RESULTS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results: {destination}")


if __name__ == "__main__":
    main()
