# SuperAgent

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![GitHub stars](https://img.shields.io/github/stars/LeapLabTHU/SuperAgent?style=social)](https://github.com/LeapLabTHU/SuperAgent/stargazers)

[English](./README.md) | [简体中文](./README_zh.md)

# What is SuperAgent

SuperAgent is an AI multi-agent collaboration system. Simply describe your task in one sentence, and SuperAgent will automatically analyze requirements, select appropriate agents, plan execution steps, and coordinate multiple agents to complete complex tasks collaboratively. Agents can be freely combined to create infinite possibilities.

# Core Philosophy

When building and refining agents becomes simple enough, the true AGI era will arrive.
SuperAgent's core goals are: helping users quickly build agents, quickly build workflows, and quickly refine workflows.

# Key Features

## Three Workflow Modes

SuperAgent provides three workflow modes covering the complete lifecycle from rapid prototyping to production deployment:

### Launch Mode - Rapid Construction
- Simply describe the target task you want to accomplish
- System automatically analyzes requirements, selects agents, and builds complete workflow
- Workflow is saved locally (`store/workflow`) after task completion for reuse and editing
- CLI command: `run-l`

### Polish Mode - Fine-tuning
- Manually or through natural language instructions adjust the workflow
- Supports modifying execution order, tool selection, LLM configuration, prompts, etc.
- Automatically optimizes based on techniques like APE and Absolute-Zero-Reasoner
- CLI command: `run-o`

### Production Mode - Production Execution
- Efficiently executes refined workflows
- Minimizes runtime intervention, uses Supervisor for result validation
- Supports checkpoint recovery to ensure reliability of long-running tasks
- CLI command: `run-p`

## Multi-Agent Collaboration Architecture

SuperAgent adopts a four-layer Coordinator-Planner-Publisher-AgentProxy architecture:

1. **Coordinator**: Understands user intent and decides whether to enter planning
2. **Planner**: Generates multi-agent collaboration plans
3. **Publisher**: Selects the next Agent to execute based on the plan
4. **Agent Proxy**: Invokes target Agent (local or remote) and collects results

## Powerful Tool Ecosystem

- **Built-in Tools**: Search, web crawling, code execution, browser operations, file management, Excel operations, etc.
- **MCP Integration**: Supports Model Context Protocol for integrating external services (e.g., Amap, AWS services)
- **Remote Tools**: Supports invoking remotely deployed Agents and tools

## Task Execution Tracking and Recovery

- **Complete Logging**: Records complete interaction history for each task execution
- **Checkpoint Mechanism**: Automatically saves state after each node execution
- **Checkpoint Recovery**: Supports resuming from any checkpoint to handle network errors, API rate limits, etc.
- **Web UI Visualization**: View task history, logs, and checkpoints through the Task History page

# Quick Start

## Installation

### Using conda

```bash
git clone https://github.com/LeapLabTHU/SuperAgent.git
cd SuperAgent

conda create -n superagent python=3.12
conda activate superagent

pip install -e .

# Optional: Required for browser tool
playwright install

# Configure environment variables
cp .env.example .env
# Edit .env file and fill in your API keys

# Start CLI
python cli.py
```

### Using venv

```bash
git clone https://github.com/LeapLabTHU/SuperAgent.git
cd SuperAgent

uv python install 3.12
uv venv --python 3.12
source .venv/bin/activate  # Windows: .venv\Scripts\activate

uv sync

# Optional: Required for browser tool
playwright install

# Configure environment variables
cp .env.example .env
# Edit .env file and fill in your API keys

# Start CLI
uv run cli.py
```

**Note**: Windows platform requires additional dependencies, see [Windows Platform Support](./docs/QA.md#windows-platform-support)

## Configuration

Configure the following environment variables in the `.env` file:

```bash
# Reasoning model (for complex reasoning tasks)
REASONING_MODEL=qwen-max-latest

# Basic model (for straightforward tasks)
BASIC_MODEL=qwen-max-latest

# Code model
CODE_MODEL=deepseek-chat

# Vision-language model
VL_MODEL=qwen2.5-vl-72b-instruct

# Browser tool (disabled by default due to long wait time)
USE_BROWSER=False

# Optional: Search API Keys
# TAVILY_API_KEY=
# JINA_API_KEY=
```

# CLI Tool Usage

## Start Interactive Interface

```bash
python cli.py
```

## Launch Mode - Create Stock Analysis Workflow

```bash
run-l -u test -m 'Create a stock analysis expert agent to analyze Xiaomi stock trends. Today is April 22, 2025. Review the past month of Xiaomi stock performance, analyze current hot news about Xiaomi, predict the stock price trend for the next trading day, and provide buy or sell recommendations.'
```

## Polish Mode - Refine Workflow

```bash
run-o -u test
```

## Production Mode - Run Production Workflow

```bash
run-p -u test -w <workflow-id>
```

## Resume from Checkpoint

```bash
resume -w <workflow-id> -s <step-number> -u test
```

## Manage Agents

```bash
# List agents
list-agents -u test

# Edit agent
edit-agent -n <agent-name> -u test

# Remove agent
remove-agent -n <agent-name> -u test
```

## View Tool List

```bash
list-default-tools
```

# Web UI

## Start Web Service

```bash
python cli.py web --host 0.0.0.0 --port 8001
```

Access `http://localhost:8001` to use the web interface.

Web UI provides:
- Task execution panel
- Agent/Tool/Workflow management
- Task history viewing
- Checkpoint visualization
- Checkpoint recovery operations

See [Web UI Guide](./docs/web_ui_guide_zh.md) for details

# MCP Service Integration

SuperAgent supports integrating external services and tools through Model Context Protocol (MCP).

## Configuration

1. Create configuration file:
```bash
cd ./config
cp mcp.json.example mcp.json
```

2. Add MCP services:
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

After configuration, SuperAgent will automatically register MCP services as available tools that agents can invoke during task execution.

Example:
```bash
run-l -u test -m 'Create a navigation agent that uses map-related tools to plan the route from Beijing West Railway Station to the Forbidden City.'
```

# Documentation

- [Architecture](./docs/architecture.md)
- [Frequently Asked Questions (FAQ)](./docs/QA.md)
- [Task Execution Tracking and Recovery](./docs/task_execution_tracking_zh(taskLog_CheckpointResume).md)
- [Remote Invocation](./docs/remote_invocation_zh.md)
- [Business Support Plan](./docs/business_support.md)

# Contributing

We welcome contributions of all forms! Please check out our [contribution guidelines](CONTRIBUTING.md) to learn how to get started.

# Community

Join our WeChat group to ask questions, share experiences, and connect with other developers!

<div align="center">
    <img src="assets/wechat_community.jpg" alt="SuperAgent Community" width="300" />
</div>

# Citation

Core contributors: Zheng Wang, Shenzhi Wang, Yue Wu, Shiji Song, Gao Huang

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

# Acknowledgments

Special thanks to all the open-source projects and contributors that made SuperAgent possible.
