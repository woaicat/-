"""Rename over real HTTP plus deterministic cancellation/SSE race regression."""

import json
import re
import time

from playwright.sync_api import expect, sync_playwright

from browser_ui_smoke import NOW, fulfill_json, snapshot
from functional_browser_acceptance import ROOT, new_page, request, server
CHINESE = ["00_方案总览.md", "01_评测指标.md", "02_样本设计.md", "03_评测方法.md", "04_执行手册.md", "05_评分表.csv", "06_报告模板.docx"]


REPORT = ROOT / "docs" / "改名与终止同步验收-20261027"
RESULTS = []


def shot(page, name):
    folder = REPORT / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / name), full_page=True)


def rename_through_agent(browser):
    with server("rename") as base:
        cid = request(base, "/__test/seed-rename", method="POST")["conversation_id"]
        before = request(base, f"/api/conversations/{cid}")
        page, errors = new_page(browser)
        page.goto(f"{base}/?conversation={cid}")
        expect(page.locator("#messageInput")).to_be_enabled()
        page.locator("#messageInput").fill("把文件名都改成中文吧")
        page.locator("#sendButton").click()
        expect(page.locator(".deliverable-row")).to_have_count(7, timeout=10000)
        expect(page.locator("#messageInput")).to_be_enabled(timeout=10000)
        after = request(base, f"/api/conversations/{cid}")
        assert [item["filename"] for item in after["artifacts"]] == CHINESE
        assert [item["content_revision"] for item in after["artifacts"]] == [1] * 7
        assert [item["artifact_id"] for item in before["artifacts"]] == [item["artifact_id"] for item in after["artifacts"]]
        assert len(after["artifact_aliases"]) == 7
        page.locator(".delivery-details summary").click()
        page.locator(".deliverable-row").filter(has_text="00_方案总览.md").click()
        expect(page.locator("#previewTitle")).to_have_text("00_方案总览.md")
        expect(page.locator("#preview .markdown-content")).to_contain_text("保持原始正文")
        page.get_by_role("link", name="指标", exact=True).click()  # Original 01_metrics.md link.
        expect(page.locator("#previewTitle")).to_have_text("01_评测指标.md")
        with page.expect_download() as download:
            page.locator("#previewDownload").click()
        assert download.value.suggested_filename == "01_评测指标.md"
        page.keyboard.press("Escape")
        page.locator("#toggleDrawer").click()
        expect(page.locator(".artifact-row")).to_have_count(7)
        shot(page, "01-seven-renamed-files.png")
        page.reload()
        expect(page.locator(".deliverable-row")).to_have_count(7)
        page.locator(".delivery-details summary").click()
        assert "00_方案总览.md" in page.locator("#thread").inner_text()
        assert not errors, errors
        page.close()
        RESULTS.append("真实 HTTP/Deep Agents：一次批量改名 7 个文件，版本/ID 不变，刷新、旧名链接与下载正常")


FAKE_SSE = """
window.__streams = [];
window.EventSource = class {
  constructor(url) { this.url=url; this.listeners={}; window.__streams.push(this); }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  close() { this.closed=true; }
};
window.__emit = (type, id) => {
  const source=window.__streams.at(-1);
  for (const handler of source.listeners[type] || []) handler({data:JSON.stringify({
    type, event_id:id, payload:{turn_id:'sync-turn'}
  })});
};
"""


def sync_fixture(browser, base, *, fast_poll=False):
    page, errors = new_page(browser)
    page.add_init_script(FAKE_SSE)
    if fast_poll:
        page.add_init_script("const originalTimeout=window.setTimeout.bind(window); window.setTimeout=(fn,ms,...args)=>originalTimeout(fn,ms===1000?10:ms,...args)")
    cid = "sync-conversation"
    turn = {"turn_id": "sync-turn", "status": "running", "scope_kind": "discussion",
            "started_at": NOW.isoformat(), "duration_ms": None}
    data = snapshot(cid, turns=[turn])
    chats = [{"conversation_id": cid, "title": "终止同步验收", "updated_at": NOW.isoformat()}]
    page.route(re.compile(r"/api/conversations$"), lambda route: fulfill_json(route, chats))
    state = {"fail": False, "reads": 0}

    def read(route):
        state["reads"] += 1
        if state["fail"]:
            route.fulfill(status=503, content_type="application/json", body='{"detail":"临时断网"}')
        else:
            fulfill_json(route, data)

    page.route(re.compile(rf"/api/conversations/{cid}$"), read)
    page.goto(base)
    expect(page.locator("#messageInput")).to_be_disabled()
    expect(page.locator("#sendButton")).to_have_attribute("aria-label", "终止任务")
    page.wait_for_function("window.__streams.length === 1")
    return page, errors, data, chats, state


