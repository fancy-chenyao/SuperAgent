# 远程 Agent / Skill / Tool / MCP 调用说明（架构与实现）

本文基于当前项目实现，说明远程资源的注册、发现、调用方式，以及为支持远程能力所做的架构调整。

## 1. 总览

系统采用统一资源模型 `ResourceSpec` 来描述 Agent、Tool、Skill、MCP，并通过统一注册表 `ResourceRegistry` 完成本地与远程资源的统一管理。远程调用的核心路径为：

1. 远程注册中心拉取资源清单并合并到本地注册表  
2. 将远程 Agent 映射为本地可调度的 Agent 实例  
3. 执行层根据资源来源路由到本地或远程执行器  
4. MCP 作为工具体系的一部分，通过热加载机制接入远程 MCP Server

## 2. 远程资源注册与发现

### 2.1 统一资源模型

`ResourceSpec` 描述所有资源的统一字段：

1. `type`: `agent` / `tool` / `skill` / `mcp`  
2. `name`: 资源名  
3. `version`: 版本号  
4. `endpoint`: 远程调用地址  
5. `protocol`: `http` / `rpc` / `sse` / `stdio`（目前远程工具/技能仍通过 HTTP 入口调用）  
6. `auth`: 认证信息（如 `api_key`）  
7. `server_id`: 资源来源服务器标识  
8. `tags`: 标签  
9. `health_url`: 健康检查地址  
10. `metadata`: 扩展字段（如描述、prompt、selected_tools 等）

文件：`src/manager/registry/resource_registry.py`

### 2.2 远程 Registry 配置

远程注册中心配置通过 `config/remote_registry.json` 提供：

1. `cache_ttl`: 拉取缓存秒数  
2. `sources`: 多源注册中心列表  
3. `base_url`: 远程注册中心地址  
4. `server_id`: 远程资源归属标识  
5. `priority`: 优先级（数值越小优先级越高）  
6. `health_check`: 是否对 `health_url` 探活  

文件：`config/remote_registry.json`

### 2.3 拉取与合并

`RemoteRegistryGateway` 会访问远程 `GET {base_url}/resources`，支持两种返回格式：

1. 直接返回数组 `[ResourceSpec, ...]`  
2. 返回 `{ "resources": [ResourceSpec, ...] }`  

合并策略：

1. `server_id + type + name` 作为唯一键  
2. 本地资源默认优先于远程  
3. 同一来源比较 `priority`，再比较版本号  
4. 健康检查失败会标记为不可用并从注册表移除  

文件：`src/manager/registry/resource_gateway.py`

### 2.4 本地与远程资源同步

`AgentManager` 初始化时，会将本地 Agent / Tool / Skill 同步到 `ResourceRegistry`，再拉取远程资源并合并：

1. `sync_local_resources()` -> 本地 Agent / Tool / Skill 入库  
2. `refresh_remote_resources()` -> 拉取远程资源  
3. `sync_remote_agents()` -> 将远程 Agent 转为本地可调度实例  

文件：`src/manager/agents.py`、`src/manager/registry/resource_sync.py`

## 3. 远程 Agent 调用方式

### 3.1 调度入口

远程 Agent 会被转换为 `Agent(source=REMOTE, endpoint=...)`，随后交由执行器统一调度。执行器由 `ExecutorFactory` 根据 `source` 分配。

文件：`src/manager/executor/factory.py`

### 3.2 远程请求格式

`RemoteExecutor` 会向 `endpoint` 发送 HTTP POST，包含：

1. `agent_name`  
2. `messages`（统一消息序列化）  
3. `context`（user_id、workflow_id、mode 等）  
4. `prompt`（若远程 Agent 有自定义 prompt）  
5. `tools`（远程 Agent 可用工具列表）  

远程响应格式：

1. `status`: `success` / `failed`  
2. `result`: 成功结果  
3. `error`: 失败原因  
4. `metadata`: 额外信息  

文件：`src/manager/executor/remote.py`

## 4. 远程 Tool 调用方式

### 4.1 本地与远程执行器

