# LangSmith 接入指南

本项目可选接入 LangSmith 托管观测服务，智能体仍在本机运行。界面和额度以自己的账号为准；本接入无需部署智能体。

## 第一次配置

1. 登录 [LangSmith](https://smith.langchain.com)。按自己的账号区域选择 API endpoint，模板使用美国区地址。
2. 在左侧 **All agents** 点击 **+ Agent → Trace an existing agent**。创建名称和 Agent ID 都为 `evaluation-agent` 的观测入口；这一步只登记已有智能体，不部署智能体。
3. 在 **Settings → API Keys** 创建密钥。如可选择，使用 **Tracing Only**，选择自己的工作区，名称填 `evaluation-agent`。创建后立即复制密钥，它只展示一次。
4. 打开本项目 `.env`，保留已有模型配置，在文件末尾填写：

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=在这里粘贴自己的LangSmith密钥
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_AGENT_ID=evaluation-agent
LANGSMITH_AGENT_ENVIRONMENT=local
LANGSMITH_WORKSPACE_ID=
```

密钥仅保存在被 Git 忽略的 `.env`，不要发送到聊天或写进前端代码。只有密钥关联多个工作区时才需要填写 `LANGSMITH_WORKSPACE_ID`，可从 Settings → General 获取。

新版 Agents 工作区使用 `LANGSMITH_AGENT_ID` 和 `LANGSMITH_AGENT_ENVIRONMENT`，不要同时配置 `LANGSMITH_PROJECT`、`LANGCHAIN_PROJECT` 或 `LANGCHAIN_SESSION`。旧版 Projects 工作区可改用 `LANGSMITH_PROJECT=evaluation-agent-v2`，并移除两个 Agent 配置项。

## 启动并查看记录

在项目根目录启动或重启服务，让进程重新加载 `.env`：

```bash
PYTHONPATH=src uv run --env-file .env uvicorn evaluation_agent.main:app --reload --timeout-graceful-shutdown 3
```

在工作台新建一个测试对话，发送“请创建一份简单的准确率评测说明.md”。在 LangSmith 选择 **evaluation-agent → Local → Tracing**，打开最新的“评测方案智能体 · 对话轮次”记录。

一条根记录覆盖本轮执行、服务端要求的修订和最终回复。展开可以查看 Deep Agents 的模型调用、文件工具与业务工具调用。`conversation_id`、`turn_id` 和 `thread_id` 用于关联本地对话与轮次；同一对话的多轮可按 thread 查看。根记录输出包含完成状态和用户可见回复，失败或取消也会记录相应状态。

开启追踪会将模型输入、输出和工具参数等运行信息发送到自己的 LangSmith 工作区，文件工具读写的内容也可能出现在记录中。关闭时将 `LANGSMITH_TRACING=false` 并重启。

## 不调用模型的连接检查

```bash
PYTHONPATH=src uv run --env-file .env python scripts/check_langsmith.py
```

脚本只上传一条固定文本的测试记录，不读取业务对话和附件，不调用模型，不输出密钥。显示“测试记录已提交”表示上传接口接受了测试记录；还需要在平台 Local 环境看到这条记录，才能确认端到端连接。

如果没有记录，依次检查：开关是否为 true、密钥是否有效、账号与 endpoint 是否同区、Agent 是否已登记、Agent ID 是否与平台一致，以及目标环境是否为 Local。创建密钥失败或没有 Tracing Only 时按当前账号 UI 提供的权限选项操作。

## 免费使用边界

Developer 套餐每月含 5,000 条 base traces，保留 14 天；实际额度以账号 Billing and Usage 为准。本接入不会创建付费部署或修改账单设置，模型供应商费用仍按原模型账号计费。

参考：[LangGraph 追踪](https://docs.langchain.com/langsmith/trace-with-langgraph)、[API 密钥](https://docs.langchain.com/langsmith/create-account-api-key)、[定价](https://www.langchain.com/pricing)。
