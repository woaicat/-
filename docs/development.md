# 开发与验证

所有命令均在仓库根目录执行。生产代码位于 `src/evaluation_agent/`，不读取上级旧项目。

## 环境与启动

```bash
uv sync --locked
uv pip check
PYTHONPATH=src uv run --locked --env-file .env uvicorn evaluation_agent.main:app \
  --host 127.0.0.1 --port 8000 --reload --timeout-graceful-shutdown 3
```

首次启动按[README](../README.md)创建 `.env`；未配置模型时仍可查看工作台。前端为原生 HTML / CSS / JavaScript，由 FastAPI 同源提供，无需 `npm install`。首页不缓存，静态资源通过文件版本参数更新。

本地状态位于被 Git 忽略的 `storage/`。备份时先停止服务，再整体复制此目录，避免遗漏 SQLite 的 WAL 文件及附件、产物；不应把其中的数据作为测试夹具提交。

## 不调用供应商的回归

```bash
PYTHONPATH=src:tests uv run --locked python -m unittest discover -s tests -p 'test_*.py' -v
node --check frontend/app.js
git diff --check
```

`test_*.py` 使用临时数据与受控模型，验证业务权限、跨对话隔离、发布、上下文、取消及 API；它们不证明真实模型在任意输入下的语义质量。`node --check` 只做 JavaScript 语法检查，Node 不参与应用运行。

## 浏览器验收

浏览器脚本使用独立的 headless 浏览器，不复用个人登录状态。当前依赖本机 Python 的 Playwright；部分专项脚本仍固定 macOS Chrome 路径，开发依赖尚未统一锁定，因此这些脚本尚不具备全部跨平台执行条件。

- `python3 tests/browser_ui_smoke.py`：先启动本地服务；脚本拦截 API 注入合成响应，不向正式对话写测试消息。可用 `EVAL_AGENT_BROWSER_BASE_URL` 指向另一个本地端口。
- `python3 tests/functional_browser_acceptance.py`：临时目录、随机端口与受控模型，实际经过 HTTP / SQLite / SSE；默认不读取 `.env`。
- `python3 tests/context_browser_acceptance.py`、`python3 tests/rename_cancel_acceptance.py`：上下文与改名/取消专项，同样使用临时服务和受控模型。
- `python3 tests/desktop_ui_acceptance.py --run 1`：桌面 UI 专项包含与历史提交 `bdbff1a` 的对比，需保留该 Git 历史；不是浅克隆或源码 ZIP 的通用入口。

部分历史验收脚本将结果写入已有日期目录，复测前应另存原结果并检查 `git diff`，避免将新结果误当成历史证据；后续可统一为显式输出目录。有关截图和断言范围见[文档导航](README.md)。

## 真实模型验收（显式开启）

```bash
PYTHONPATH=src uv run --locked --env-file .env python tests/clarification_real_acceptance.py \
  --real --output /tmp/evaluation-agent-clarification-results.json
```

该专项使用配置的真实模型，可能产生费用；使用合成需求、临时工作区并关闭 LangSmith 追踪，不确认大纲、不发布交付文件。可追加 `--cases sparse unknown` 单独检查简短需求与未知回答。

完整流程历史脚本也有 `--real` 模式，开启前请阅读脚本内容；不要把使用受控模型的结果当成真实模型行为验证。

## 变更范围

提交前复核自己的文件与测试结果，不暂存 `.env`、`storage/` 或供应商密钥。界面规则维护在 [design/工作台界面规范.md](design/工作台界面规范.md)，历史设计在 `architecture/`，验收证据按主题与日期保留。运行行为与接口改变时，同步维护 README、当前架构说明或对应专项说明。
