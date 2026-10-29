"""Full-browser acceptance against disposable real HTTP servers.

System Python needs Playwright and Chrome; the app runs in its own .venv.
Run: python3 tests/functional_browser_acceptance.py [--real]
All server data goes to a temporary directory; port 8000 is never used.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "功能测试报告-20261025"
SHOTS = REPORT / "screenshots"
RESULTS = []


def request(base, path, *, method="GET", body=None, timeout=15):
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"} if raw is not None else {}
    req = urllib.request.Request(base + path, data=raw, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read()
        return json.loads(data) if "application/json" in response.headers.get("Content-Type", "") else data


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def server(mode):
    with tempfile.TemporaryDirectory(prefix=f"eval-v2-{mode}-") as directory:
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        args = ["tests/functional_server.py", "--data-root", directory, "--port", str(port), "--mode", mode]
        if mode.startswith("real"):
            command = ["uv", "run", "--offline", "--env-file", ".env", "python", *args]
        else:
            command = [str(ROOT / ".venv" / "bin" / "python"), *args]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT / "tests")
        with (Path(directory) / "server.log").open("w") as output:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"Isolated {mode} server exited with {process.returncode}")
                    try:
                        request(base, "/")
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.1)
                else:
                    raise TimeoutError(f"Isolated {mode} server failed to start")
                yield base
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def new_page(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    return page, errors


def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    file = SHOTS / name
    page.screenshot(path=str(file), full_page=True)
    return str(file.relative_to(REPORT))


def record(case_id, category, title, expected, actual, screenshot=None, status="PASS"):
    RESULTS.append({"id": case_id, "category": category, "title": title,
                    "expected": expected, "actual": actual,
                    "screenshot": screenshot, "status": status})
    print(f"{case_id} {status}: {title} — {actual}", flush=True)


def fail(case_id, category, title, expected, error, page=None):
    screenshot = shot(page, f"{case_id}-failure.png") if page else None
    record(case_id, category, title, expected, f"{type(error).__name__}: {error}", screenshot, "FAIL")


def wait_final(page, timeout=20000):
    page.wait_for_function("document.querySelector('#topStatus')?.textContent === '本轮已完成' && !!document.querySelector('.message.assistant .text')", timeout=timeout)


def fake_chat(browser):
    with server("chat") as base:
        page, errors = new_page(browser)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#history button").first.wait_for()
            first = page.url.split("conversation=")[-1]
            assert first and first != page.url
            input_box = page.locator("#messageInput")
            input_box.fill("A 对话独有草稿")
            page.locator("#newChat").click()
            page.wait_for_function("previous => new URL(location.href).searchParams.get('conversation') !== previous", arg=first)
            second = page.url.split("conversation=")[-1]
            assert input_box.input_value() == ""
            input_box.fill("B 对话独有消息")
            page.locator("#history button:not(.active)").click()
            page.wait_for_function("expected => new URL(location.href).searchParams.get('conversation') === expected", arg=first)
            assert input_box.input_value() == "A 对话独有草稿"
            page.locator("#history button:not(.active)").click()
            page.wait_for_function("expected => new URL(location.href).searchParams.get('conversation') === expected", arg=second)
            assert input_box.input_value() == "B 对话独有消息"
            record("FE-01", "真实浏览器＋模拟模型", "新建两段对话后草稿按会话隔离",
                   "A/B 草稿互不显示，切回各自恢复原文", "两次往返切换均显示正确草稿", shot(page, "01-conversation-drafts.png"))

            page.locator("#sendButton").click()
            page.locator(".message.assistant .markdown-content strong").wait_for(timeout=20000)
            wait_final(page)
            assert page.locator(".message.assistant .markdown-content h2").inner_text() == "测试回复"
            assert "**Markdown" not in page.locator(".message.assistant .text").inner_text()
            assert page.locator(".message.assistant table").count() == 1
            assert input_box.is_enabled()
            assert not errors, errors
            record("FE-02", "真实浏览器＋模拟模型", "真实 HTTP 发送、流式回复及 Markdown",
                   "发送成功；加粗/表格渲染；执行时禁输入，完成后恢复", "B 消息和已渲染回复可见；最终输入框可用；无页面异常", shot(page, "02-send-markdown.png"))

            page.locator("#history button:not(.active)").click()
            page.wait_for_function("expected => new URL(location.href).searchParams.get('conversation') === expected", arg=first)
            assert input_box.input_value() == "A 对话独有草稿"
            assert page.locator(".message.user").count() == 0
            page.locator("#sendButton").click()
            wait_final(page)
            first_data = request(base, f"/api/conversations/{first}")
            second_data = request(base, f"/api/conversations/{second}")
            assert first_data["messages"][0]["body"] == "A 对话独有草稿"
            assert second_data["messages"][0]["body"] == "B 对话独有消息"
            assert first_data["conversation"]["agent_thread_id"] != second_data["conversation"]["agent_thread_id"]
            other, other_errors = new_page(browser)
            other.goto(f"{base}/?conversation={second}", wait_until="domcontentloaded")
            other.locator(".message.user .text").wait_for()
            assert other.locator(".message.user .text").first.inner_text() == "B 对话独有消息"
            assert not other_errors, other_errors
            other.close()
            record("FE-03", "真实浏览器＋模拟模型", "消息及智能体线程隔离，跨标签读取",
                   "A/B 各自只有本会话文本且检查点线程不同", "API 快照、当前标签和第二标签一致；线程 ID 不同", shot(page, "03-message-isolation.png"))

            page.locator("#fileInput").set_input_files({"name": "需求.md", "mimeType": "text/markdown", "buffer": b"# Product A\nNeeds test."})
            page.locator(".attachment-chip").wait_for()
            input_box.fill("参考刚上传的资料")
            page.locator("#sendButton").click()
            wait_final(page)
            page.wait_for_function("document.querySelectorAll('.message.user').length === 2")
            updated = request(base, f"/api/conversations/{first}")
            assert len(updated["attachments"]) == 1
            assert updated["attachments"][0]["message_id"] == updated["messages"][2]["message_id"]
            assert request(base, f"/api/conversations/{second}")["attachments"] == []
            record("FE-04", "真实浏览器＋模拟模型", "对话内上传 MD 并随消息发送",
                   "附件与该会话的第二条用户消息关联，B 不受影响", "文件上传/发送完成，服务端绑定到 A，B 附件为空", shot(page, "04-attachment-sent.png"))

            invalid = page.request.post(f"{base}/api/conversations/{first}/attachments", multipart={
                "upload_key": "invalid-pdf", "file": {"name": "不支持.pdf", "mimeType": "application/pdf", "buffer": b"%PDF"},
            })
            assert invalid.status == 422
            assert "只支持" in invalid.json()["detail"]
            assert len(request(base, f"/api/conversations/{first}")["attachments"]) == 1
            record("BE-01", "真实 HTTP API", "不支持文件类型被后端拒绝",
                   "PDF 返回 422 且不加入附件列表", "HTTP 422；原有附件数不变", shot(page, "05-invalid-upload.png"))

            seeded = request(base, "/__test/seed-previews", method="POST")['conversation_id']
            page.goto(f"{base}/?conversation={seeded}", wait_until="domcontentloaded")
            page.locator("#toggleDrawer").click()
            page.locator(".artifact-row").filter(has_text="方案.md").click()
            page.locator(".preview-page .markdown-content table").wait_for()
            assert page.locator(".preview-page .markdown-content strong").inner_text() == "关键指标"
            assert page.locator(".preview-page img").count() == 0
            record("FE-06", "真实浏览器＋模拟数据", "真实 API 的 Markdown 产物预览与净化",
                   "标题/加粗/表格渲染；危险 HTML 不执行", "浏览器正确渲染并移除 img 注入", shot(page, "06-markdown-preview.png"))
            page.locator("#closePreview").click()
            page.locator(".artifact-row").filter(has_text="样本.csv").click()
            page.locator(".preview-page .csv-table").wait_for()
            assert page.locator(".csv-table tbody tr").count() == 2
            assert page.locator(".csv-table tbody tr").first.locator("td").nth(1).inner_text() == "电压,多少"
            assert page.locator(".csv-table tbody tr").first.locator("td").nth(2).inner_text() == "第一行\n第二行"
            assert page.locator(".preview-page script").count() == 0
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            record("FE-07", "真实浏览器＋模拟数据", "CSV 引号/换行预览与窄屏布局",
                   "CSV 正确分列，无脚本执行；390px 页面无横向溢出", "2 行数据正确，嵌入脚本未生成；窄屏未溢出", shot(page, "07-csv-mobile.png"))
            assert not errors, errors
        except Exception as error:
            fail("FE-UNEXPECTED", "真实浏览器＋模拟模型", "聊天/附件/预览流程中断", "前述用例完成", error, page)
        finally:
            page.close()


def initialization_race(browser):
    with server("chat") as base:
        page, errors = new_page(browser)
        held = []
        send_urls = []

        def hold_create(route):
            if route.request.method == "POST":
                held.append(route)
            else:
                route.continue_()

        page.route(re.compile(r"/api/conversations$"), hold_create)
        page.on("request", lambda req: send_urls.append(req.url) if "/messages" in req.url else None)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_timeout(300)
            assert held, "Initial conversation create request was not held"
            assert page.locator("#messageInput").is_disabled()
            assert page.locator("#attachButton").is_disabled()
            assert page.locator("#sendButton").is_disabled()
            record("FE-INIT-01", "真实浏览器＋故意延迟初始 API", "会话初始化期间不允许错误发送",
                   "会话 ID 未建立时输入/发送/上传均禁用，不请求 /null/messages",
                   "延迟创建期间三个控件均锁定", shot(page, "00-initialization-locked.png"))
            held.pop().continue_()
            page.locator("#history button").first.wait_for()
            assert page.locator("#messageInput").is_enabled()
            page.locator("#messageInput").fill("初始化后可正常发送")
            page.locator("#sendButton").click()
            wait_final(page)
            assert all("/null/" not in url for url in send_urls)
            assert not errors, errors
            record("FE-INIT-02", "真实浏览器＋模拟模型", "初始化完成后恢复发送",
                   "消息发至真实会话，回复可见且无 /null/messages",
                   "已发送并完成；所捕获请求均使用真实会话 ID", shot(page, "00-initialization-recovered.png"))
        except Exception as error:
            fail("FE-INIT-UNEXPECTED", "真实浏览器＋模拟模型", "初始化竞态回归失败",
                 "初始锁定后正常发送", error, page)
        finally:
            for route in held:
                try:
                    route.abort()
                except Exception:
                    pass
            page.close()


def outline_switch_race(browser):
    with server("workflow") as base:
        first = request(base, "/api/conversations", method="POST", body={"title": "大纲竞争 A"})["conversation_id"]
        request(base, f"/api/conversations/{first}/messages", method="POST",
                body={"body": "请设计一整套评测方案", "client_key": "outline-race-plan"})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            data = request(base, f"/api/conversations/{first}")
            if data["outlines"] and data["turns"][-1]["status"] == "completed":
                break
            time.sleep(0.1)
        else:
            record("FE-OUTLINE-01", "真实浏览器＋模拟智能体", "大纲确认时切换会话",
                   "准备好 A 的待确认大纲", "准备阶段未完成", None, "BLOCKED")
            return
        page, errors = new_page(browser)
        held = []
        confirm_urls = []

        def hold_edit(route):
            if route.request.method == "PUT":
                held.append(route)
            else:
                route.continue_()

        page.on("request", lambda req: confirm_urls.append(req.url) if "/confirm" in req.url else None)
        try:
            page.goto(f"{base}/?conversation={first}", wait_until="domcontentloaded")
            page.get_by_role("button", name="编辑大纲").wait_for()
            page.route(re.compile(rf"/api/conversations/{first}/outlines/[^/]+$"), hold_edit)
            page.get_by_role("button", name="编辑大纲").click()
            editor = page.get_by_role("textbox", name="可编辑的评测方案大纲")
            editor.fill(editor.input_value() + "\n确认切换回归。")
            page.get_by_role("button", name="确认并开始 →").click()
            deadline = time.monotonic() + 5
            while not held and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert held, "Outline PUT was not held"
            page.locator("#newChat").click()
            page.wait_for_function("old => new URL(location.href).searchParams.get('conversation') !== old", arg=first)
            second = page.url.split("conversation=")[-1]
            request(base, "/__test/phase/generation", method="POST")
            held.pop().continue_()
            deadline = time.monotonic() + 10
            while not confirm_urls and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert confirm_urls, "No confirm request after PUT"
            assert all(f"/conversations/{first}/" in url for url in confirm_urls), confirm_urls
            assert page.url.endswith(second)
            assert page.locator("#conversationTitle").inner_text() == "新对话"
            assert not errors, errors
            record("FE-OUTLINE-01", "真实浏览器＋故意延迟大纲 PUT", "确认大纲中途切换会话仍提交到原会话",
                   "确认 POST 固定 A，B 页面不被旧回调覆盖",
                   f"确认请求仅发向 A；当前页面仍为 B（{len(confirm_urls)} 次确认请求）",
                   shot(page, "19-outline-switch-stays-on-b.png"))
            page.get_by_role("button", name="发送消息").wait_for()
            first_data = request(base, f"/api/conversations/{first}")
            assert first_data["outlines"][0]["status"] == "confirmed"
        except Exception as error:
            fail("FE-OUTLINE-UNEXPECTED", "真实浏览器＋模拟智能体", "大纲确认切换竞态回归失败",
                 "确认保留在 A，B 不受影响", error, page)
        finally:
            for route in held:
                try:
                    route.abort()
                except Exception:
                    pass
            page.close()


def fake_edit_and_products(browser):
    with server("edit") as base:
        fixture = request(base, "/__test/seed-edit", method="POST")
        conversation_id = fixture["conversation_id"]
        ids = fixture["artifacts"]
        before = request(base, f"/api/conversations/{conversation_id}")
        before_versions = {row["artifact_id"]: row["content_revision"] for row in before["artifacts"]}
        page, errors = new_page(browser)
        try:
            page.goto(f"{base}/?conversation={conversation_id}", wait_until="domcontentloaded")
            page.locator("#toggleDrawer").click()
            assert page.locator(".folder").count() == 2
            assert page.locator(".folder").filter(has_text="甲产品").count() == 1
            assert page.locator(".folder").filter(has_text="乙产品").count() == 1
            assert len({row["evaluation_id"] for row in before["artifacts"]}) == 2
            page.locator(".artifact-row").filter(has_text="需求摘要.md").click()
            page.locator(".preview-page .markdown-content").wait_for()
            assert "只属于乙产品" in page.locator(".preview-page").inner_text()
            record("FE-11", "真实浏览器＋真实 HTTP 测试夹具", "同一会话两个产品独立文件夹和预览",
                   "甲/乙文件分别归属产品，点击乙文件只显示乙内容",
                   "抽屉显示两个文件夹，API evaluation_id 不同；乙预览正确",
                   shot(page, "20-two-product-folders.png"))

            page.locator("#closePreview").click()
            page.locator("#closeDrawer").click()
            page.locator("#messageInput").fill("把甲产品指标框架的准确率阈值改为 90%，执行说明和乙产品需求摘要保持原样。")
            page.locator("#sendButton").click()
            wait_final(page, timeout=30000)
            final = request(base, f"/api/conversations/{conversation_id}")
            assert final["turns"][-1]["status"] == "completed"
            final_by_id = {row["artifact_id"]: row for row in final["artifacts"]}
            assert final_by_id[ids["指标框架.md"]]["content_revision"] == before_versions[ids["指标框架.md"]] + 1
            assert final_by_id[ids["执行说明.md"]]["content_revision"] == before_versions[ids["执行说明.md"]]
            assert final_by_id[ids["需求摘要.md"]]["content_revision"] == before_versions[ids["需求摘要.md"]]
            assert final["messages"][-1]["artifact_ids"] == [ids["指标框架.md"]]
            page.locator(".delivery-details summary").click()
            page.locator(".deliverable-row").click()
            page.locator(".preview-page .markdown-content").wait_for()
            assert "90%" in page.locator(".preview-page").inner_text()
            assert "80%" not in page.locator(".preview-page").inner_text()
            assert not errors, errors
            record("AG-16", "真实浏览器＋真实 HTTP＋模拟智能体", "聊天修改已有产物并只升级目标版本",
                   "模型读旧文件、选择目标权限、发布修订；稳定 artifact ID，未点名文件不变；回复可点击最新预览",
                   "指标框架版本 1→2、预览阈值 90%；另两文件仍版本 1，回复仅关联目标文件",
                   shot(page, "21-focused-edit-updated-preview.png"))
        except Exception as error:
            fail("AG-EDIT-UNEXPECTED", "真实浏览器＋模拟智能体", "局部修改或多产品流程中断",
                 "多产品独立且仅目标文件升版", error, page)
        finally:
            page.close()


def fake_continue_after_cancel(browser):
    with server("continue") as base:
        fixture = request(base, "/__test/seed-cancelled", method="POST")
        conversation_id = fixture["conversation_id"]
        before = request(base, f"/api/conversations/{conversation_id}")
        assert before["evaluations"][0]["package_status"] == "incomplete"
        assert [item["status"] for item in before["deliverables"]] == ["ready", "pending"]
        page, errors = new_page(browser)
        try:
            page.goto(f"{base}/?conversation={conversation_id}", wait_until="domcontentloaded")
            page.locator(".notice").wait_for()
            assert "本轮已终止" in page.locator(".notice").inner_text()
            page.locator("#toggleDrawer").click()
            assert "未完成的任务产物" in page.locator(".folder-state").inner_text()
            page.locator(".artifact-row").click()
            page.locator(".preview-page").wait_for()
            assert "已完成的设计思路" in page.locator(".preview-page").inner_text()
            record("AG-17", "真实浏览器＋已终止任务夹具", "终止后已完成文件保留并标记未完成",
                   "任务状态已终止；已完成文件可预览，文件夹标记未完成的任务产物",
                   "页面与 API 均显示 1 个 ready、1 个 pending；已完成文件仍可预览",
                   shot(page, "22-cancelled-package-keeps-file.png"))

            page.locator("#closePreview").click()
            page.locator("#closeDrawer").click()
            page.locator("#messageInput").fill("接着已有文件完成剩余工作，只补样本.csv，不要重写评测方案.md。")
            page.locator("#sendButton").click()
            wait_final(page, timeout=30000)
            final = request(base, f"/api/conversations/{conversation_id}")
            assert final["turns"][-1]["scope_kind"] == "package_continue"
            assert final["turns"][-1]["status"] == "completed"
            assert final["evaluations"][0]["package_status"] == "completed"
            assert [item["status"] for item in final["deliverables"]] == ["ready", "ready"]
            assert len(final["artifacts"]) == 2
            assert final["artifacts"][0]["artifact_id"] == fixture["first_artifact_id"]
            assert final["artifacts"][0]["content_revision"] == 1
            assert set(final["messages"][-1]["artifact_ids"]) == {item["artifact_id"] for item in final["artifacts"]}
            assert page.locator(".deliverable-row").count() == 2
            assert page.locator("#artifactCount").inner_text() == "2"
            assert not errors, errors
            record("AG-18", "真实浏览器＋真实 HTTP＋模拟智能体", "通过聊天续做剩余文件并恢复整包完成",
                   "仅补待办文件；原文件 ID 和版本不变；完成回复可点击全部产物",
                   "续做轮次为 package_continue；原文件版本仍为 1，新增样本.csv，2 个文件均在回复清单",
                   shot(page, "23-continued-package-complete.png"))
        except Exception as error:
            fail("AG-CONTINUE-UNEXPECTED", "真实浏览器＋模拟智能体", "终止后续做流程中断",
                 "已完成文件保留，聊天续做只补剩余文件", error, page)
        finally:
            page.close()


def confirmed_outline_and_live_progress(browser):
    with server("continue") as base:
        fixture = request(base, "/__test/seed-running", method="POST")
        cid = fixture["conversation_id"]
        page, errors = new_page(browser)
        try:
            page.goto(f"{base}/?conversation={cid}", wait_until="domcontentloaded")
            card = page.locator(".outline-card.is-confirmed")
            card.wait_for()
            assert "评测目标" in card.inner_text()
            assert "已确认" in card.inner_text()
            assert card.get_by_role("button", name="确认并开始 →").count() == 0
            assert card.get_by_role("button", name="编辑大纲").count() == 0
            progress = page.locator(".package-progress")
            progress.wait_for()
            assert card.bounding_box()["y"] < progress.bounding_box()["y"]
            assert progress.locator(".package-progress-count").inner_text() == "已完成 1 / 2"
            assert progress.get_by_role("progressbar").get_attribute("aria-valuenow") == "1"
            assert progress.locator(".package-progress-item.is-ready").count() == 1
            assert progress.locator(".package-progress-item.is-pending").count() == 1
            page.locator("#chatScroll").evaluate("element => { element.scrollTop = 0; }")
            record("AG-PROGRESS-01", "真实浏览器＋真实 HTTP 测试夹具", "确认后保留大纲并显示已有文件进度",
                   "已确认大纲只读保留在进度卡片上方；实时显示 1/2 和待办文件",
                   "大纲正文和已确认状态可见；进度 1/2，已完成文件可点击、剩余文件待办",
                   shot(page, "35-confirmed-outline-progress-one-of-two.png"))
            progress.scroll_into_view_if_needed()
            record("AG-PROGRESS-01B", "真实浏览器＋真实 HTTP 测试夹具", "部分文件完成时展示清单与预览入口",
                   "生成中展示 1/2 进度、已完成和待完成文件；已完成项可点击",
                   "文件清单显示一项可预览、一项待完成",
                   shot(page, "35b-partial-progress-one-of-two.png"))

            progress.locator("button.package-progress-item").click()
            page.locator(".preview-page").wait_for()
            assert "已完成的设计思路" in page.locator(".preview-page").inner_text()
            page.locator("#closePreview").click()
            request(base, f"/__test/publish-remaining/{cid}", method="POST")
            page.wait_for_function("document.querySelector('.package-progress-count')?.textContent === '已完成 2 / 2'")
            assert progress.locator(".package-progress-item.is-ready").count() == 2
            assert page.locator("#artifactCount").inner_text() == "2"
            assert not errors, errors
            record("AG-PROGRESS-02", "真实浏览器＋SSE 产物事件", "新文件就绪后聊天进度立即更新",
                   "artifact.ready 到达后进度变 2/2，已完成文件可直接预览",
                   "页面未刷新即更新到 2/2；抽屉数量同步变为 2；无 JavaScript 异常",
                   shot(page, "36-live-progress-two-of-two.png"))

            page.reload(wait_until="domcontentloaded")
            page.locator(".package-progress-count").wait_for()
            assert page.locator(".outline-card.is-confirmed").count() == 1
            assert page.locator(".package-progress-count").inner_text() == "已完成 2 / 2"
            request(base, f"/__test/finish-running/{cid}", method="POST")
            page.wait_for_function("document.querySelector('#topStatus')?.textContent === '本轮已完成'")
            assert page.locator(".outline-card.is-confirmed").count() == 1
            assert page.locator(".package-progress").count() == 0
            record("AG-PROGRESS-03", "真实浏览器＋持久快照", "刷新和任务完成后大纲仍保留",
                   "运行中刷新恢复进度；任务完成后大纲继续可看且不出现重复确认",
                   "刷新恢复 2/2；任务完成后只读大纲仍在，进行中进度结束",
                   shot(page, "37-outline-retained-after-completion.png"))
        except Exception as error:
            fail("AG-PROGRESS-UNEXPECTED", "真实浏览器＋真实 HTTP 测试夹具", "确认大纲与进度流程中断",
                 "大纲持久、进度随文件发布更新并在刷新后恢复", error, page)
        finally:
            page.close()


def fake_workflow(browser):
    with server("workflow") as base:
        page, errors = new_page(browser)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#history button").first.wait_for()
            page.locator("#messageInput").fill("为产品 A 设计一整套评测方案，先给我可确认的大纲")
            page.locator("#sendButton").click()
            page.locator(".outline-card").wait_for(timeout=30000)
            page.locator(".outline-preview h2").first.wait_for()
            page.get_by_role("button", name="编辑大纲").wait_for(state="visible")
            page.wait_for_function("document.querySelector('.outline-edit')?.disabled === false", timeout=30000)
            conversation_id = page.url.split("conversation=")[-1]
            data = request(base, f"/api/conversations/{conversation_id}")
            assert data["artifacts"] == []
            assert data["outlines"][0]["status"] == "pending"
            assert page.locator(".outline-textarea").is_hidden()
            record("AG-01", "真实浏览器＋模拟智能体", "整包任务先展示可确认大纲",
                   "包含目标/指标/数据/执行/清单；确认前无产物", "大纲卡片渲染，API 确认前 artifacts 为空", shot(page, "08-outline-before-confirm.png"))

            page.get_by_role("button", name="编辑大纲").click()
            editor = page.get_by_role("textbox", name="可编辑的评测方案大纲")
            editor.fill(data["outlines"][0]["body"].replace("检验质量", "检验质量与稳定性"))
            page.get_by_role("button", name="预览大纲").click()
            assert "检验质量与稳定性" in page.locator(".outline-preview").inner_text()
            request(base, "/__test/phase/generation", method="POST")
            page.get_by_role("button", name="确认并开始 →").click()
            page.locator(".delivery-details summary").click(timeout=30000)
            page.locator(".deliverable-row").wait_for(timeout=30000)
            page.wait_for_function("document.querySelector('#topStatus')?.textContent === '本轮已完成'", timeout=30000)
            final = request(base, f"/api/conversations/{conversation_id}")
            assert final["outlines"][0]["status"] == "confirmed"
            assert final["outlines"][0]["revision"] == 2
            assert len(final["artifacts"]) == 1
            assert final["artifacts"][0]["filename"] == "评测方案.docx"
            assert final["messages"][-1]["artifact_ids"] == [final["artifacts"][0]["artifact_id"]]
            assert not errors, errors
            record("AG-02", "真实浏览器＋模拟智能体", "大纲编辑确认后生成并给出可点击产物清单",
                   "修订版本被采用，完成回复列出文件并可预览", "大纲 revision=2 且已确认；DOCX 就绪，完成消息关联产物", shot(page, "09-completed-deliverables.png"))

            page.locator(".deliverable-row").click()
            page.locator(".preview-page").wait_for()
            assert "评测方案" in page.locator(".preview-page").inner_text()
            record("AG-03", "真实浏览器＋模拟智能体", "完成回复点击产物打开只读预览",
                   "文件从回复列表直达当前页面大弹窗", "点击完成回复中的文件后弹窗显示正文", shot(page, "10-completed-preview.png"))
            page.reload(wait_until="domcontentloaded")
            page.locator(".deliverable-row").wait_for(state="attached")
            assert page.locator(".message.assistant .elapsed-value").count() > 0
            assert page.locator("#artifactCount").inner_text() == "1"
            record("AG-04", "真实浏览器＋模拟智能体", "刷新后恢复对话、耗时与产物",
                   "刷新后仍可看到回复、最终耗时和产物清单", "刷新恢复后文件数 1，时长标签存在", shot(page, "11-refresh-recovery.png"))
        except Exception as error:
            fail("AG-UNEXPECTED", "真实浏览器＋模拟智能体", "大纲/交付流程中断", "前述用例完成", error, page)
        finally:
            page.close()


def outline_is_confirmable_when_model_followup_is_slow(browser):
    with server("outline-slow-final") as base:
        page, errors = new_page(browser)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#messageInput").wait_for(state="visible")
            page.wait_for_function("document.querySelector('#messageInput')?.disabled === false")
            page.locator("#messageInput").fill("请为产品 A 设计一整套评测方案")
            page.locator("#sendButton").click()
            page.wait_for_function(
                "document.querySelector('.outline-card') && "
                "document.querySelector('#topStatus')?.textContent === '等待确认' && "
                "document.querySelector('.outline-card button.primary')?.disabled === false",
                timeout=8000,
            )
            cid = page.url.split("conversation=")[-1]
            snapshot = request(base, f"/api/conversations/{cid}")
            assert snapshot["turns"][-1]["status"] == "completed"
            assert snapshot["outlines"][0]["status"] == "pending"
            assert page.get_by_role("button", name="编辑大纲").is_enabled()
            assert page.locator("#messageInput").is_enabled()
            record("AG-OUTLINE-READY-01", "真实浏览器＋慢速后续模型", "大纲提交后无需等待模型再回复",
                   "规划轮次结束，大纲立即可编辑和确认",
                   "模拟后续模型延迟 10 秒；大纲出现时轮次已完成，编辑与确认按钮可用",
                   shot(page, "32-outline-immediately-confirmable.png"))

            request(base, "/__test/phase/generation", method="POST")
            page.get_by_role("button", name="确认并开始 →").click()
            page.wait_for_function(
                "document.querySelector('#artifactCount')?.textContent === '1' && "
                "document.querySelector('#topStatus')?.textContent === '本轮已完成'",
                timeout=20000,
            )
            final = request(base, f"/api/conversations/{cid}")
            assert final["outlines"][0]["status"] == "confirmed"
            assert final["turns"][-1]["status"] == "completed"
            assert len(final["artifacts"]) == 1
            assert not errors, errors
            record("AG-OUTLINE-READY-02", "真实浏览器＋慢速后续模型", "大纲可直接确认并继续生成",
                   "点击确认启动新轮次，文件生成完成，无需先终止规划轮次",
                   "确认请求成功，后续轮次完成并发布 1 个文件",
                   shot(page, "33-outline-confirmed-without-stop.png"))
        except Exception as error:
            fail("AG-OUTLINE-READY-UNEXPECTED", "真实浏览器＋慢速后续模型", "大纲即时确认回归失败",
                 "大纲可编辑、确认并生成文件", error, page)
        finally:
            page.close()


def fake_cancel(browser):
    with server("slow") as base:
        page, errors = new_page(browser)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#history button").first.wait_for()
            page.locator("#messageInput").fill("请慢慢回答")
            page.locator("#sendButton").click()
            page.locator(".processing-activity").wait_for(timeout=15000)
            assert page.locator(".processing-label").inner_text() in {"稍等一下", "努力规划中", "马上出炉"}
            assert page.locator("#messageInput").is_disabled()
            assert page.locator("#attachButton").is_disabled()
            assert page.get_by_role("button", name="终止任务").is_enabled()
            record("FE-08", "真实浏览器＋慢速模拟模型", "运行中锁定输入并显示动态状态/耗时",
                   "不能输入/上传，显示活动提示、耗时和停止按钮", "运行中状态和控件限制均生效", shot(page, "12-running-state.png"))
            page.get_by_role("button", name="终止任务").click()
            page.locator(".notice").filter(has_text="本轮已终止").wait_for(timeout=15000)
            assert page.locator("#messageInput").is_enabled()
            conversation_id = page.url.split("conversation=")[-1]
            final = request(base, f"/api/conversations/{conversation_id}")
            assert final["turns"][-1]["status"] == "cancelled"
            assert not errors, errors
            record("FE-09", "真实浏览器＋慢速模拟模型", "手动停止后保留内容并恢复输入",
                   "状态 cancelled，无迟到完成；输入恢复", "页面显示已终止；API 状态 cancelled；输入可用", shot(page, "13-cancelled-state.png"))
        except Exception as error:
            fail("FE-CANCEL-UNEXPECTED", "真实浏览器＋慢速模拟模型", "取消流程中断", "运行中和取消用例完成", error, page)
        finally:
            page.close()


def real_outline_regression(browser):
    with server("real") as base:
        page, errors = new_page(browser)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#messageInput").wait_for(state="visible")
            page.wait_for_function("document.querySelector('#messageInput')?.disabled === false")
            page.locator("#messageInput").fill(
                "为一款产品问答助手设计整套评测方案：它从版本化产品知识库检索信息，"
                "回答用户关于功能、参数和兼容性的问题。请先给出可编辑大纲与产物清单，"
                "资料缺口作为假设写进大纲，等我确认再生成文件。"
            )
            page.locator("#sendButton").click()
            page.wait_for_function(
                "document.querySelector('.outline-card') && "
                "document.querySelector('#topStatus')?.textContent === '等待确认' && "
                "document.querySelector('.outline-card button.primary')?.disabled === false",
                timeout=180000,
            )
            cid = page.url.split("conversation=")[-1]
            snapshot = request(base, f"/api/conversations/{cid}")
            trace = request(base, f"/__test/outline-attempts/{cid}")
            turn = snapshot["turns"][-1]
            assert turn["status"] == "completed"
            assert len(snapshot["outlines"]) == 1 and snapshot["outlines"][0]["status"] == "pending"
            assert snapshot["artifacts"] == []
            assert trace["submit_attempts"] >= 1
            assert page.get_by_role("button", name="编辑大纲").is_enabled()
            assert not errors, errors
            elapsed = round((datetime.fromisoformat(snapshot["outlines"][0]["created_at"]) -
                             datetime.fromisoformat(turn["started_at"])).total_seconds(), 1)
            record("RM-OUTLINE-01", "真实浏览器＋真实模型", "真实模型大纲完成后立即可确认",
                   "大纲成功入库即结束规划轮次，按钮可用；记录耗时与重试次数",
                   f"大纲耗时 {elapsed} 秒，提交 {trace['submit_attempts']} 次、退回 "
                   f"{trace['rejected_attempts']} 次；编辑和确认按钮可用",
                   shot(page, "34-real-outline-immediate-confirm.png"))
        except Exception as error:
            fail("RM-OUTLINE-UNEXPECTED", "真实浏览器＋真实模型", "真实模型大纲即时确认失败",
                 "真实模型大纲完成后按钮立即可用", error, page)
        finally:
            page.close()


def fake_failure(browser):
    with server("failure") as base:
        page, errors = new_page(browser)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#history button").first.wait_for()
            page.locator("#messageInput").fill("触发隔离测试中的模型异常")
            page.locator("#sendButton").click()
            page.locator(".notice").filter(has_text="本轮未完成").wait_for(timeout=20000)
            conversation_id = page.url.split("conversation=")[-1]
            data = request(base, f"/api/conversations/{conversation_id}")
            assert data["turns"][-1]["status"] == "failed"
            assert page.locator("#messageInput").is_enabled()
            assert page.locator(".message.assistant .text").count() == 1
            assert not errors, errors
            record("FE-10", "真实浏览器＋故障模拟模型", "模型异常显示诚实失败状态并恢复输入",
                   "不能误报完成；有失败说明，允许继续对话", "状态 failed；失败说明和未完成提示可见；输入恢复", shot(page, "14-failed-state.png"))
        except Exception as error:
            fail("FE-FAILURE-UNEXPECTED", "真实浏览器＋故障模拟模型", "失败反馈流程中断", "模型异常得到可见说明", error, page)
        finally:
            page.close()


def real_model(browser):
    with server("real") as base:
        page, errors = new_page(browser)
        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("#history button").first.wait_for()
            page.locator("#messageInput").fill("请用一句中文概括你能提供的帮助，不要调用工具。")
            page.locator("#sendButton").click()
            wait_final(page, timeout=180000)
            cid = page.url.split("conversation=")[-1]
            data = request(base, f"/api/conversations/{cid}")
            assert data["turns"][-1]["status"] == "completed"
            assert data["messages"][-1]["body"].strip()
            assert not errors, errors
            record("RM-01", "真实模型＋真实浏览器", "本机配置模型真实问答",
                   "实际 API 完成并在页面显示回复，最终耗时可见", "真实模型完成对话且消息持久化", shot(page, "15-real-model-chat.png"))

            previous_id = cid
            page.locator("#newChat").click()
            page.wait_for_function("previous => new URL(location.href).searchParams.get('conversation') !== previous", arg=previous_id)
            page.locator("#messageInput").wait_for(state="visible")
            page.wait_for_function("document.querySelector('#messageInput')?.disabled === false")
            page.locator("#fileInput").set_input_files({"name": "合成需求.md", "mimeType": "text/markdown", "buffer": (
                "# 星河报销问答助手\n面向员工回答差旅报销流程问题。资料目前只有功能说明，没有制度原文。"
                "不得编造报销金额或时限。评测关注回答正确性、拒答边界和引用来源。\n"
            ).encode()})
            page.locator(".attachment-chip").wait_for()
            page.locator("#messageInput").fill("根据上传文档设计一整套评测方案。先提交大纲让我确认，产物清单控制在 2 个文件以内；缺失制度原文时标为待补。")
            page.locator("#sendButton").click()
            page.locator(".outline-card").wait_for(timeout=240000)
            cid = page.url.split("conversation=")[-1]
            plan = request(base, f"/api/conversations/{cid}")
            assert plan["artifacts"] == [] and plan["outlines"][0]["status"] == "pending"
            assert "产物清单" in plan["outlines"][0]["body"]
            record("RM-02", "真实模型＋真实浏览器", "上传资料后真实模型提交待确认大纲",
                   "确认前不得有产物；大纲含产物清单", "真实模型已提交可编辑大纲，正式产物为空", shot(page, "16-real-model-outline.png"))

            page.get_by_role("button", name="确认并开始 →").click()
            page.wait_for_function("document.querySelector('#topStatus')?.textContent === '本轮已完成' && !!document.querySelector('.deliverable-row')", timeout=420000)
            final = request(base, f"/api/conversations/{cid}")
            assert final["turns"][-1]["status"] == "completed"
            assert final["artifacts"]
            assert len(final["messages"][-1]["artifact_ids"]) == len(final["artifacts"])
            record("RM-03", "真实模型＋真实浏览器", "真实模型确认后生成整包并列出产物",
                   "全部计划文件完成，回复可点击，API 校验通过", f"真实模型发布 {len(final['artifacts'])} 个文件并关联完成回复", shot(page, "17-real-model-completed.png"))
            page.locator(".delivery-details summary").last.click()
            page.locator(".deliverable-row").first.click()
            page.locator(".preview-page").wait_for()
            record("RM-04", "真实模型＋真实浏览器", "真实产物从完成回复打开预览",
                   "点击文件直接打开只读大弹窗", "产物正文在页面弹窗显示", shot(page, "18-real-model-preview.png"))
        except Exception as error:
            fail("RM-UNEXPECTED", "真实模型＋真实浏览器", "真实模型流程中断", "真实问答、大纲、整包生成和预览完成", error, page)
        finally:
            page.close()


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real", action="store_true", help="Call the configured real model (may take minutes)")
    mode.add_argument("--real-outline", action="store_true", help="Only validate real-model outline timing and readiness")
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        launch = {"executable_path": str(chrome)} if chrome.exists() else {}
        browser = playwright.chromium.launch(headless=True, **launch)
        try:
            if args.real:
                real_model(browser)
            elif args.real_outline:
                real_outline_regression(browser)
            else:
                initialization_race(browser)
                fake_chat(browser)
                fake_edit_and_products(browser)
                fake_continue_after_cancel(browser)
                confirmed_outline_and_live_progress(browser)
                fake_workflow(browser)
                outline_is_confirmable_when_model_followup_is_slow(browser)
                outline_switch_race(browser)
                fake_cancel(browser)
                fake_failure(browser)
        finally:
            browser.close()
    destination = REPORT / ("real-model-results.json" if args.real else
                            "real-outline-results.json" if args.real_outline else
                            "isolated-browser-results.json")
    destination.write_text(json.dumps({"run_at": datetime.now(timezone.utc).isoformat(),
        "cases": RESULTS}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results: {destination}")
    raise SystemExit(1 if any(row["status"] == "FAIL" for row in RESULTS) else 0)


if __name__ == "__main__":
    main()
