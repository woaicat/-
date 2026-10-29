"""Selected A desktop UI through real HTTP/SQLite and controlled local models.

python3 tests/desktop_ui_acceptance.py --run 1
No .env reads or production data. Browser smoke cases also cover error paths.
"""

import argparse
import json
import subprocess
from io import BytesIO

from docx import Document
from playwright.sync_api import expect, sync_playwright

from functional_browser_acceptance import ROOT, new_page, request, server
import browser_ui_smoke as smoke

REPORT = ROOT / "docs" / "桌面UI验收-20261028"


def static_response(body, content_type):
    def handle(route):
        route.fulfill(content_type=content_type, body=body)
    return handle


def run(browser, folder):
    results = []
    folder.mkdir(parents=True, exist_ok=True)
    with server("chat") as base:
        page, errors = new_page(browser)
        page.set_viewport_size({"width": 1440, "height": 960})
        page.goto(base)
        expect(page.locator("#messageInput")).to_be_enabled()
        cid = page.url.split("conversation=")[-1]
        expect(page.locator(".empty-state h2")).to_have_text("把产品需求，变成可执行的评测方案")
        page.screenshot(animations="disabled", path=str(folder / "01-new-conversation.png"))
        page.locator(".welcome-prompt").first.click()
        expect(page.locator("#messageInput")).to_have_value("为产品问答助手设计一套评测方案")
        assert request(base, f"/api/conversations/{cid}")["messages"] == []
        page.locator("#welcomeUpload").focus()
        with page.expect_response(f"{base}/api/conversations/{cid}"):
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(page.locator("#welcomeUpload")).to_be_focused()
        document = Document()
        document.add_paragraph("产品知识库需要覆盖真实用户问题。")
        stream = BytesIO()
        document.save(stream)
        with page.expect_file_chooser() as chooser:
            page.locator("#welcomeUpload").click()
        chooser.value.set_files([
            {"name": "需求.md", "mimeType": "text/markdown", "buffer": "# 需求\n产品知识库问答".encode()},
            {"name": "补充.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "buffer": stream.getvalue()},
        ])
        expect(page.locator("#selectedAttachments .attachment-chip")).to_have_count(2)
        data = request(base, f"/api/conversations/{cid}")
        assert len(data["attachments"]) == 2 and not data["messages"]
        expect(page.locator("#messageInput")).to_have_value("为产品问答助手设计一套评测方案")
        page.locator("#messageInput").press("Enter")
        expect(page.locator(".message.assistant .text")).to_contain_text("Markdown 已渲染")
        expect(page.locator("#messageInput")).to_be_enabled()
        expect(page.locator(".empty-state")).to_have_count(0)
        data = request(base, f"/api/conversations/{cid}")
        assert len(data["messages"]) == 2
        assert all(a["message_id"] == data["messages"][0]["message_id"] for a in data["attachments"])
        page.reload()
        expect(page.locator(".message.user .attachment")).to_have_count(2)
        results.append("新对话：建议只填草稿；真实 Markdown/Word 多文件上传；Enter 发送；刷新恢复消息及附件")

        cid = request(base, "/__test/seed-desktop", method="POST")["conversation_id"]
        # Same workspace and viewport before/after, with the pre-implementation assets.
        before, before_errors = new_page(browser)
        before.set_viewport_size({"width": 1440, "height": 960})
        for path in ["frontend/app.js", "frontend/styles.css", "frontend/index.html"]:
            old = subprocess.check_output(["git", "show", f"bdbff1a:{path}"], cwd=ROOT).decode()
            old = old.replace("{{ASSET_VERSION}}", "baseline")
            route_path = f"{base}/?*" if path.endswith("index.html") else "**/assets/" + path.split("/")[-1] + "?*"
            content_type = "text/html" if path.endswith("html") else "text/css" if path.endswith("css") else "text/javascript"
            before.route(route_path, static_response(old, content_type))
        before.goto(f"{base}/?conversation={cid}")
        expect(before.locator("#artifactCount")).to_have_text("6")
        before.locator("#toggleDrawer").click()
        expect(before.locator(".artifact-row")).to_have_count(6)
        before.screenshot(animations="disabled", path=str(folder / "02-before-files.png"))
        old_history = before.locator("#history").inner_text()
        assert not before_errors, before_errors
        before.close()

        page.goto(f"{base}/?conversation={cid}")
        expect(page.locator("#artifactCount")).to_have_text("6")
        expect(page.locator("#topStatus")).to_have_text("本轮已完成")
        assert page.locator("#history").inner_text() == old_history
        details = page.locator(".delivery-details")
        expect(details).not_to_have_attribute("open", "")
        page.locator(".delivery-browse").click()
        expect(page.locator("#drawer")).to_be_visible()
        expect(page.locator(".artifact-row")).to_have_count(6)
        expect(page.locator(".file-group-head")).to_have_count(2)
        expect(page.locator(".file-group-head").first).to_contain_text("文档")
        data = request(base, f"/api/conversations/{cid}")
        for artifact in data["artifacts"]:
            row = page.locator(f'.artifact-row[data-artifact-id="{artifact["artifact_id"]}"]')
            expect(row.locator(".artifact-revision")).to_contain_text(f'第 {artifact["content_revision"]} 版')
            assert row.locator(".artifact-name").evaluate("node => getComputedStyle(node).fontSize") == "13px"
        assert {a["content_revision"] for a in data["artifacts"]} == {1, 2}
        page.screenshot(animations="disabled", path=str(folder / "03-after-files.png"))
        # Verify the actual filename determines preview and download, including revised files.
        row = page.locator(".artifact-row").filter(has_text="产品问答助手评测方案.md")
        row.click()
        expect(page.locator("#previewTitle")).to_have_text("产品问答助手评测方案.md")
        expect(page.locator("#previewRevision")).to_contain_text("第 2 版")
        expect(page.locator("#preview")).to_contain_text("补充人工复核")
        with page.expect_download() as download:
            page.locator("#previewDownload").click()
        assert download.value.suggested_filename == "产品问答助手评测方案.md"
        page.screenshot(animations="disabled", path=str(folder / "04-preview.png"))
        page.keyboard.press("Escape")
        expect(row).to_be_focused()
        page.locator(".artifact-row").filter(has_text="评测样本设计表.csv").click()
        expect(page.locator(".csv-table tbody tr")).to_have_count(1)
        page.keyboard.press("Escape")
        results.append("交付区：同数据前后对比；历史列表一致；6 份文件按类型分组；逐份真实版本；Markdown/CSV 预览、下载与焦点恢复")

        page.locator("#closeDrawer").click()
        page.locator(".delivery-details summary").click()
        expect(details).to_have_attribute("open", "")
        page.locator(".deliverable-row").filter(has_text="评测指标定义表.csv").click()
        expect(page.locator(".csv-table")).to_be_visible()
        expect(page.locator("#drawer")).to_be_hidden()
        page.keyboard.press("Escape")
        # Foreground synchronization must not unexpectedly collapse an expanded list.
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(details).to_have_attribute("open", "")
        page.reload()
        expect(page.locator("#artifactCount")).to_have_text("6")
        expect(page.locator(".delivery-details")).not_to_have_attribute("open", "")
        page.locator(".delivery-browse").click()
        page.set_viewport_size({"width": 1280, "height": 900})
        page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(animations="disabled", path=str(folder / "05-desktop-1280.png"))
        page.locator("#newChat").click()
        expect(page.locator("#conversationTitle")).to_have_text("新对话")
        expect(page.locator("#drawer")).to_be_hidden()
        expect(page.locator("#contextPercent")).to_have_text("—")
        expect(page.locator("#welcomeUpload")).to_be_visible()
        results.append("折叠清单：可展开直达预览，同步不重置；刷新恢复真实文件；1280 桌面无横向溢出；新建对话清空文件区与占用值")
        assert not errors, errors
        page.close()

        # Existing regression cases use intercepted API responses, no production writes.
        smoke.BASE_URL = base
        for case in [smoke.check_send_retry, smoke.check_conversation_isolation, smoke.check_outline,
                     smoke.check_running, smoke.check_stream_and_previews]:
            case(browser)
        results.append("既有回归：发送失败幂等重试、对话及草稿隔离、快速切换、大纲编辑确认、运行中停止、流式 Markdown/CSV 与内容清理")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=int)
    args = parser.parse_args()
    folder = REPORT / f"pass-{args.run}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
        try:
            results = run(browser, folder)
        finally:
            browser.close()
    (folder / "results.json").write_text(json.dumps({"status": "passed", "run": args.run, "cases": results}, ensure_ascii=False, indent=2) + "\n")
    print(f"Desktop acceptance pass {args.run}: passed " + "；".join(results))