def sidebar_terminal_race(browser):
    with server("chat") as base:
        page, errors, data, chats, state = sync_fixture(browser, base)
        held = []
        page.route(re.compile(r"/api/conversations$"), lambda route: held.append(route))
        data["turns"][0]["status"] = "cancel_requested"
        data["last_event_id"] = 1
        with page.expect_request(lambda req: req.url == base + "/api/conversations"):
            page.evaluate("__emit('turn.cancel_requested',1)")
        deadline = time.monotonic() + 2
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert held
        data["turns"][0]["status"] = "cancelled"
        data["turns"][0]["duration_ms"] = 1000
        data["last_event_id"] = 2
        page.evaluate("__emit('turn.cancelled',2)")
        expect(page.locator("#messageInput")).to_be_enabled(timeout=3000)
        expect(page.locator(".notice")).to_contain_text("本轮已终止")
        shot(page, "02-terminal-during-sidebar-refresh.png")
        page.unroute(re.compile(r"/api/conversations$"))
        for route in held:
            fulfill_json(route, chats)
        assert not errors, errors
        page.close()
        RESULTS.append("受控竞态：侧栏请求仍挂起时收到终止完成事件，事件被消费且输入恢复")


def cancel_poll_and_failure_retry(browser):
    with server("chat") as base:
        page, errors, data, chats, state = sync_fixture(browser, base)

        def cancel(route):
            data["turns"][0]["status"] = "cancel_requested"
            fulfill_json(route, {"turn_id": "sync-turn", "status": "cancel_requested"})

        page.route(re.compile(r"/turns/sync-turn/cancel$"), cancel)
        page.locator("#sendButton").click()
        expect(page.locator("#sendButton")).to_be_disabled()
        data["turns"][0]["status"] = "cancelled"
        data["turns"][0]["duration_ms"] = 2000
        # No terminal SSE: the bounded status poll must recover the page.
        expect(page.locator("#messageInput")).to_be_enabled(timeout=4000)
        assert not errors, errors
        page.close()
        RESULTS.append("不发送终态 SSE：点击终止后主动查询和短轮询仍能确认终态并恢复输入")

        page, errors, data, chats, state = sync_fixture(browser, base)

        def cancel_with_lost_sync(route):
            data["turns"][0]["status"] = "cancelled"
            data["turns"][0]["duration_ms"] = 2000
            state["fail"] = True
            fulfill_json(route, {"turn_id": "sync-turn", "status": "cancel_requested"})

        page.route(re.compile(r"/turns/sync-turn/cancel$"), cancel_with_lost_sync)
        page.locator("#sendButton").click()
        expect(page.locator("#statusSync")).to_contain_text("状态同步失败")
        expect(page.locator("#messageInput")).to_be_disabled()
        shot(page, "03-sync-failed-and-retry.png")
        state["fail"] = False
        page.locator("#retryStatusSync").click()
        expect(page.locator("#messageInput")).to_be_enabled()
        expect(page.locator("#statusSync")).to_be_hidden()
        assert not errors, errors
        page.close()
        RESULTS.append("临时断网：显示同步失败及重试按钮，未确认前保持锁定，重试确认后恢复")


def reconnect_and_foreground(browser):
    with server("chat") as base:
        for action in ("reconnect", "foreground"):
            page, errors, data, chats, state = sync_fixture(browser, base)
            data["turns"][0]["status"] = "cancelled"
            data["turns"][0]["duration_ms"] = 1000
            if action == "reconnect":
                page.evaluate("__streams.at(-1).onopen()")
            else:
                page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'visible'}); document.dispatchEvent(new Event('visibilitychange'))")
            expect(page.locator("#messageInput")).to_be_enabled()
            assert not errors, errors
            page.close()
        RESULTS.append("SSE 重连及页面回到前台：不依赖补发终态事件，主动快照同步可恢复正确状态")


def bounded_poll_and_rejected_cancel(browser):
    with server("chat") as base:
        page, errors, data, chats, state = sync_fixture(browser, base, fast_poll=True)

        def cancel(route):
            data["turns"][0]["status"] = "cancel_requested"
            fulfill_json(route, {"turn_id": "sync-turn", "status": "cancel_requested"})

        page.route(re.compile(r"/turns/sync-turn/cancel$"), cancel)
        page.locator("#sendButton").click()
        expect(page.locator("#statusSync")).to_contain_text("仍在等待服务端确认终止")
        reads = state["reads"]
        page.wait_for_timeout(100)
        assert state["reads"] == reads, "Polls must stop after the bounded budget"
        expect(page.locator("#messageInput")).to_be_disabled()
        data["turns"][0]["status"] = "cancelled"
        page.locator("#retryStatusSync").click()
        expect(page.locator("#messageInput")).to_be_enabled()
        assert not errors, errors
        page.close()
        RESULTS.append("轮询达到 15 次后停止自动请求并提示重试；未确认终态不解锁输入")

        page, errors, data, chats, state = sync_fixture(browser, base)
        page.route(re.compile(r"/turns/sync-turn/cancel$"), lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"detail":"终止请求未受理"}'))
        page.locator("#sendButton").click()
        expect(page.locator("#toast")).to_contain_text("请再次点击终止任务")
        expect(page.locator("#sendButton")).to_be_enabled()
        expect(page.locator("#messageInput")).to_be_disabled()
        assert not errors, errors
        page.close()
        RESULTS.append("终止 POST 失败但快照仍运行：允许重试终止，保持输入锁定")


if __name__ == "__main__":
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
        try:
            rename_through_agent(browser)
            sidebar_terminal_race(browser)
            cancel_poll_and_failure_retry(browser)
            reconnect_and_foreground(browser)
            bounded_poll_and_rejected_cancel(browser)
        finally:
            browser.close()
    (REPORT / "browser-results.json").write_text(json.dumps({"status": "passed", "cases": RESULTS}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Rename/cancel browser acceptance passed: " + "；".join(RESULTS))
