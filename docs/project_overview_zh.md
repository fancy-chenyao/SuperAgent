# SuperAgent 项目说明

本文基于当前仓库源码整理，说明这个项目的定位、运行方式、核心功能模块、数据流和扩展点。它不是只复述 README，而是结合 `cli.py`、`src/workflow`、`src/manager`、`src/service`、`remote_agents`、`assets`、`config` 等实际文件给出的结构化分析。

## 1. 项目定位

SuperAgent 是一个 Python 实现的 AI 多智能体协作系统。用户输入一个自然语言任务后，系统会：

1. 理解用户意图；
2. 选择或创建可用 Agent；
3. 生成多步骤执行计划；
4. 按计划调度本地 Agent 或远程 Agent；
5. 让 Agent 调用搜索、爬虫、代码执行、浏览器、文件、Excel、MCP、远程业务工具等能力；
6. 流式返回执行过程；
7. 保存 workflow、任务日志和 checkpoint，支持后续复用、调优和断点恢复。

从工程形态看，它同时包含：

- 一个命令行应用：`cli.py`
- 一个 FastAPI Web 服务：`src/service/web_app.py`
- 一个多智能体工作流引擎：`src/workflow`
- Agent、Tool、Skill、MCP、Remote Resource 的统一管理层：`src/manager`
- 本地工具集合：`src/tools`
- 远程 Agent 模拟服务与业务工具：`remote_agents`、`mock_remote_agent.py`、`mock_remote_tool_skill.py`
- Web 前端静态页面：`web`
- 业务样例数据：`assets`
- 任务日志、checkpoint、workflow 持久化能力：`src/robust`、`store`

项目包名在 `pyproject.toml` 中是 `cooragent`，但 README 和多数文档使用 `SuperAgent` 作为项目名。

## 2. 核心使用场景

### 2.1 通用多智能体任务

用户可以要求系统完成信息检索、分析、写报告、代码执行、网页操作等复合任务。系统默认内置以下共享 Agent：

| Agent | 作用 | 典型工具 |
| --- | --- | --- |
| `researcher` | 搜索和网页内容采集，整理研究结果 | `tavily_tool`、`crawl_tool` |
| `coder` | Python/Bash 执行、计算、数据处理、代码类任务 | `python_repl_tool`、`bash_tool` |
| `browser` | 浏览器交互、点击、输入、滚动、网页信息提取 | `browser_tool` |
| `reporter` | 基于已有信息撰写结构化报告 | 无工具，主要依赖上下文 |

这些默认 Agent 在 `src/manager/agents.py` 的 `_load_default_agents()` 中定义，除非通过 `DISABLE_DEFAULT_AGENTS=True` 关闭。

### 2.2 工作流快速生成、打磨和生产执行

项目提供三个主要工作模式：

| 模式 | CLI 命令 | 作用 |
| --- | --- | --- |
| Launch | `run-l` | 根据用户任务自动规划并执行，执行结束后保存 workflow |
| Polish | `run-o` | 对已有 workflow 做调优，例如改计划、改 Agent、改工具、改 Prompt |
| Production | `run-p` | 执行已经保存好的 workflow，减少临时规划，偏生产复用 |

此外支持：

- `resume`：从 checkpoint 恢复执行；
- `list-agents`：列出 Agent；
- `edit-agent`：编辑 Agent；
- `remove-agent`：删除 Agent；
- `list-default-tools`：查看默认工具；
- `web`：启动 Web UI。

### 2.3 企业/办公业务自动化样例

仓库内的 `remote_agents` 和 `assets` 显示，这个项目不仅是通用 Agent 框架，还包含一组业务自动化样例：

- HR 人员与薪资查询；
- 收入证明、文档模板生成；
- 请假和出差申请；
- 日程和待办管理；
- 通讯录查询和邮件发送；
- 会议安排；
- 知识库问答；
- 独角兽企业筛选；
- 企业信用风险查询；
- 报告生成与邮件分发。

