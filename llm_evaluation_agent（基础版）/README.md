# LLM Evaluation Agent (LangGraph Edition)

本仓库实现了一个“本地可部署”的大模型评测方案设计智能体。后端使用 **FastAPI + LangGraph** 驱动一个可恢复的 Planner→Executor→Tool→Reflection QA→Reflection→Summary 状态机，前端是原生 JavaScript 写成的时间轴页面，通过 SSE 实时获取运行节点、产物和知识命中信息。系统支持用户上传 Markdown/Word 需求文档、可选填写产品要素，Agent 会自动规划执行、生成评测方案、数据集规范、样本 CSV、方法指南等文件并写入本地目录。

## 项目结构速览

| 目录 | 说明 |
| --- | --- |
| `app/` | FastAPI 应用及 LangGraph 编排（核心逻辑、工具、配置、节点定义、文档解析、向量检索等）。 |
| `frontend/` | 纯静态前端（`index.html + main.js + styles.css`），使用原生 fetch 流式处理 SSE，并提供暂停/重连、时间轴、文件列表、摘要等 UI。 |
| `scripts/` | `dev_server.py`（自动重载但排除 outputs/uploads），`sample_request.py`（命令行调用 `/generate-plan` 并下载产物）。 |
| `storage/` | 长期状态：`storage/checkpoints/agent_state.sqlite`（LangGraph `AsyncSqliteSaver`）与 `storage/chroma/`（Chroma + Doubao 向量检索）。 |
| `outputs/` | 各任务产物目录（`{product}_{request_id}`）与 `manifest.json`，`app/file_manager.py` 负责清理过期目录。 |
| `uploads/` | 上传文档暂存目录，上传解析完成后会清理临时文件并把源文件复制到对应输出目录。 |
| `tests/` | Pytest 用例，覆盖 API、TaskState、文件名清洗、Agent 步骤等。 |

## 后端架构

### FastAPI 应用（`app/main.py`）

- 路由：
  - `POST /generate-plan`：接收 JSON 或 multipart/form-data（含 `payload` 与可选 `document`）。支持 `X-API-Key` 或 `?api_key=` 鉴权。
  - `GET /events/{request_id}`：返回 SSE 流（文本 event-stream）。`TaskState` 在内存中缓存最近 10 分钟的任务事件，支持断线续传。
  - `GET /manifest/{request_id}`：优先读取内存中的 manifest，否则从磁盘输出目录加载（即使进程重启也可取历史产物）。
  - `GET /download/{request_id}/{filename}` / `GET /preview/...`：下载或在线预览 Word/CSV/文本（`_render_preview_*` 支持 docx 段落与表格渲染）。
  - `GET /demo-events`：使用 `langgraph_demo.py` 的演示图生成一批 SSE payload，方便前端联调。
  - `GET /health`、`GET /`（静态前端）。
- `TaskState`：记录事件、监听队列、manifest、错误、人类友好的 `finished_at`。配合 `TASK_RETENTION_SECONDS = 600` 实现任务状态保留机制，避免 SSE 重连 404。
- 文档上传：仅允许 `.md/.markdown/.docx`，最大 50MB。Word 会通过 `_convert_docx_to_markdown` 转 markdown 后送入 `DocumentAnalyzer`。
- 文档解析：`DocumentAnalyzer` 将 markdown 按段落切片（最多 10 * 8000 chars），并行调用 LLM 梳理要点，再抽取结构化信息（场景/用户/核心功能/工作流等）。
- 合并上下文：`_merge_form_and_document` 将表单与文档提取信息去重合并，优先保留有内容的字段。
- 事件流：所有 LangGraph 节点 emit 的 `StreamEvent` 都被编码成 `data: {json}\n\n` 推送，前端解析成时间轴、Ticker、Summary 等。
- 输出清理：`clean_expired_outputs` 根据 `MAX_OUTPUT_AGE_HOURS` 定期删除旧目录（应用启动时及每次任务完成都会触发）。

### LangGraph 工作流（`app/langgraph_runner.py`）

