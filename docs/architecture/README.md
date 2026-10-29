# 当前技术架构

![技术架构图](../assets/architecture.svg)

[SVG 源图](../assets/architecture.svg) · [PNG 图片](../assets/architecture.png)

该图描述当前代码：单个 Deep Agents 智能体在本地 FastAPI 进程中运行，模型与可选 LangSmith 位于外部服务；用户通过浏览器访问同源工作台。

## 模块映射

| 模块 | 实现 | 职责 |
| --- | --- | --- |
| 对话工作台 | [`frontend/`](../../frontend/) | 消息与附件、大纲确认、文件预览下载、流式状态。 |
| HTTP / SSE 服务 | [`api.py`](../../src/evaluation_agent/api.py) | 会话与文件接口、确认和取消、快照与持久化事件回放。 |
| 轮次执行器 | [`executor.py`](../../src/evaluation_agent/executor.py) | 后台任务生命周期、事件投影、交付验收和有界修订。 |
| 单智能体内核 | [`runtime.py`](../../src/evaluation_agent/runtime.py)、[`prompts.py`](../../src/evaluation_agent/prompts.py) | 模型与工具循环、需求澄清规则、上下文预算与自动摘要。 |
| 业务工具 | [`product_tools.py`](../../src/evaluation_agent/product_tools.py) | 选择产品与任务范围、提交大纲、发布与改名。 |
| 文件作用域 | [`scoped_backend.py`](../../src/evaluation_agent/scoped_backend.py) | 当前对话可读资料投影与本轮草稿写入限制。 |
| 应用存储与发布 | [`store.py`](../../src/evaluation_agent/store.py)、[`artifacts.py`](../../src/evaluation_agent/artifacts.py) | 应用数据库、资源归属、发布事务与 MD / CSV / DOCX 文件。 |
| 外部服务适配 | [`model_config.py`](../../src/evaluation_agent/model_config.py)、[`observability.py`](../../src/evaluation_agent/observability.py) | OpenAI 兼容模型配置，以及可选 LangSmith tracing。 |

## 运行链路

1. 用户发送消息，应用先保存用户消息和轮次，再由执行器启动后台任务；浏览器通过 SSE 接收可回放的增量事件。
2. 同一对话使用稳定的 `thread_id` 读取 LangGraph checkpoint，智能体结合历史、产品索引与已读取附件决定追问、规划或修改。
3. 整包请求必须先提交大纲，用户确认后才创建具有交付范围的新轮次；此限制由应用权限控制，不能仅靠提示词绕过。
4. 智能体通过通用文件工具写草稿，通过业务工具发布正式文件；应用完成格式、归属、清单及显式引用检查后形成交付摘要。
5. 取消、断线或进程重启后，已发布文件仍保留；后续新轮次读取现有交付继续执行，不保证从任意中断指令原地恢复。

## 两类工具与三类持久化

- **6 个文件工具**：`ls`、`read_file`、`glob`、`grep`、`write_file`、`edit_file`；模型写入仅限 `/drafts/<turn_id>/`。
- **4 个业务工具**：`select_evaluation`、`submit_outline`、`publish_artifact`、`rename_artifacts`；`discussion` 是任务范围参数，不是独立工具。
- **`storage/workspace.sqlite`**：消息、轮次、产品、大纲、交付项、文件索引与事件；这是工作台状态依据。
- **`storage/checkpoints.sqlite`**：智能体消息与图状态，含虚拟草稿和摘要上下文；不直接作为前端消息数据库。
- **`storage/uploads/`、`storage/outputs/`**：原始附件、提取文本、正式产物源与导出文件。

LangSmith 按 `thread_id` 关联同一对话；每轮有一个根 trace，模型与工具调用嵌套其中。它提供执行记录，不参与本地业务状态的恢复。图中的虚线表示可选追踪，实线表示交互、调用或持久化关系。

## 范围

当前不是多智能体系统；未接入向量数据库、MCP、Skill 或任意代码执行沙箱。SQLite 和本地文件适合当前本地单用户范围，不能直接视为多实例或多租户部署方案。

历史设计见[后端方案](后端与智能体服务端技术方案.md)、[运行内核方案](智能体服务端技术方案.md)与[重构计划](重构计划.md)。