这些业务能力主要通过远程 Agent + 远程 Tool 模拟，适合演示“多个业务系统被 Agent 编排调用”的流程。

## 3. 总体架构

项目主链路如下：

```text
用户输入
  -> CLI 或 Web API
  -> run_agent_workflow()
  -> Coordinator
  -> Planner
  -> Publisher
  -> AgentProxy
  -> ExecutorFactory
  -> LocalExecutor 或 RemoteExecutor
  -> ToolRegistry / MCP / Remote Tool / Skill
  -> 结果流式返回
  -> workflow、log、checkpoint 持久化
```

关键文件：

| 层级 | 文件 | 职责 |
| --- | --- | --- |
| 入口层 | `cli.py` | Click 命令行入口，支持交互式和命令式调用 |
| Web 服务 | `src/service/web_app.py` | FastAPI 应用、SSE 流式接口、Agent/Tool/Workflow/Task API |
| 服务封装 | `src/service/server.py` | 将 API 请求转换为 workflow 调用，并管理会话上下文 |
| 工作流主循环 | `src/workflow/process.py` | 运行 Agent 工作流、事件流、checkpoint、hook、自恢复 |
| 工作流节点 | `src/workflow/coor_task.py` | Coordinator、Planner、Publisher、AgentProxy 节点逻辑 |
| 工作流图 | `src/workflow/graph.py` | 简化版工作流图执行结构 |
| Workflow 缓存 | `src/workflow/cache.py` | workflow 读写、执行队列、Mermaid 可视化、instruction history |
| Agent 模型 | `src/interface/agent.py` | Agent、请求、状态、枚举、Pydantic schema |
| Agent 管理 | `src/manager/agents.py` | 默认 Agent、磁盘 Agent、远程 Agent、工具和 Skill 初始化 |
| 执行器 | `src/manager/executor` | 本地/远程 Agent、Tool、Skill 执行 |
| 工具注册 | `src/manager/registry/tool_registry.py` | 全局工具和 Agent 专属工具注册表 |
| 工具加载 | `src/manager/registry/tool_loader.py` | 加载内置工具和 MCP 工具 |
| 远程资源 | `src/manager/registry/resource_*` | 统一管理远程 Agent/Tool/Skill/MCP 资源 |
| 稳定性 | `src/robust` | TaskLogger、Checkpoint、自恢复 Hook、失败归因 |

## 4. 工作流执行机制

### 4.1 Coordinator

文件：`src/workflow/coor_task.py`

Coordinator 负责理解用户输入，判断是否需要交给 Planner。Launch 模式下，它会把系统节点写入 workflow cache。如果 LLM 输出包含 `handover_to_planner`，流程进入 Planner。

Production 模式下，Coordinator 基本直接把控制权交给 Publisher，因为生产模式通常已经有可执行计划。

### 4.2 Planner

Planner 负责把用户任务转换为结构化步骤。它会调用 Prompt 模板，必要时先搜索，再用 LLM 生成计划。

计划格式一般包含：

```json
{
  "steps": [
    {
      "agent_name": "researcher",
      "title": "任务标题",
      "description": "该步骤要做什么",
      "note": "注意事项",
      "inputs": []
    }
  ]
}
```

Planner 还有几个增强逻辑：

- 支持流式输出 planner delta，Web UI 可实时展示；
- 会尝试从 LLM 回复中提取 JSON；
- JSON 解析失败时会追加“只输出 JSON”的提示重试；
- Launch 模式下会校验 Agent 的 `requires` / `produces` 数据依赖；
- 如果数据依赖错误，会请求 LLM 修复计划；
- 支持 `stop_after_planner`，只生成计划不继续执行。

### 4.3 Publisher

Publisher 决定下一步应该交给哪个 Agent。

- Launch 模式：调用 LLM，根据当前计划和上下文选择下一位 Agent；
- Production/Polish 模式：从 `WorkflowCache.queue` 中取下一个执行节点；
- 如果下一个节点是 `FINISH`，流程结束；
- 否则进入 `agent_proxy`。

