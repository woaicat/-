"""Render the local, self-contained functional acceptance report from saved evidence."""

import html
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "docs" / "功能测试报告-20261025"


def read_json(filename):
    path = FOLDER / filename
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"cases": []}


def escape(value):
    return html.escape(str(value), quote=True)


def model_name():
    path = ROOT / ".env"
    if not path.exists():
        return "未配置"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MODEL_NAME="):
            return line.split("=", 1)[1].strip().strip('"\'')
    return "未配置"


def screenshot_link(row):
    name = row.get("screenshot")
    if not name:
        return "API 断言 / 测试日志"
    return f'<a href="{escape(name)}">查看截图</a>'


def case_rows(cases):
    return "\n".join(
        f'<tr><td><code>{escape(row["id"])}</code></td><td>{escape(row["category"])}</td>'
        f'<td>{escape(row["title"])}</td><td>{escape(row["expected"])}</td>'
        f'<td>{escape(row["actual"])}</td><td><span class="status {escape(row["status"].lower())}">'
        f'{escape(row["status"])}</span></td><td>{screenshot_link(row)}</td></tr>'
        for row in cases
    )


def gallery(cases):
    cards = []
    for row in cases:
        path = row.get("screenshot")
        if not path:
            continue
        cards.append(f'<figure><a href="{escape(path)}"><img src="{escape(path)}" '
                     f'alt="{escape(row["title"])}"></a>'
                     f'<figcaption><b>{escape(row["id"])} · {escape(row["title"])}</b>'
                     f'<span>{escape(row["actual"])}</span></figcaption></figure>')
    offline = "screenshots/29-browser-offline-during-model-stream.png"
    if (FOLDER / offline).is_file():
        cards.append(f'<figure><a href="{offline}"><img src="{offline}" '
                     'alt="浏览器离线时真实模型仍在服务端运行"></a>'
                     '<figcaption><b>SP-02B · 离线过程截图</b>'
                     '<span>浏览器新请求被阻断期间，服务端任务继续执行。</span></figcaption></figure>')
    return "\n".join(cards)


def main():
    simulated = read_json("isolated-browser-results.json")["cases"]
    real = read_json("real-model-results.json")["cases"]
    special = read_json("special-results.json")["cases"]
    special_real = read_json("special-real-results.json")["cases"]
    real_outline = read_json("real-outline-results.json")["cases"]
    cases = simulated + real + special + special_real + real_outline
    unit_log = FOLDER / "unit-tests.txt"
    unit_count = 0
    if unit_log.exists():
        found = re.search(r"Ran (\d+) tests", unit_log.read_text(encoding="utf-8"))
        unit_count = int(found.group(1)) if found else 0
    passed = sum(row["status"] == "PASS" for row in cases)
    failed = sum(row["status"] == "FAIL" for row in cases)
    blocked = sum(row["status"] == "BLOCKED" for row in cases)
    revision = subprocess.check_output(
        ["git", "log", "-1", "--format=%h", "--", "frontend", "src/evaluation_agent"],
        cwd=ROOT, text=True,
    ).strip()
    html_text = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>对话式评测智能体 · 功能测试报告</title>
