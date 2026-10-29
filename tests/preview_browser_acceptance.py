"""Large preview dialog through real HTTP fixtures and controlled request faults.

Run: python3 tests/preview_browser_acceptance.py
Uses temporary databases/random ports and no model provider credentials.
"""

import json

from playwright.sync_api import expect, sync_playwright

from functional_browser_acceptance import ROOT, new_page, request, server


REPORT = ROOT / "docs" / "大弹窗预览验收-20261027"
SHOTS = REPORT / "screenshots"


def shot(page, filename):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / filename), full_page=True)


def run(browser):
    with server("chat") as base:
        cid = request(base, "/__test/seed-previews", method="POST")["conversation_id"]
        data = request(base, f"/api/conversations/{cid}")
        md = next(item for item in data["artifacts"] if item["kind"] == "md")
        page, errors = new_page(browser)
        page.emulate_media(reduced_motion="reduce")
        page.goto(f"{base}/?conversation={cid}")
        entry = page.locator(".deliverable-row").filter(has_text="方案.md")
        page.locator(".delivery-details summary").click()
        entry.wait_for()
        chat_scroll = page.locator("#chatScroll").evaluate("node => node.scrollTop")
        entry.click()
        dialog = page.locator("#previewDialog")
        expect(dialog).to_be_visible()
        expect(page.locator("#previewTitle")).to_have_text("方案.md")
        expect(page.locator(".preview-page .markdown-content h1")).to_have_text("评测方案")
        expect(page.locator("#closePreview")).to_be_focused()
        expect(page.locator("#drawer")).to_be_hidden()
        assert page.locator(".preview-page img").count() == 0
        bounds = dialog.bounding_box()
        assert bounds["width"] > 1440 * 0.8 and bounds["height"] > 900 * 0.8
        shot(page, "01-markdown-desktop.png")
        body = page.locator("#preview")
        assert body.evaluate("node => node.scrollHeight > node.clientHeight")
        head = page.locator(".preview-top").bounding_box()
        foot = page.locator(".preview-foot").bounding_box()
        body.hover()
        page.mouse.wheel(0, 1400)
        page.wait_for_function("document.querySelector('#preview').scrollTop > 500")
        assert page.locator(".preview-top").bounding_box()["y"] == head["y"]
        assert page.locator(".preview-foot").bounding_box()["y"] == foot["y"]
        page.locator("#closePreview").focus()
        for _ in range(4):
            page.keyboard.press("Tab")
            assert dialog.evaluate("node => node.contains(document.activeElement)")
        with page.expect_download() as download:
            page.locator("#previewDownload").click()
        assert download.value.suggested_filename == "方案.md"
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(entry).to_be_focused()
        assert page.locator("#chatScroll").evaluate("node => node.scrollTop") == chat_scroll

        page.locator("#toggleDrawer").click()
        csv_entry = page.locator(".artifact-row").filter(has_text="样本.csv")
        csv_entry.click()
        expect(page.locator(".csv-table tbody tr")).to_have_count(2)
        assert page.locator(".csv-table tbody tr").first.locator("td").nth(1).inner_text() == "电压,多少"
        assert page.locator(".csv-table tbody tr").first.locator("td").nth(2).inner_text() == "第一行\n第二行"
        assert page.locator("#preview script").count() == 0
        shot(page, "02-csv-desktop.png")
        page.locator("#closePreview").click()
        expect(csv_entry).to_be_focused()

        # A slow failure from an earlier file must not overwrite the new preview.
        held = []
        pattern = f"**/artifacts/{md['artifact_id']}/preview"
        page.route(pattern, lambda route: held.append(route))
        page.locator(".artifact-row").filter(has_text="方案.md").click()
        expect(page.locator(".preview-loading")).to_be_visible()
        page.locator("#closePreview").click()
        csv_entry.click()
        expect(page.locator(".csv-table")).to_be_visible()
        assert len(held) == 1
        with page.expect_response(lambda response: md["artifact_id"] in response.url):
            held[0].fulfill(status=503, content_type="application/json", body='{"detail":"旧预览请求失败"}')
        expect(page.locator("#previewTitle")).to_have_text("样本.csv")
        expect(page.locator(".preview-error")).to_have_count(0)
        page.unroute(pattern)
        page.keyboard.press("Escape")

        # A current failure stays inside the dialog and has a working retry.
        page.route(pattern, lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"detail":"暂时无法读取文件"}',
        ), times=1)
        page.locator(".artifact-row").filter(has_text="方案.md").click()
        expect(page.locator(".preview-error")).to_contain_text("暂时无法读取文件")
        page.get_by_role("button", name="重试预览").click()
        expect(page.locator(".preview-page .markdown-content h1")).to_have_text("评测方案")
        page.keyboard.press("Escape")

        # Backdrop closes the dialog; mobile keeps header/download reachable.
        csv_entry.click()
        expect(page.locator(".csv-table")).to_be_visible()
        page.mouse.click(5, 5)
        expect(dialog).to_be_hidden()
        page.set_viewport_size({"width": 390, "height": 844})
        csv_entry.click()
        expect(page.locator(".csv-table")).to_be_visible()
        bounds = dialog.bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        assert bounds["height"] > 844 * 0.9
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        shot(page, "03-csv-mobile.png")
        page.keyboard.press("Escape")
        page.locator("#closeDrawer").click()
        entry.click()
        expect(page.locator(".preview-page .markdown-content h1")).to_have_text("评测方案")
        shot(page, "04-markdown-mobile.png")
        page.locator("#closePreview").click()
        expect(entry).to_be_focused()
        assert not errors, errors
        page.close()


if __name__ == "__main__":
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True,
        )
        try:
            run(browser)
        finally:
            browser.close()
    (REPORT / "browser-results.json").write_text(json.dumps({
        "status": "passed", "cases": [
            "聊天与抽屉入口统一大弹窗，聊天入口不强制打开抽屉",
            "真实 HTTP Markdown/CSV 预览与净化、下载文件名正确",
            "长文正文独立滚动、标题和下载固定、聊天位置保留",
            "焦点限制与关闭后恢复、Escape/关闭按钮/遮罩关闭",
            "关闭后迟到请求不覆盖下一文件、当前失败可重试",
            "1440px 桌面与 390px 窄屏，大弹窗和横向表格无页面溢出",
        ],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Large preview browser acceptance passed: Markdown/CSV, download, scrolling, focus, close, stale requests, retry and mobile.")