### 4.4 AgentProxy

AgentProxy 是实际执行 Agent 的代理节点。它会：

1. 根据 `state["next"]` 找到目标 Agent；
2. 构造 `ExecutionContext`；
3. 调用 `execute_agent()`；
4. 把结果转换成普通文本消息和结构化结果消息；
5. 更新 workflow cache 或 production 队列；
6. 返回 Publisher，继续下一步。

这使工作流层不需要关心目标 Agent 是本地还是远程。

## 5. Agent 系统

### 5.1 Agent 数据模型

文件：`src/interface/agent.py`

Agent 的核心字段：

| 字段 | 说明 |
| --- | --- |
| `user_id` | 所属用户，默认共享 Agent 使用 `share` |
| `agent_name` | 全局调度名 |
| `nick_name` | 显示名称 |
| `description` | 给 Planner/Coordinator 使用的能力描述 |
| `llm_type` | `basic`、`reasoning`、`vision`、`code` |
| `selected_tools` | Agent 可调用工具列表 |
| `prompt` | Agent 系统提示词 |
| `source` | `local` 或 `remote` |
| `endpoint` | 远程 Agent HTTP 地址 |
| `api_key` | 远程调用鉴权 |
| `mcp_config` | Agent 专属 MCP 配置 |
| `requires` / `produces` | 计划数据流校验用的输入/输出能力描述 |

### 5.2 Agent 持久化

Agent 存储在：

- `store/agents/*.json`
- `store/prompts/*.md`

`AgentRegistry` 负责注册、获取、更新、删除和从磁盘加载 Agent。

### 5.3 AgentManager 初始化流程

文件：`src/manager/agents.py`

初始化顺序大致是：

1. 创建 `store/tools`、`store/agents`、`store/prompts`、`store/skills`；
2. 加载默认 Agent；
3. 从磁盘加载用户 Agent；
4. 加载内置工具；
5. 初始化 Skill；
6. 同步本地 Agent/Tool/Skill 到统一资源注册表；
7. 拉取远程资源；
8. 将远程 Agent 同步为本地可调度的 `Agent(source="remote")`；
9. 刷新内存缓存。

## 6. 执行器系统

文件目录：`src/manager/executor`

### 6.1 ExecutorFactory

`ExecutorFactory` 根据 Agent 的 `source` 字段选择执行器：

- `local` -> `LocalExecutor`
- `remote` -> `RemoteExecutor`

它也提供 Tool 和 Skill 执行入口：

- 本地 Tool：`ToolExecutor`
- 远程 Tool：`RemoteToolExecutor`
- 本地 Skill：`SkillExecutor`
- 远程 Skill：`RemoteSkillExecutor`

### 6.2 LocalExecutor

文件：`src/manager/executor/local.py`

本地 Agent 使用 LangGraph 的 `create_react_agent` 执行。执行流程：

1. 从 `ToolRegistry` 找到该 Agent 可用工具；
2. 根据 `agent.llm_type` 创建或复用 LLM；
3. 用 `agent.prompt` 和消息上下文构造提示词；
4. 运行 ReAct Agent；
5. 取最后一条消息作为结果；
6. 返回 `ExecuteResult`。

### 6.3 RemoteExecutor

文件：`src/manager/executor/remote.py`

远程 Agent 使用 HTTP POST 调用。请求格式包含：

- `agent_name`
- `messages`
- `context`
- `prompt`
- `tools`

远程响应格式：

```json
{
  "status": "success",
  "result": "...",
  "metadata": {}
}
```

或：

```json
{
  "status": "failed",
  "error": "失败原因"
}
```

RemoteExecutor 内置：

- aiohttp session 复用；
- 超时控制；
- 并发限制；
- 重试；
- API Key Bearer Header；
- `/health` 健康检查辅助方法。

## 7. Tool、Skill 与 MCP

### 7.1 内置工具

文件：`src/tools`

当前主要工具包括：

