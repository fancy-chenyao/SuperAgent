# SuperAgent

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![GitHub stars](https://img.shields.io/github/stars/LeapLabTHU/SuperAgent?style=social)](https://github.com/LeapLabTHU/SuperAgent/stargazers)

[English](./README.md) | [简体中文](./README_zh.md)

# SuperAgent 是什么

SuperAgent 是一个 AI 多智能体协作系统。通过一句话描述任务，SuperAgent 会自动分析需求、选择合适的智能体、规划执行步骤，并协调多个智能体协作完成复杂任务。智能体可以自由组合，创造出无限可能。

# 核心理念

当智能体的构建和打磨变得足够简单，AGI 时代将真正到来。
SuperAgent 的核心目标是：帮助用户快速构建智能体，快速构建工作流，快速打磨工作流。

# 核心特性

## 三种工作模式

SuperAgent 提供三种工作流模式，覆盖从快速原型到生产部署的完整生命周期：

### Launch 模式 - 快速构建
- 只需描述想要完成的目标任务
- 系统自动分析需求、选择智能体、构建完整工作流
- 任务结束后工作流保存在本地（`store/workflow`），支持后续复用与编辑
- CLI 命令：`run-l`

### Polish 模式 - 精细打磨
- 手动或通过自然语言指令调整工作流
- 支持修改执行顺序、工具选择、LLM 配置、Prompt 等
- 基于 APE、Absolute-Zero-Reasoner 等技术自动优化
- CLI 命令：`run-o`

### Production 模式 - 生产运行
- 使用打磨好的工作流高效执行
- 避免过多运行干预，使用 Supervisor 对结果兜底
- 支持断点恢复，确保长时间任务的可靠性
- CLI 命令：`run-p`

## 多智能体协作架构

SuperAgent 采用 Coordinator-Planner-Publisher-AgentProxy 四层架构：

1. **Coordinator（协调器）**：理解用户意图，决定是否进入规划
2. **Planner（规划器）**：生成多智能体协作计划
3. **Publisher（发布器）**：根据计划选择下一位执行的 Agent
4. **Agent Proxy（代理执行器）**：调用目标 Agent（本地或远程），收集结果

## 强大的工具生态

- **内置工具**：搜索、爬虫、代码执行、浏览器操作、文件管理、Excel 操作等
- **MCP 集成**：支持 Model Context Protocol，可集成外部服务（如高德地图、AWS 服务）
- **远程工具**：支持调用远程部署的 Agent 和工具

## 任务执行追踪与恢复

- **完整日志记录**：记录每个任务执行的完整交互历史
- **检查点机制**：每个节点执行后自动保存状态
- **断点恢复**：支持从任意检查点恢复执行，应对网络错误、API 限流等场景
- **Web UI 可视化**：通过 Task History 页面查看任务历史、日志和检查点

# 快速开始

## 安装

### 使用 conda

```bash
git clone https://github.com/LeapLabTHU/SuperAgent.git
cd SuperAgent

conda create -n superagent python=3.12
conda activate superagent

pip install -e .

# 可选：使用 browser 工具时需要安装
playwright install

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入你的 API keys

# 启动 CLI
python cli.py
```

### 使用 venv

```bash
git clone https://github.com/LeapLabTHU/SuperAgent.git
cd SuperAgent

uv python install 3.12
uv venv --python 3.12
source .venv/bin/activate  # Windows: .venv\Scripts\activate

uv sync

# 可选：使用 browser 工具时需要安装
playwright install

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入你的 API keys

# 启动 CLI
uv run cli.py
```

**注意**：Windows 平台需要安装额外依赖，详见 [Windows 平台支持](./docs/QA_zh.md#windows-平台支持)

## 配置

在 `.env` 文件中配置以下环境变量：

```bash
# 推理模型（用于复杂推理任务）
REASONING_MODEL=qwen-max-latest

# 基础模型（用于简单任务）
BASIC_MODEL=qwen-max-latest

# 代码模型
CODE_MODEL=deepseek-chat

# 视觉语言模型
VL_MODEL=qwen2.5-vl-72b-instruct

# 浏览器工具（默认关闭，因为耗时较长）
USE_BROWSER=False

# 可选：搜索 API Key
# TAVILY_API_KEY=
# JINA_API_KEY=
```

# CLI 工具使用

## 启动交互式界面

```bash
python cli.py
```

## Launch 模式 - 创建股票分析工作流

```bash
run-l -u test -m '创建一个股票分析专家 agent. 今天是 2025年 4 月 22 日，查看过去一个月的小米股票走势，分析当前小米的热点新闻，预测下个交易日的股价走势，并给出买入或卖出的建议。'
```

## Polish 模式 - 打磨工作流

```bash
run-o -u test
```

## Production 模式 - 运行生产工作流

```bash
run-p -u test -w <workflow-id>
```

## 断点恢复

```bash
resume -w <workflow-id> -s <step-number> -u test
```

## 管理 Agent

```bash
# 列出 Agent
list-agents -u test

# 编辑 Agent
edit-agent -n <agent-name> -u test

# 删除 Agent
remove-agent -n <agent-name> -u test
```

## 查看工具列表

```bash
list-default-tools
```

# Web UI

## 启动 Web 服务

```bash
python cli.py web --host 0.0.0.0 --port 8001
```

访问 `http://localhost:8001` 使用 Web 界面。

Web UI 提供：
- 任务执行面板
- Agent/Tool/Workflow 管理
- 任务历史查看
- 检查点可视化
- 断点恢复操作

详见 [Web UI 使用指南](./docs/web_ui_guide_zh.md)

# MCP 服务集成

SuperAgent 支持通过 Model Context Protocol (MCP) 集成外部服务和工具。

## 配置方法

1. 创建配置文件：
```bash
cd ./config
cp mcp.json.example mcp.json
```

2. 添加 MCP 服务：
```json
{
  "mcpServers": {
    "AMAP": {
      "url": "https://mcp.amap.com/sse",
      "env": {
        "AMAP_MAPS_API_KEY": "YOUR_API_KEY"
      }
    }
  }
}
```

配置完成后，SuperAgent 会自动将 MCP 服务注册为可用工具，智能体可以在任务执行时调用这些工具。

示例：
```bash
run-l -u test -m '创建一个导航智能体，使用地图相关工具，规划如何从北京西站到故宫。'
```

# 文档

- [架构说明](./docs/architecture.md)
- [常见问题 (FAQ)](./docs/QA_zh.md)
- [任务执行追踪与恢复](./docs/task_execution_tracking_zh(taskLog_CheckpointResume).md)
- [远程调用说明](./docs/remote_invocation_zh.md)
- [商业支持计划](./docs/business_support_zh.md)

# 贡献

我们欢迎各种形式的贡献！请查看 [贡献指南](CONTRIBUTING.md) 了解如何开始。

# 社区

欢迎加入我们的微信群，随时提问、分享、交流。

<div align="center">
    <img src="assets/wechat_community.jpg" alt="SuperAgent 微信群" width="300" />
</div>

# 引用

核心贡献者：Zheng Wang, Shenzhi Wang, Yue Wu, Shiji Song, Gao Huang

```bibtex
@misc{wang2025superagent,
  title        = {SuperAgent: An AI Multi-Agent Collaboration System},
  author       = {Zheng Wang, Shenzhi Wang, Yue Wu, Chi Zhang, Shiji Song, Gao Huang},
  howpublished = {\url{https://github.com/LeapLabTHU/SuperAgent}},
  year         = {2025}
}
```

# Star History

![Star History Chart](https://api.star-history.com/svg?repos=LeapLabTHU/SuperAgent&type=Date)

# 致谢

特别感谢所有让 SuperAgent 成为可能的开源项目和贡献者。