- `LangGraphAgentRunner` 使用 `StateGraph` 构建 Planner → Executor → Tool → Reflection QA → Reflection → Summary → END 的 DAG，结合 `AsyncSqliteSaver`（`aiosqlite`）实现 checkpoint 持久化，可追踪/恢复 `queue`、`executed`、`reflections` 等状态。Reflection QA 节点会调用 `REFLECTION_PROMPT` 对每个步骤产物质检，可根据 LLM 输出决定 `redo/extend/continue` 并自动追加补救步骤。
- `planner` 节点：调用 `PlanGenerator.create_plan`（LLM + JSON 解析/修复）生成 `PlanOutline`，会将步骤和摘要写入 state 并 emit planning 事件。
- `executor` 节点：从队列取 `StepEntry`，调用 `ContextManager.enrich_context` 合并当前步骤、向量检索命中（`vector_notes`）、历史 insight 等，再进入 Tool 节点。
- `tool_executor`：调用 `PlanGenerationAgent.execute_step` 执行各步骤 handler，所有产物通过 `ToolService` 写入输出目录（Word/CSV/文本），并 emit 执行情报。
- `reflection`：根据执行结果决定 `retry/continue/done`，不足则重回队列；成功则记录 `executed` 与 `reflections`。
- `summary`：调用 `PlanGenerationAgent.generate_final_summary` 生成最终总结，用于 `/events` 的 `final_summary` 节点。

### Agent 组件

- `PlanGenerationAgent`：封装 LangChain `ChatOpenAI`，提供四个核心 handler：
  - `product_analysis`、`plan_design`、`data_delivery`、`execution_manual`。
  - 每个 handler 可输出多个 artefact（产品解析、评测方案、数据集规范、多张样本表、执行手册、提示词模板、自动化脚本等），并生成 `insight` 供 `ContextManager` 入库。
  - 失败会按 `llm_max_retries` 重试 LLM 调用，Step 级别允许额外 3 次。
  - 所有步骤会记录结构化 metadata，供前端展示。
- `PlanGenerator`：保障 planner 返回 JSON，若 LLM 输出格式错误会尝试截取 JSON 片段或调用 `PLAN_REPAIR_PROMPT` 重新修复。
- `ContextManager`：将上传文档与步骤结果写入 `VectorService`，执行前再检索 3 条命中并注入 `vector_notes`。若未配置 EMBEDDING_*，vector 功能会自动降级（不会抛错）。
- `VectorService`：包装 `chromadb.PersistentClient` + `DoubaoEmbeddingFunction`，兼容 `chromadb>=0.5` 新接口；将内容按 `request_id` + `step_id` 存储，可根据 request_id 做 where 过滤。
- `ToolService`：负责输出目录创建、文件登记（download/preview URL）、CSV/文本/Word 写入。`build_manifest` 会在任务结束后落盘 `manifest.json`，并为 `/manifest` 返回带 preview_url 的 `GeneratedFile`。

## 前端交互（`frontend/`）

- 完整静态资源（无需构建）：`index.html` + `styles.css` + `main.js`，通过 FastAPI `/static` 提供。
- 表单字段：产品名称（必填）、约束、API Token、可选文档上传。`collectFormData()` 当前聚焦核心字段，后续可扩展更多输入项。
- 事件流：使用 `fetch` + `ReadableStreamDefaultReader` 解析 SSE，配合 `appendAndParseSseBuffer` 手动拆包，可避免浏览器原生 `EventSource` 对自定义 header/暂停的限制。
- 断线恢复：指数退避（最多 5 次），支持“暂停/继续”按钮，暂停后会取消 SSE 请求并保留现有日志；继续时重新连接并复用在内存中缓存的历史事件。
- 时间轴：展示 Planner 输出的步骤卡片、执行状态、重试情况、知识命中、产物链接、摘要等；Ticker 列展示最近事件。
- Manifest：在收到 `finalize` 事件后主动调用 `/manifest/{request_id}`，并渲染文件列表（download / preview 带上 API Token）。
- 调试：可直接访问 `/demo-events` 看静态演示流；`timelineTicker` 默认提示“等待执行”。

## API & 运行路径

| Endpoint | 描述 |
| --- | --- |
| `POST /generate-plan` | 提交任务（JSON 或 multipart），返回 `request_id`。 |
| `GET /events/{request_id}` | SSE 事件流（需要 `Accept: text/event-stream`），自动推送历史 + 新事件。 |
| `GET /manifest/{request_id}` | 返回 `Manifest`，在任务执行中会返回 202。 |
| `GET /download/{request_id}/{filename}` | 下载产物。 |
| `GET /preview/{request_id}/{filename}` | Word/CSV/文本在线预览，内联下载链接支持透传 `api_key`。 |
| `GET /demo-events` | LangGraph demo SSE 流。 |
| `GET /health` | 健康检查。 |