| 工具 | 文件 | 作用 |
| --- | --- | --- |
| `tavily_tool` | `src/tools/search.py` | 搜索引擎查询 |
| `crawl_tool` | `src/tools/crawl.py`、`src/tools/crawler/*` | 网页抓取和正文抽取 |
| `python_repl_tool` | `src/tools/python_repl.py` | Python 代码执行 |
| `bash_tool` | `src/tools/bash_tool.py` | Shell/Bash 命令执行 |
| `browser_tool` | `src/tools/browser.py` | 浏览器自动化 |
| `write_file_tool` | `src/tools/file_management.py` | 文件写入 |
| `avatar_tool` | `src/tools/avatar_tool.py` | 图像/头像相关能力 |
| `web_preview_tool` | `src/tools/web_preview_tool.py` | Web 预览服务 |
| Excel 工具 | `src/tools/excel/*` | 工作簿、单元格、公式、图表、透视表等操作 |
| 邮件/办公工具 | `src/tools/gmail.py`、`src/tools/office365.py`、`src/tools/slack.py` | 外部办公集成 |

`ToolLoader` 会把内置工具注册到 `ToolRegistry`。如果 `USE_BROWSER=False`，`browser` 工具不会加载。

### 7.2 ToolRegistry

文件：`src/manager/registry/tool_registry.py`

ToolRegistry 是工具注册中心，支持：

- 全局工具；
- Agent 专属工具；
- 按 scope/server/tag 查询工具；
- 工具版本号；
- MCP 工具和内置工具共存；
- 工具名冲突时的合并逻辑。

### 7.3 Skill 系统

文件：`src/skills`

Skill 是另一类可执行能力，和 Tool 类似但抽象更高。当前包含：

- `calculator_skill`
- `greeting_skill`

`SkillsManager` 负责加载、查询和执行 Skill。执行器层还支持远程 Skill。

### 7.4 MCP 集成

MCP 配置来源：

- `config/mcp.json`
- `config/mcp.json.example`
- `config/mcp_sources.json`

相关模块：

- `src/manager/mcp.py`
- `src/manager/hot_reload/mcp_reload.py`
- `src/manager/registry/tool_loader.py`

MCP 工具会通过 `MultiServerMCPClient` 加载，然后注册进 ToolRegistry。系统支持：

- SSE / stdio MCP Server；
- 配置指纹；
- 热加载；
- Agent 专属 MCP；
- 加载失败回滚；
- Web API 查看 MCP 配置。

## 8. 远程资源与业务 Agent

### 8.1 远程资源模型

文件：`src/manager/registry/resource_registry.py`

`ResourceSpec` 统一描述远程和本地资源：

- `type`：`agent` / `tool` / `skill` / `mcp`
- `name`
- `version`
- `endpoint`
- `protocol`
- `auth`
- `server_id`
- `tags`
- `health_url`
- `metadata`

远程 registry 配置在 `config/remote_registry.json`，默认指向本地模拟服务：

```json
{
  "cache_ttl": 5,
  "sources": [
    {
      "name": "remote-demo",
      "base_url": "http://127.0.0.1:8012",
      "server_id": "remote-demo",
      "priority": 50,
      "timeout": 5,
      "health_check": true
    }
  ]
}
```

### 8.2 远程资源发现

`RemoteRegistryGateway` 会请求：

```text
GET {base_url}/resources
```

支持返回：

```json
[
  { "type": "agent", "name": "..." }
]
```

或：

```json
{
  "resources": []
}
```

本地示例资源文件是 `mock_remote_registry.json`。

### 8.3 远程业务 Agent

`remote_agents/factory.py` 注册的远程 Agent 包括：

