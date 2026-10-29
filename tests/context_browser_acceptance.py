"""Context UI acceptance against disposable FastAPI/SQLite/Deep Agents servers.

Run with system Python: python3 tests/context_browser_acceptance.py
No provider calls, .env access or user workspace data. The framework really
summarizes with a controlled local model and emits the production SSE protocol.
"""

import json
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from functional_browser_acceptance import ROOT, new_page, request, server


REPORT = ROOT / "docs" / "上下文状态验收-20261027"
SHOTS = REPORT / "screenshots"
RESULTS = []


def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / name), full_page=True)


def send(page):
    page.locator("#messageInput").fill("测试需求资料。" * 80)
    page.locator("#sendButton").click()


def finished(page):
    expect(page.locator("#messageInput")).to_be_enabled(timeout=10000)


def success_and_isolation(browser):
    with server("context") as base:
        page, errors = new_page(browser)
        page.goto(base)
        finished(page)
        cid = page.url.split("conversation=")[-1]
        expect(page.locator("#contextPercent")).to_have_text("—")
        page.locator("#contextButton").focus()
        expect(page.locator("#contextTooltip")).to_be_visible()
        expect(page.locator("#contextHeadline")).to_contain_text("尚未统计")
        page.keyboard.press("Escape")
        expect(page.locator("#contextTooltip")).to_be_hidden()
        send(page)
        finished(page)
        data = request(base, f"/api/conversations/{cid}")
        usage = data["context"]["usage"]
        percent = int(usage["used_tokens"] / usage["window_tokens"] * 100 + 0.5)
        expect(page.locator("#contextPercent")).to_have_text(f"{percent}%")
        page.locator("#contextButton").hover()
        expect(page.locator("#contextHeadline")).to_contain_text(f"剩余 {max(0, 100 - percent)}%")
        expect(page.locator("#contextTokens")).to_contain_text(f"{usage['used_tokens']:,}")
        expect(page.locator("#contextTooltip")).to_contain_text("近似估算")
        shot(page, "01-usage-desktop.png")
        page.locator("#contextButton").click()
        page.mouse.move(10, 10)
        expect(page.locator("#contextTooltip")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#contextTooltip")).to_be_hidden()
        page.reload()
        finished(page)
        expect(page.locator("#contextPercent")).to_have_text(f"{percent}%")

        send(page)
        notice = page.locator("#contextNotice")
        expect(notice).to_contain_text("正在整理对话上下文", timeout=10000)
        expect(page.locator("#contextMeter")).to_have_class(re.compile("is-compressing"))
        expect(page.locator("#sendButton")).to_be_enabled()
        expect(page.locator("#sendButton")).to_have_attribute("aria-label", "终止任务")
        expect(page.locator("#messageInput")).to_be_disabled()
        page.locator("#contextButton").hover()
        shot(page, "02-compression-running.png")
        page.reload()
        expect(notice).to_contain_text("正在整理对话上下文")
        finished(page)
        expect(notice).to_be_hidden()
        after = request(base, f"/api/conversations/{cid}")
        assert after["context"]["compression"]["status"] == "completed"
        assert "内部摘要" not in page.locator("#thread").inner_text()
        RESULTS.append("真实框架压缩：开始提示、停止按钮、刷新恢复、完成后占用更新、摘要不外泄")

        # New conversation has no measurements; selecting the old one restores its own value.
        current_percent = page.locator("#contextPercent").inner_text()
        page.locator("#newChat").click()
        finished(page)
        expect(page.locator("#contextPercent")).to_have_text("—")
        page.locator("#history button").nth(1).click()
        expect(page.locator("#contextPercent")).to_have_text(current_percent)
        RESULTS.append("占用值与 API 一致、未知值不伪装为零、刷新和对话切换隔离")

        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#contextButton").click()
        expect(page.locator("#contextTooltip")).to_be_visible()
        bounds = page.locator("#contextTooltip").bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        shot(page, "03-usage-mobile.png")
        page.keyboard.press("Escape")
        page.locator("#messageInput").click()
        page.keyboard.press("Tab")  # Upload button.
        page.keyboard.press("Tab")  # Context button.
        expect(page.locator("#contextButton")).to_be_focused()
        expect(page.locator("#contextTooltip")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#contextTooltip")).to_be_hidden()
        RESULTS.append("桌面悬停/点击、窄屏点击、键盘聚焦/Escape、浮层不溢出")
        assert not errors, errors
        page.close()


def failure_and_cancel(browser, mode):
    with server(mode) as base:
        page, errors = new_page(browser)
        page.goto(base)
        finished(page)
        send(page)
        finished(page)
        send(page)
        expect(page.locator("#contextNotice")).to_contain_text("正在整理对话上下文", timeout=10000)
        if mode == "context-slow":
            page.locator("#sendButton").click()
            finished(page)
            expect(page.locator("#contextNotice")).to_be_hidden()
            expect(page.locator("#contextMeter")).not_to_have_class(re.compile("is-compressing"))
            expect(page.locator(".notice")).to_contain_text("本轮已终止")
            shot(page, "05-compression-cancelled.png")
            RESULTS.append("压缩中手动终止：输入恢复、动画停止、无持续整理提示")
        else:
            finished(page)
            expect(page.locator("#contextNotice")).to_contain_text("整理对话上下文失败")
            expect(page.locator("#thread")).to_contain_text("整理对话上下文失败")
            assert "private summary error" not in page.locator("#thread").inner_text()
            shot(page, "04-compression-failed.png")
            RESULTS.append("压缩失败：真实错误状态、可见说明、输入恢复、内部错误不外泄")
        assert not errors, errors
        page.close()


if __name__ == "__main__":
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True,
        )
        try:
            success_and_isolation(browser)
            failure_and_cancel(browser, "context-failure")
            failure_and_cancel(browser, "context-slow")
        finally:
            browser.close()
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "browser-results.json").write_text(json.dumps({
        "status": "passed", "scope": "真实 HTTP/SQLite/SSE/Deep Agents；受控本地模型；未调用供应商 API",
        "cases": RESULTS,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Context browser acceptance passed: " + "；".join(RESULTS))