1. 本地 Tool：通过 `ToolRegistry` 查找并调用  
2. 远程 Tool：通过 `RemoteToolExecutor` HTTP 调用  

文件：`src/manager/executor/tool.py`

### 4.2 远程请求格式

HTTP POST `endpoint`，payload 结构：

1. `tool`: 工具名  
2. `arguments`: 调用参数  

可选鉴权：

1. `auth.api_key` -> `Authorization: Bearer <api_key>`

### 4.3 远程工具在 Agent 内部使用

远程工具通过 `RemoteToolProxy` 适配为 LangChain Tool，以便被 Agent 直接选择和调用。

文件：`src/manager/executor/remote_tool_proxy.py`

## 5. 远程 Skill 调用方式

### 5.1 执行器

1. 本地 Skill：通过 `SkillsManager.execute_skill()`  
2. 远程 Skill：通过 `RemoteSkillExecutor` HTTP 调用  

文件：`src/manager/executor/skill.py`

### 5.2 远程请求格式

HTTP POST `endpoint`，payload 结构：

1. `skill`: 技能名  
2. `arguments`: 调用参数  

同样支持 `auth.api_key` 作为 Bearer 认证。

## 6. MCP 远程调用方式

MCP 被视作工具体系的一部分，通过配置文件和热加载机制接入远程 MCP Server。

### 6.1 MCP 配置与加载

配置来源：

1. `config/mcp.json`  
2. `config/mcp_sources.json`（支持多源合并）  

加载流程：

1. `MCPHotReloadManager` 读取配置  
2. `MultiServerMCPClient` 连接 MCP Server（支持 `sse` / `stdio`）  
3. 加载 MCP tools 并注册到 `ToolRegistry`  
4. 配置变更可热更新，失败会回滚到上一个快照  

文件：`src/manager/hot_reload/mcp_reload.py`、`src/manager/registry/tool_loader.py`

### 6.2 MCP 工具调用

MCP 工具最终以普通 Tool 的形式进入 `ToolRegistry`，因此在 Planner/Agent 视角与普通工具一致。  
其调用由 MCP client 完成，不经过 HTTP ToolExecutor。

## 7. 架构调整清单（落地变化）

1. 统一资源描述与注册  
   引入 `ResourceSpec` + `ResourceRegistry`，统一管理 Agent / Tool / Skill / MCP  

2. 远程注册中心接入  
   引入 `RemoteRegistryGateway` + `remote_registry.json`，支持多源拉取与合并  

3. 资源同步与热更新  
   引入 `refresh_remote_resources()` + `start_remote_registry_watch()`  

4. 统一执行层  
   新增 `RemoteExecutor`、`RemoteToolExecutor`、`RemoteSkillExecutor`  
   由 `ExecutorFactory` 统一路由本地/远程执行  

5. MCP 多源与热加载  
   MCP 配置支持多源合并，热加载失败自动回滚  
   MCP 工具作为 ToolRegistry 资源参与规划与执行  

## 8. 远程服务需要实现的接口约定

### 8.1 Remote Agent

HTTP POST `endpoint` 接口，返回示例：

1. `{"status": "success", "result": "...", "metadata": {}}`  
2. `{"status": "failed", "error": "reason"}`  

### 8.2 Remote Tool

HTTP POST `endpoint` 接口，返回示例：

1. `{"status": "success", "result": ...}`  
2. `{"status": "failed", "error": "reason"}`  

### 8.3 Remote Skill

HTTP POST `endpoint` 接口，返回格式与 Tool 一致。

## 9. 关键文件索引

1. 统一资源模型：`src/manager/registry/resource_registry.py`  
2. 远程注册网关：`src/manager/registry/resource_gateway.py`  
3. 资源同步：`src/manager/registry/resource_sync.py`  
4. 资源刷新入口：`src/manager/resource.py`  
5. 远程 Agent 执行器：`src/manager/executor/remote.py`  
6. 远程 Tool/Skill 执行器：`src/manager/executor/tool.py`、`src/manager/executor/skill.py`  
7. MCP 热加载：`src/manager/hot_reload/mcp_reload.py`  
8. MCP 工具加载器：`src/manager/registry/tool_loader.py`