| 远程 Agent | 作用 |
| --- | --- |
| `RemoteHRAssistantAgent` | 查询人员信息、薪资收入 |
| `RemoteKnowledgeAgent` | 查询 HR 政策、劳动法规、知识库 |
| `RemoteDocumentGeneratorAgent` | 根据模板生成 Word 文档 |
| `RemoteReportAgent` | 根据结构化数据生成 Markdown 报告 |
| `RemoteOfficeAssistantAgent` | 请假、出差申请保存和查询 |
| `RemoteBusinessRiskAgent` | 查询企业信用风险指标 |
| `RemoteEmailDispatchAgent` | 发送邮件 |
| `RemoteUnicornSelectorAgent` | 从企业库筛选独角兽企业 |
| `RemoteMeetingManagerAgent` | 会议创建和查询 |
| `RemoteCommunicationAgent` | 通讯录查询和邮件发送 |

`mock_remote_registry.json` 中还包含更多以资源形式注册的 Agent，例如天气、日程、待办等。

### 8.4 远程 Tool 示例

远程 Tool 包括：

- `remote_weather_tool`
- `remote_person_info_tool`
- `remote_salary_info_tool`
- `remote_unicorn_db_tool`
- `remote_credit_risk_db_tool`
- `remote_report_builder_tool`
- `remote_email_tool`
- `remote_docx_generator_tool`
- `knowledge_search_tool`
- `get_calendar_events_tool`
- `create_calendar_event_tool`
- `save_leave_record`
- `query_leave_record`
- `save_travel_record`
- `query_travel_record`
- `remote_meeting_scheduling_tool`
- `remote_contact_query_tool`

这些工具的模拟数据主要来自 `assets`。

## 9. 数据资产

`assets` 是业务样例数据目录，包含：

| 文件 | 说明 |
| --- | --- |
| `person_info_sample.json` | 人员信息样例 |
| `mock_salary_db.json` | 薪资/收入数据 |
| `document_templates.json` | 文档模板配置 |
| `knowledge_base.json` | HR 政策和知识库 |
| `calendar_events.json` | 日程数据 |
| `todo_sample.json` | 待办数据 |
| `contacts.json` | 通讯录 |
| `email_log.json` | 邮件发送记录 |
| `leave_applications.json` | 请假申请 |
| `travel_applications.json` | 出差申请 |
| `unicorn_db.json` / `unicorn_db_sample.json` | 企业/独角兽数据库 |
| `credit_risk_db.json` / `credit_risk_db_sample.json` | 信用风险数据 |
| `visit_schedule.json` | 访问日程 |

`output` 目录保存生成结果，例如已生成的收入证明 `.docx` 文件。

## 10. Web UI 与 API

### 10.1 启动方式

```bash
python cli.py web --host 0.0.0.0 --port 8001
```

前端静态文件位于：

- `web/index.html`
- `web/app.js`
- `web/styles.css`
- `web/mermaid.min.js`

### 10.2 主要 API

文件：`src/service/web_app.py`

| API | 说明 |
| --- | --- |
| `POST /api/workflows/run` | 启动 workflow，SSE 流式返回 |
| `GET /api/agents` | 列出 Agent |
| `GET /api/agents/default` | 列出默认 Agent |
| `GET /api/agents/health` | 检查远程 Agent 健康状态 |
| `GET /api/agents/stats` | Agent 使用统计 |
| `GET /api/tools` | 列出工具 |
| `GET /api/tools/{tool_name}` | 工具详情 |
| `GET /api/tools/stats` | 工具使用统计 |
| `GET /api/tools/mcp` | MCP 工具配置 |
| `GET /api/workflows` | workflow 列表 |
| `GET /api/workflows/{workflow_id}` | workflow 详情 |
| `GET /api/workflows/{workflow_id}/mermaid` | workflow Mermaid 图 |
| `GET /api/tasks` | 任务执行实例列表 |
| `GET /api/tasks/{task_id}/log` | 任务结构化日志 |
| `GET /api/tasks/{task_id}/checkpoints` | checkpoint 列表 |
| `GET /api/tasks/{task_id}/checkpoints/{step}` | checkpoint 详情 |
| `POST /api/tasks/resume` | 从指定 step 恢复执行 |
| `DELETE /api/tasks/{task_id}` | 删除任务日志和 checkpoint |