<style>
:root {{color-scheme:light;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft Yahei",sans-serif;color:#273950;background:#eef3fa}}
*{{box-sizing:border-box}}body{{margin:0}}main{{max-width:1500px;margin:auto;padding:38px 28px 100px}}
.hero{{background:#172c4d;color:white;border-radius:24px;padding:36px 40px;box-shadow:0 18px 46px #132c5020}}
.hero small{{letter-spacing:.08em;color:#a9c6f5}}h1{{font-size:34px;margin:12px 0}}h2{{font-size:23px;margin:0 0 16px}}h3{{font-size:17px}}
.hero p{{color:#d2e0f3;line-height:1.8;max-width:1000px}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-top:28px}}
.metric{{background:#ffffff16;border:1px solid #ffffff2a;border-radius:14px;padding:16px}}.metric b{{display:block;font-size:27px}}.metric span{{color:#c5d8f2}}
section{{background:white;margin-top:22px;border-radius:20px;padding:26px 30px;box-shadow:0 8px 28px #19385b0b}}
p,li{{line-height:1.75}}ul,ol{{padding-left:23px}}.subtle{{color:#6e7f96}}.note{{background:#edf5ff;border-left:4px solid #2772eb;padding:14px 18px;border-radius:8px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{border-bottom:1px solid #e4eaf2;text-align:left;vertical-align:top;padding:12px 9px;line-height:1.6}}th{{background:#f3f7fc;position:sticky;top:0}}.wide{{overflow:auto;max-height:780px;border:1px solid #e4eaf2;border-radius:10px}}
.status{{font-weight:700;border-radius:99px;padding:3px 8px}}.status.pass{{color:#176347;background:#def5e9}}.status.fail{{color:#a62e2e;background:#ffe7e7}}.status.blocked{{color:#8a6100;background:#fff2cb}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:17px}}figure{{border:1px solid #dce5f2;border-radius:14px;overflow:hidden;margin:0;background:#f8fbff}}
figure img{{display:block;width:100%;height:280px;object-fit:contain;background:#e9eef6}}figcaption{{display:grid;gap:7px;padding:13px 15px;font-size:13px;line-height:1.5}}figcaption span{{color:#64758c}}
a{{color:#1e68d7}}code{{background:#eff4fb;border-radius:4px;padding:2px 5px}}.timeline li{{margin-bottom:8px}}
@media(max-width:750px){{main{{padding:12px}}.hero,section{{padding:20px}}.metrics{{grid-template-columns:repeat(2,1fr)}}h1{{font-size:27px}}}}
</style></head><body><main>
<div class="hero"><small>独立环境 · 可复查证据 · 更新于 2026-10-26</small><h1>对话式评测智能体功能测试报告</h1>
<p>本轮以隔离临时数据目录和独立随机端口测试，不访问用户正在使用的 8000 端口数据。浏览器用 Chrome {escape('155.0.8059.85')}；后端用项目锁定的 Python 3.12 环境。真实模型名称：<b>{escape(model_name())}</b>。密钥及服务地址不写入报告。</p>
<div class="metrics"><div class="metric"><b>{unit_count}</b><span>后端单元/API 测试</span></div><div class="metric"><b>{passed}</b><span>浏览器/模型用例通过</span></div><div class="metric"><b>{failed}</b><span>当前失败</span></div><div class="metric"><b>{blocked}</b><span>阻塞用例</span></div></div></div>
<section><h2>结论与判定范围</h2>
<p>本次基于产品代码版本 <code>{escape(revision)}</code>。完整用例表包含每项期望、实际结果与截图。前端测试分为真实浏览器＋真实 HTTP＋模拟模型、真实浏览器＋真实模型两类；后端契约测试在 TestClient/临时 SQLite 中执行。模拟模型测试验证产品流程和事件契约，不代表所接模型对任意需求的质量。</p>
<p>本次界面改动重新执行了 {len(simulated)} 项隔离浏览器用例和 {unit_count} 项单元/API 测试；其余 {len(real) + len(special) + len(special_real) + len(real_outline)} 项真实模型及专项结果是此前保存的验收记录，列入累计结果，本次没有重新调用真实模型生成整包。</p>
<p class="note">累计发现八个应用缺陷。此次修复两项：确认后大纲仍保存在后端，但前端只显示待确认状态，导致卡片消失；生成期间后端已有交付项状态和文件就绪事件，但聊天区没有进度组件。现在已确认大纲作为只读卡片留在原对话位置，生成轮次下方按真实交付项展示已完成数、待办文件和可点击的已完成文件。进度随文件发布事件更新，刷新后可恢复。此前修复的大纲解析和确认等待问题也已回归。真实模型一次大纲测试耗时 38.5 秒、提交 1 次；这是单次样本，不代表稳定时延。</p>
<p>四项专项验收已执行：缩小测试窗口触发真实模型摘要并验证信息找回；真实模型流式输出途中手动终止，以及浏览器离线后恢复网络并刷新；首个文件发布后从页面点击终止，再在同一对话续做；强制结束服务进程后重启并恢复未完成文件。另在受控 SSE 断流后验证了原页面自动重连与消息补齐。<b>边界：</b>压缩测试用 12,000 token 测试窗口与 2,200 token 触发值，未实际填满生产 128,000/102,400 配置；仅对照了一次直接请求的近似计数和供应商精确输入用量，未逐次核对完整智能体请求。真实物理断网后不刷新页面时的自动重连仍未单独证明。压力并发、恶意 DOCX 与多人跨设备不计入本轮通过结论。</p>
<p class="note">计数观察：同一条中文直接请求，现有近似计数为 164 tokens，供应商报告输入 455 tokens。这是一次样本，提示近似计数可能低估；不能据此断定完整智能体请求的偏差比例。生产阈值仍需按实际模型用量验证。</p></section>
<section><h2>测试方法与复现</h2><ol>
<li>项目后端与 API：<code>PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -v</code>；完整日志 <a href="unit-tests.txt">unit-tests.txt</a>。</li>
<li>组件级浏览器模拟：<code>EVAL_AGENT_BROWSER_BASE_URL=http://127.0.0.1:&lt;隔离端口&gt; python3 tests/browser_ui_smoke.py</code>；运行记录 <a href="browser-smoke-output.txt">browser-smoke-output.txt</a>。</li>
<li>真实 HTTP/浏览器：<code>python3 tests/functional_browser_acceptance.py</code>。脚本自动启动临时服务，端口随机分配；结果 <a href="isolated-browser-results.json">isolated-browser-results.json</a>。</li>
<li>真实模型：<code>python3 tests/functional_browser_acceptance.py --real</code>；只使用本项目本地 <code>.env</code>，结束后删除隔离运行数据；结果 <a href="real-model-results.json">real-model-results.json</a>。</li>
<li>真实模型大纲专项回归：<code>python3 tests/functional_browser_acceptance.py --real-outline</code>；记录大纲耗时、工具提交/退回次数，以及卡片按钮状态；结果 <a href="real-outline-results.json">real-outline-results.json</a>。</li>
<li>终止续做与崩溃恢复：<code>python3 tests/special_acceptance.py</code>；结果 <a href="special-results.json">special-results.json</a>。</li>
<li>真实模型压缩、终止与离线恢复：<code>python3 tests/special_acceptance.py --real</code>；结果 <a href="special-real-results.json">special-real-results.json</a>。</li>
</ol><p>所有详情在 <a href="测试用例.md">测试用例.md</a>。浏览器断言同时监听 JavaScript 页面异常；截图来自断言成功后的同一浏览器窗口，没有替换成设计稿。</p></section>
<section><h2>逐项结果</h2><div class="wide"><table><thead><tr><th>ID</th><th>类型</th><th>用例</th><th>期望</th><th>实际</th><th>结论</th><th>证据</th></tr></thead><tbody>{case_rows(cases)}</tbody></table></div></section>
<section><h2>截图证据</h2><p class="subtle">点击图片查看原始分辨率。包括初始锁定、跨会话草稿、正常完成回复、产物预览、运行中、取消、故障及真实模型流程。</p><div class="gallery">{gallery(cases)}</div></section>
<section><h2>缺陷与回归记录</h2><div class="wide"><table><thead><tr><th>编号</th><th>首次复现</th><th>影响</th><th>修复后回归</th></tr></thead><tbody>
<tr><td>BUG-01</td><td>延迟初始创建请求时，发送调用 <code>/api/conversations/null/messages</code>；新会话创建后草稿消失，提示却称已保留。</td><td>首条消息丢失；错误反馈与实际不符。</td><td>FE-INIT-01/02：初始化控件锁定，完成后发到真实 ID。</td></tr>
<tr><td>BUG-02</td><td>A 大纲 PUT 延迟时切换到 B，旧回调向 <code>/conversations/B/outlines/A-id/confirm</code> 发 POST，提示 Outline not found。</td><td>用户确认未生效，跨会话操作混淆。</td><td>FE-OUTLINE-01：确认请求固定 A，B 页面不被覆盖。</td></tr>
<tr><td>BUG-03</td><td>大纲生成完成时顶部显示“等待确认”，编辑和确认按钮持续 disabled。</td><td>整包流程无法继续。</td><td>AG-01～04：按钮恢复、编辑、确认、产物和刷新全链路。</td></tr>
<tr><td>BUG-04</td><td>真实模型触发内部摘要时，流式模型返回 AsyncStream，摘要轮次失败。</td><td>长对话到压缩阈值会中断。</td><td>SP-01：摘要改为非流式调用；真实模型压缩后完成轮次并找回早期代号。</td></tr>
<tr><td>BUG-05</td><td>真实模型用带编号的“交付文件清单”标题、带说明的列表和不支持的扩展名时，产物清单被反复退回。</td><td>大纲生成出现多次工具重试；用户等待明显变长。</td><td>单测覆盖标题、表格、序号、文件类型边界；RM-OUTLINE-01 真实模型提交 1 次、退回 0 次。</td></tr>
<tr><td>BUG-06</td><td><code>outline.ready</code> 发出后，规划轮次仍等待模型追加回复，导致按钮继续禁用。</td><td>卡片虽已出现，用户仍无法立即编辑和确认。</td><td>AG-OUTLINE-READY-01/02 用 10 秒慢模型验证轮次立即结束、卡片可编辑并确认；RM-OUTLINE-01 真实模型复核。</td></tr>
<tr><td>BUG-07</td><td>前端只渲染 <code>pending</code> 大纲，确认后持久化的 <code>confirmed</code> 大纲被过滤。</td><td>用户在聊天中看不到已确认的生成依据。</td><td>AG-PROGRESS-01/03：确认后的大纲只读保留在原位置，刷新与任务完成后仍可见。</td></tr>
<tr><td>BUG-08</td><td>生成轮次没有把交付项状态与 <code>artifact.ready</code> 事件呈现在聊天区。</td><td>用户在等待文件时只看见计时和轮播提示。</td><td>AG-PROGRESS-01/02：1/2 → 2/2 实时更新，已完成文件可直接预览；运行中刷新后恢复。</td></tr>
</tbody></table></div></section>
<section><h2>后端与智能体覆盖摘要</h2><p>本次 {unit_count} 项单元/API 测试覆盖会话/消息/检查点隔离、上传解析与幂等、SSE 重放与游标、任务取消、异常恢复、整包大纲确认门槛、发布权限与文件引用、局部修改版本、上下文预算和内部摘要不外泄。新增解析格式与大纲入库即结束规划轮次的回归测试；详情及预期见测试用例文档和单测日志。</p></section>
</main></body></html>'''
    target = FOLDER / "功能测试报告.html"
    target.write_text(html_text, encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