### 存储 & 保留策略

- `outputs/{product}_{request_id}/`：任务产物目录，包含上传源文件副本、生成 artefacts、`manifest.json`、`context_snapshot.json`。
- `uploads/`：上传文件暂存，解析结束后删除；异常会清理残留。
- `storage/checkpoints/agent_state.sqlite`：LangGraph Async checkpoint，允许重新启动后恢复流程。
- `storage/chroma/`：Chroma 数据目录；未配置嵌入时不会创建。
- `TASK_RETENTION_SECONDS=600`：内存任务状态保留时间；`MAX_OUTPUT_AGE_HOURS` 控制磁盘清理（默认 6 小时）。

## 环境依赖与配置

1. **Python 版本**：建议 3.10+。
2. **依赖安装**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. **环境变量（`.env`）**
   ```env
   OPENAI_API_KEY=your-chat-api-key
   OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4   # 可指定豆包/自建模型
   MODEL_NAME=doubao-pro-32k

   EMBEDDING_API_KEY=your-embedding-key                   # 可为空：为空则禁用向量检索
   EMBEDDING_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
   EMBEDDING_MODEL=doubao-embedding-text-240715

   OUTPUTS_DIR=./outputs
   UPLOADS_DIR=./uploads
   MAX_OUTPUT_AGE_HOURS=6
   API_TOKEN=optional-token                               # 为空则开放访问
   LLM_MAX_RETRIES=2                                      # LLM 层自动重试次数
   LLM_RETRY_DELAY=2.0
   ENABLE_REFLECTION_QA=true                              # 是否启用执行后质检

   # LangSmith（可选，开启观测/调试）
   LANGSMITH_API_KEY=your-langsmith-key
   LANGSMITH_PROJECT=llm-eval-local
   # LANGSMITH_ENDPOINT=https://api.smith.langchain.com   # 如需自建 endpoint
   ```

## 运行方式

### 后端开发/部署

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或使用包含热重载的脚本
python scripts/dev_server.py
```

服务启动后访问 `http://localhost:8000/`，在表单中填入产品名称（必填）、可选 API Token 与需求文档，即可发起任务。上传 `.docx` 会自动转换成 markdown 并抽取结构化信息；上传内容及每步产物都会写入向量库（若启用嵌入）。

### 前端调试

前端是纯静态资源，无需构建；如需跑 Vitest 或 lint，可在 `frontend/` 下执行：

```bash
cd frontend
npm install
npm run test   # 当前没有测试文件，会提示 No test files found
```

### CLI 示例

`scripts/sample_request.py` 演示如何从命令行调用：

```bash
python scripts/sample_request.py
```

脚本会：
1. 构造示例 payload 提交 `/generate-plan`；
2. 轮询 `/manifest/{request_id}`；
3. 将产物下载到 `sample_outputs/{request_id}/`。

## 测试

```bash
pytest
```

主要用例：
- `tests/test_api_generate_plan.py`：验证 `/generate-plan` + `/manifest` 的端到端行为（包含文件名清洗、manifest availability）。
- `tests/test_file_manager.py`：确保 `sanitize_filename`、CSV/Word 写入与清理逻辑安全。
- `tests/test_agent_dynamic.py`：覆盖 Agent 步骤执行/重试逻辑。
- `tests/test_task_state.py`：验证 `TaskState` 事件缓存、监听器与清理机制。

## 调试与辅助工具

- `/demo-events`：回放 LangGraph demo 事件，前端可在真正任务前验证 SSE UI。
- `context_snapshot.json`：每次任务开始会持久化请求与上下文（`outputs/{product}_{request_id}/`），方便追溯输入。
- `preview` 页面：支持 docx/CSV/文本在线预览，便于联调 SSE + 产物下载体验。
- LangSmith：设置 `LANGSMITH_API_KEY` 与 `LANGSMITH_PROJECT` 后，系统会为每个任务自动生成 trace，并携带 `request_id`/`product_name` 元数据。
- `storage/checkpoints/`：若需要重置状态，可删除该目录（会清空 LangGraph checkpoint）。

## 后续可拓展方向

- 在 LangSmith / LangFuse 等观测平台注册 `LangGraphAgentRunner` 事件，增强可观测性与调试体验。
- 针对 >10 分钟长任务进一步打磨 SSE 自动重连策略与前端状态展示。
- 增加更多工具节点（HTTP 调用、Excel 输出、外部数据库写入等）与更细粒度的单元/集成测试。