## 11. 可靠性：日志、Checkpoint 和 Hook

文件目录：`src/robust`

### 11.1 TaskLogger

`TaskLogger` 记录每次执行实例的结构化历史，包括：

- workflow 开始/结束；
- Agent 开始/结束；
- 消息；
- 错误；
- 执行阶段：初始规划、重规划、执行等。

### 11.2 CheckpointManager

每个节点执行结束后，`_process_workflow()` 会保存 checkpoint，包含：

- workflow_id；
- task_id；
- step；
- 当前节点；
- 下一个节点；
- 当前 state。

恢复时，系统会从 `resume_step - 1` 的 checkpoint 读取状态，然后执行 `resume_step`。

### 11.3 Hook 自动恢复

Hook 系统位于 `src/robust/hooks`。如果 `AUTO_RECOVERY_ENABLED=True`，执行循环会在这些点触发 Hook：

- `NODE_START`
- `NODE_END`
- `WORKFLOW_END`
- `ERROR`

规则包括：

- 输出校验；
- 循环检测；
- 长消息检测；
- 未完成任务检测；
- 异常检测。

处理器包括：

- 预防；
- 校验；
- 失败归因；
- 修正注入。

这部分用于长任务稳定性、失败定位和自动恢复。

## 12. 持久化目录

| 目录 | 说明 |
| --- | --- |
| `store/agents` | Agent JSON 定义 |
| `store/prompts` | Agent Prompt |
| `store/workflows` | 保存的 workflow |
| `store/tools` | 工具相关持久化 |
| `store/skills` | Skill 持久化 |
| `output` | 文档等执行输出 |
| `assets` | 业务模拟数据 |

workflow 文件命名方式通常是：

```text
store/workflows/{user_id}/{polish_id}.json
```

workflow_id 格式通常是：

```text
{user_id}:{polish_id}
```

## 13. LLM 配置

配置来自 `.env` 和 `src/service/env.py`。

主要变量：

| 变量 | 说明 |
| --- | --- |
| `REASONING_MODEL` | 复杂规划和推理模型 |
| `REASONING_BASE_URL` | 推理模型兼容 OpenAI API 的 base url |
| `REASONING_API_KEY` | 推理模型 API Key |
| `BASIC_MODEL` | 普通任务模型 |
| `BASIC_BASE_URL` | 普通模型 base url |
| `BASIC_API_KEY` | 普通模型 API Key |
| `CODE_MODEL` | 代码模型 |
| `CODE_BASE_URL` | 代码模型 base url |
| `CODE_API_KEY` | 代码模型 API Key |
| `VL_MODEL` | 视觉语言模型 |
| `VL_BASE_URL` | 视觉模型 base url |
| `VL_API_KEY` | 视觉模型 API Key |
| `USE_BROWSER` | 是否启用浏览器工具 |
| `USE_MCP_TOOLS` | 是否启用 MCP 工具 |
| `DISABLE_DEFAULT_AGENTS` | 是否关闭默认 Agent 创建 |
| `MAX_STEPS` | Agent ReAct 最大递归步数 |
| `AUTO_RECOVERY_ENABLED` | 是否启用 Hook 自动恢复 |

LLM 创建逻辑在 `src/llm/llm.py`，本质上使用 OpenAI 兼容接口：

- `basic`
- `reasoning`
- `code`
- `vision`

## 14. 配置文件

| 文件 | 说明 |
| --- | --- |
| `.env.example` | 环境变量模板 |
| `config/workflow.json` | 示例 workflow 配置 |
| `config/remote_registry.json` | 远程资源中心配置 |
| `config/mcp.json.example` | MCP 配置模板 |
| `config/mcp_sources.json` | 多源 MCP 配置 |
| `config/global_variables.py` | 项目目录、开关等全局变量 |
| `config/global_functions.py` | workflow 条件函数等 |

## 15. 典型执行流程示例

用户输入：

