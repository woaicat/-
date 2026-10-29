"""Browser regression for the conversational workbench's rendered content and stop control.

Run against a local server: python3 tests/browser_ui_smoke.py
Requires the optional system Playwright package and Chrome; API responses are synthetic.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("EVAL_AGENT_BROWSER_BASE_URL", "http://127.0.0.1:8000")
SCREENSHOTS = Path(tempfile.gettempdir()) / "evaluation-agent-v2-ui-smoke-screenshots"
NOW = datetime.now(timezone.utc)


def snapshot(conversation_id, *, turns=None, messages=None, outlines=None, artifacts=None):
    return {
        "conversation": {"conversation_id": conversation_id, "title": "界面回归示例"},
        "turns": turns or [],
        "messages": messages or [],
        "outlines": outlines or [],
        "artifacts": artifacts or [],
        "attachments": [],
        "evaluations": [{"evaluation_id": "e1", "name": "示例产品", "package_status": "completed"}],
        "deliverables": [],
        "last_event_id": 0,
        "server_now": NOW.isoformat(),
    }


def fulfill_json(route, value):
    route.fulfill(status=200, content_type="application/json", body=json.dumps(value, ensure_ascii=False))


def install_snapshot_routes(page, conversation_id, data):
    page.route(re.compile(r"/api/conversations$"), lambda route: fulfill_json(route, [{
        "conversation_id": conversation_id, "title": "界面回归示例", "updated_at": NOW.isoformat(),
    }]))
    page.route(re.compile(rf"/api/conversations/{conversation_id}$"), lambda route: fulfill_json(route, data))
    page.route(re.compile(r"/events(?:\?|$)"), lambda route: route.abort())


def browser_page(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    return page, errors


def check_send_retry(browser):
    page, errors = browser_page(browser)
    conversation_id = "ui-send"
    data = snapshot(conversation_id)
    install_snapshot_routes(page, conversation_id, data)
    keys = []

    def send(route):
        payload = json.loads(route.request.post_data)
        keys.append(payload["client_key"])
        if len(keys) == 1:
            route.abort("failed")
            return
        data["turns"].append({
            "turn_id": "t-send", "status": "completed", "scope_kind": "discussion",
            "started_at": NOW.isoformat(), "duration_ms": 1200,
            "client_key": payload["client_key"],
        })
        data["messages"].append({
            "message_id": "u-send", "turn_id": "t-send", "role": "user",
            "body": payload["body"], "status": "completed",
        })
        fulfill_json(route, {"turn_id": "t-send", "trigger_message_id": "u-send"})

    page.route(re.compile(rf"/api/conversations/{conversation_id}/messages$"), send)
    page.goto(BASE_URL, wait_until="domcontentloaded")
    assert "?v=" in page.locator('script[src*="/assets/app.js"]').get_attribute("src")
    message = page.locator("#messageInput")
    message.fill("你能干啥")
    page.locator("#sendButton").click()
    page.locator("#toast").wait_for(state="visible")
    assert message.is_enabled() and message.input_value() == "你能干啥"
    page.locator("#sendButton").click()
    page.locator(".message.user .text").wait_for()
    assert page.locator(".message.user .text").inner_text() == "你能干啥"
    assert message.is_enabled() and message.input_value() == ""
    assert len(keys) == 2 and keys[0] == keys[1]
    assert not errors, errors
    page.close()


def check_conversation_isolation(browser):
    page, errors = browser_page(browser)
    first, second = "ui-chat-a", "ui-chat-b"
    chats = [
        {"conversation_id": first, "title": "对话 A", "updated_at": NOW.isoformat()},
        {"conversation_id": second, "title": "对话 B", "updated_at": NOW.isoformat()},
    ]
    data = {first: snapshot(first), second: snapshot(second)}
    data[first]["conversation"]["title"] = "对话 A"
    data[second]["conversation"]["title"] = "对话 B"
    delayed = []
    hold_second = False
    page.route(re.compile(r"/api/conversations$"), lambda route: fulfill_json(route, chats))

    def get_chat(route):
        conversation_id = route.request.url.split("?")[0].rsplit("/", 1)[-1]
        if conversation_id == second and hold_second:
            delayed.append(route)
        else:
            fulfill_json(route, data[conversation_id])

    page.route(re.compile(r"/api/conversations/ui-chat-[ab]$"), get_chat)
    page.route(re.compile(r"/events(?:\?|$)"), lambda route: route.abort())

    def send(route):
        payload = json.loads(route.request.post_data)
        assert route.request.url.endswith(f"/{second}/messages")
        data[second]["turns"].append({
            "turn_id": "t-b", "status": "completed", "scope_kind": "discussion",
            "started_at": NOW.isoformat(), "duration_ms": 800,
            "client_key": payload["client_key"],
        })
        data[second]["messages"].append({
            "message_id": "u-b", "turn_id": "t-b", "role": "user",
            "body": payload["body"], "status": "completed",
        })
        fulfill_json(route, {"turn_id": "t-b", "trigger_message_id": "u-b"})

    page.route(re.compile(r"/api/conversations/ui-chat-b/messages$"), send)
    page.goto(f"{BASE_URL}/?conversation={first}", wait_until="domcontentloaded")
    page.get_by_role("heading", name="对话 A").wait_for()
    message = page.locator("#messageInput")
    message.fill("只属于 A 的草稿")
    page.locator("#history button").filter(has_text="对话 B").click()
    page.get_by_role("heading", name="对话 B").wait_for()
    assert message.input_value() == ""
    message.fill("只属于 B 的消息")
    page.locator("#history button").filter(has_text="对话 A").click()
    page.get_by_role("heading", name="对话 A").wait_for()
    assert message.input_value() == "只属于 A 的草稿"
    page.locator("#history button").filter(has_text="对话 B").click()
    page.get_by_role("heading", name="对话 B").wait_for()
    assert message.input_value() == "只属于 B 的消息"
    page.locator("#sendButton").click()
    page.locator(".message.user .text").wait_for()
    assert data[first]["messages"] == []
    page.locator("#history button").filter(has_text="对话 A").click()
    page.get_by_role("heading", name="对话 A").wait_for()
    assert message.input_value() == "只属于 A 的草稿"
    assert page.locator(".message.user").count() == 0

    hold_second = True
    page.locator("#history button").filter(has_text="对话 B").click()
    page.wait_for_function('new URL(location.href).searchParams.get("conversation") === "ui-chat-b"')
    page.wait_for_timeout(100)
    assert delayed
    page.locator("#history button").filter(has_text="对话 A").click()
    page.get_by_role("heading", name="对话 A").wait_for()
    delayed.pop().fulfill(status=200, content_type="application/json", body=json.dumps(data[second]))
    page.wait_for_timeout(100)
    assert page.locator("#conversationTitle").inner_text() == "对话 A"
    assert message.input_value() == "只属于 A 的草稿"
    assert page.locator(".message.user").count() == 0
    assert not errors, errors
    page.close()


def check_outline(browser):
    page, errors = browser_page(browser)
    conversation_id = "ui-outline"
    outline = {
        "outline_id": "o1", "evaluation_id": "e1", "revision": 1, "status": "pending",
        "body": "## 评测方案大纲\n\n| 维度 | 样本量 |\n|---|---:|\n| 准确性 | 40 |",
    }
    turn = {
        "turn_id": "t1", "status": "completed", "scope_kind": "package_outline",
        "started_at": NOW.isoformat(), "duration_ms": 4200,
    }
    data = snapshot(conversation_id, turns=[turn], outlines=[outline], messages=[{
        "message_id": "a1", "turn_id": "t1", "role": "assistant", "body":
        "## 先看大纲\n\n| 阶段 | 工作 |\n|---|---|\n| 1 | **分析需求** |",
        "artifact_ids": [],
    }])
    install_snapshot_routes(page, conversation_id, data)
    captured = {}

    def save_outline(route):
        captured["edited"] = json.loads(route.request.post_data)["body"]
        outline["body"] = captured["edited"]
        outline["revision"] = 2
        fulfill_json(route, {"revision": 2})

    def confirm_outline(route):
        captured["confirmed"] = json.loads(route.request.post_data)["expected_revision"]
        outline["status"] = "confirmed"
        fulfill_json(route, {"turn_id": "t2"})

    page.route(re.compile(r"/outlines/o1$"), save_outline)
    page.route(re.compile(r"/outlines/o1/confirm$"), confirm_outline)
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator(".outline-preview table").wait_for()
    assert page.locator(".message.assistant .markdown-content table").count() == 1
    assert page.locator(".outline-textarea").is_hidden()
    assert "|---|" not in page.locator(".outline-preview").inner_text()
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / "ui-outline-rendered.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("button", name="编辑大纲").click()
    editor = page.get_by_role("textbox", name="可编辑的评测方案大纲")
    assert editor.is_visible()
    editor.fill("## 已修改的大纲\n\n| 项目 | 内容 |\n|---|---|\n| 样本 | 50 条 |")
    page.get_by_role("button", name="预览大纲").click()
    assert page.locator(".outline-preview h2").inner_text() == "已修改的大纲"
    assert page.locator(".outline-preview table td").last.inner_text() == "50 条"
    page.get_by_role("button", name=re.compile("确认并开始")).click()
    page.locator(".outline-card.is-confirmed").wait_for()
    assert page.locator(".outline-preview h2").inner_text() == "已修改的大纲"
    assert page.get_by_role("button", name=re.compile("确认并开始")).count() == 0
    assert captured == {"edited": "## 已修改的大纲\n\n| 项目 | 内容 |\n|---|---|\n| 样本 | 50 条 |", "confirmed": 2}
    assert not errors, errors
    page.close()


def check_running(browser):
    page, errors = browser_page(browser)
    conversation_id = "ui-running"
    turn = {
        "turn_id": "t2", "status": "running", "scope_kind": "package_outline",
        "started_at": (NOW - timedelta(seconds=2)).isoformat(), "duration_ms": None,
    }
    data = snapshot(conversation_id, turns=[turn], messages=[{
        "message_id": "u2", "role": "user", "body": "请先分析产品需求", "turn_id": None,
    }])
    install_snapshot_routes(page, conversation_id, data)
    captured = {}

    def cancel(route):
        captured["cancelled"] = True
        turn["status"] = "cancelled"
        turn["duration_ms"] = 2200
        fulfill_json(route, {"status": "cancelled"})

    page.route(re.compile(r"/turns/t2/cancel$"), cancel)
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator(".message.waiting .processing-activity").wait_for()
    first_phase = page.locator(".processing-label").inner_text()
    page.wait_for_function("previous => document.querySelector('.processing-label')?.textContent !== previous", arg=first_phase, timeout=6000)
    assert page.locator(".run-card, .composer-note").count() == 0
    assert page.get_by_role("textbox", name="发送给评测智能体的消息").is_disabled()
    assert page.get_by_role("button", name="上传文件").is_disabled()
    stop = page.get_by_role("button", name="终止任务")
    assert stop.is_enabled()
    assert stop.evaluate("button => getComputedStyle(button, '::before').width") == "11px"
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / "ui-running-compact.png"))
    stop.click()
    page.get_by_role("button", name="发送消息").wait_for()
    assert page.get_by_role("textbox", name="发送给评测智能体的消息").is_enabled()
    assert captured.get("cancelled") is True
    assert not errors, errors
    page.close()


def check_stream_and_previews(browser):
    page, errors = browser_page(browser)
    conversation_id = "ui-previews"
    turn = {
        "turn_id": "t3", "status": "running", "scope_kind": "discussion",
        "started_at": NOW.isoformat(), "duration_ms": None,
    }
    md_url = f"/api/conversations/{conversation_id}/artifacts/md1/preview"
    csv_url = f"/api/conversations/{conversation_id}/artifacts/csv1/preview"
    artifacts = [
        {"artifact_id": "md1", "evaluation_id": "e1", "kind": "md", "filename": "方案.md", "content_revision": 1, "preview_url": md_url},
        {"artifact_id": "csv1", "evaluation_id": "e1", "kind": "csv", "filename": "样本.csv", "content_revision": 1, "preview_url": csv_url},
    ]
    assistant = {"message_id": "a3", "turn_id": "t3", "role": "assistant", "body": "", "artifact_ids": []}
    data = snapshot(conversation_id, turns=[turn], messages=[assistant], artifacts=artifacts)
    install_snapshot_routes(page, conversation_id, data)
    delta = "## 流式标题\n\n**1. 设计评测方案**\n- 明确评测目标\n\n| 项目 | 结果 |\n|---|---|\n| 加粗 | **通过** |"
    delivered = False

    def sse(route):
        nonlocal delivered
        if delivered:
            route.abort()
            return
        delivered = True
        event = {"event_id": 1, "type": "message.delta", "payload": {"message_id": "a3", "delta": delta}}
        route.fulfill(status=200, content_type="text/event-stream", body=f"event: message.delta\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")

    page.route(re.compile(r"/api/conversations/ui-previews/events(?:\?|$)"), sse)
    page.route(re.compile(r"/artifacts/md1/preview$"), lambda route: route.fulfill(
        status=200, content_type="text/plain; charset=utf-8",
        body="# 文件预览\n\n| 指标 | 值 |\n|---|---|\n| 准确率 | 90% |\n\n<img src=x onerror=alert(1)>",
    ))
    page.route(re.compile(r"/artifacts/csv1/preview$"), lambda route: route.fulfill(
        status=200, content_type="text/plain; charset=utf-8",
        body='id,问题,备注\n1,"电压,多少","首行\n次行"\n2,价格,"<script>alert(1)</script>"\n',
    ))
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator(".message.assistant .markdown-content table").wait_for()
    assert page.locator(".message.assistant .markdown-content h2").inner_text() == "流式标题"
    assert page.locator(".message.assistant .markdown-content strong").first.inner_text() == "1. 设计评测方案"
    assert page.locator(".message.assistant .markdown-content strong").last.inner_text() == "通过"
    assert "**1." not in page.locator(".message.assistant .markdown-content").inner_text()
    assert page.locator(".message.assistant .processing-activity").count() == 1
    page.get_by_role("button", name=re.compile("产物")).click()
    page.locator(".artifact-row").filter(has_text="方案.md").click()
    page.locator(".preview-page .markdown-content table").wait_for()
    assert page.locator(".preview-page .markdown-content h1").inner_text() == "文件预览"
    assert page.locator(".preview-page img").count() == 0
    page.locator("#closePreview").click()
    page.locator(".artifact-row").filter(has_text="样本.csv").click()
    page.locator(".preview-page .csv-table").wait_for()
    assert page.locator(".csv-table th").count() == 3
    assert page.locator(".csv-table tbody tr").count() == 2
    assert page.locator(".csv-table tbody tr").first.locator("td").nth(1).inner_text() == "电压,多少"
    assert page.locator(".csv-table tbody tr").first.locator("td").nth(2).inner_text() == "首行\n次行"
    assert page.locator(".preview-page script").count() == 0
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / "ui-csv-rendered.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors, errors
    page.close()


if __name__ == "__main__":
    with sync_playwright() as playwright:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        launch = {"executable_path": str(chrome)} if chrome.exists() else {}
        browser = playwright.chromium.launch(headless=True, **launch)
        try:
            check_send_retry(browser)
            check_conversation_isolation(browser)
            check_outline(browser)
            check_running(browser)
            check_stream_and_previews(browser)
        finally:
            browser.close()
    print("Browser UI smoke passed: conversation isolation and rapid switching, send retry, rendered Markdown/CSV, outline edit/confirm, compact running state, stop control, streaming, and sanitization.")