```text
帮我查询王强的收入信息，并生成一份收入证明。
```

可能的执行链路：

1. Coordinator 判断需要规划；
2. Planner 生成计划：
   - `RemoteHRAssistantAgent` 查询人员与薪资；
   - `RemoteDocumentGeneratorAgent` 根据模板生成收入证明；
   - `RemoteReportAgent` 或 `reporter` 汇总结果；
3. Publisher 选择第一个 Agent；
4. AgentProxy 调用 RemoteExecutor；
5. 远程 Agent 调用 `remote_person_info_tool`、`remote_salary_info_tool`；
6. 返回结构化员工/薪资数据；
7. Publisher 继续调度文档生成 Agent；
8. 文档工具读取 `assets/document_templates.json` 并写入 `output/*.docx`；
9. workflow、日志、checkpoint 保存到本地。

## 16. 测试与验证

测试目录：

- `tests/test_app.py`
- `tests/test_hooks.py`
- `tests/test_hooks_standalone.py`
- `tests/integration/test_crawler.py`
- `tests/integration/test_bash_tool.py`
- `tests/integration/test_avatar.py`

项目使用 pytest，配置在 `pyproject.toml`：

```bash
pytest
```

测试配置包含：

- `testpaths = ["tests"]`
- `python_files = ["test_*.py"]`
- 覆盖率目标：`--cov=src`

## 17. 项目当前值得注意的点

1. `README_zh.md` 和部分中文注释/文档存在编码显示异常，但英文 README 和源码结构可读。
2. `src/workflow/graph.py` 是自定义简化图执行器，不是完整依赖 LangGraph 图对象；本地 Agent 执行时才使用 LangGraph ReAct Agent。
3. Launch 模式由 LLM 动态选择下一 Agent；Production 模式更多依赖已保存 workflow 的执行队列。
4. 远程业务能力目前主要是 mock/demo 服务，通过本地端口 `8010`、`8011`、`8012` 模拟。
5. workflow cache 同时负责状态、执行队列和 Mermaid 可视化，职责较重。
6. 代码中有一些中文字符串显示为乱码，说明历史文件可能经历过编码转换问题。
7. `assets/email_log.json` 和 `src/workflow/cache.py` 当前在工作区已有未提交修改，分析时只读取，未改动。

## 18. 扩展方式

### 18.1 新增本地 Agent

可以通过 CLI/Web 创建，也可以直接写入：

- `store/agents/{agent_name}.json`
- `store/prompts/{agent_name}.md`

Agent 需要定义：

- 名称；
- 描述；
- LLM 类型；
- Prompt；
- 可用工具；
- 可选的 `requires` / `produces`。

### 18.2 新增内置工具

步骤通常是：

1. 在 `src/tools` 中实现 LangChain Tool；
2. 在 `src/tools/__init__.py` 导出；
3. 在 `ToolLoader._get_builtin_tool_instances()` 中加入；
4. 给 Agent 的 `selected_tools` 配置工具名。

### 18.3 新增远程 Agent/Tool

步骤通常是：

1. 远程服务实现 HTTP 接口；
2. 在远程 registry 返回 `ResourceSpec`；
3. 配置 `config/remote_registry.json`；
4. AgentManager 初始化时同步远程资源；
5. Planner 根据 Agent 描述选择调用。

### 18.4 新增 MCP 工具

步骤通常是：

1. 创建 `config/mcp.json`；
2. 添加 MCP Server；
3. 确保 `USE_MCP_TOOLS=True`；
4. 触发 MCP reload；
5. 工具进入 ToolRegistry，Agent 可选择调用。

## 19. 一句话总结

这个项目是一个“多智能体工作流编排平台”：上层让用户用自然语言发起任务，中层用 Coordinator/Planner/Publisher/AgentProxy 编排执行，下层统一接入本地 Agent、远程 Agent、Tool、Skill、MCP 和业务系统，并通过 workflow 持久化、任务日志、checkpoint、Web UI 支撑从原型到生产复用的完整链路。
