# Cooragent 能力复现指南

本指南将帮助你快速复现 Cooragent 项目的核心能力。你可以直接运行 `reproduce_demo.bat` 脚本，或者按照以下步骤手动执行。

## 1. 环境准备

确保你已经安装了 `uv` (一个极速 Python 包管理器)。如果未安装，请访问 [uv 官网](https://github.com/astral-sh/uv) 安装。

### 1.1 创建虚拟环境

```bash
uv python install 3.12
uv venv --python 3.12
```

### 1.2 激活环境 (Windows)

```bash
.venv\Scripts\activate
```

### 1.3 安装依赖

```bash
uv sync
pip install pyreadline  # Windows 特需
playwright install      # 用于浏览器工具
```

## 2. 配置

复制配置文件示例并填入你的 API Key。

```bash
copy .env.example .env
```

**重要**：请编辑 `.env` 文件，填入必要的 API Key，例如 `OPENAI_API_KEY`, `TAVILY_API_KEY` 等。

## 3. 能力复现

### 3.1 Agent Factory (单体智能体生成)

一句话创建一个具备特定能力的智能体。

```bash
uv run cli.py run-l -t agent_factory -u demo_user -m "创建一个股票分析专家 agent. 查看过去一个月的小米股票走势，分析当前小米的热点新闻，预测下个交易日的股价走势。"
```

### 3.2 Agent Workflow (多智能体协作)

一句话创建一个多智能体协作的工作流。

```bash
uv run cli.py run-l -t agent_workflow -u demo_user -m "为我规划一个 2025 年五一期间去云南旅游的行程。首先爬取云南旅游景点信息，然后规划5天行程，最后生成一份 PDF 报告。"
```

### 3.3 查看已创建的智能体

```bash
uv run cli.py list-agents -u demo_user
```

### 3.4 生产模式运行 (Run Production)

运行之前创建并保存的工作流。

```bash
uv run cli.py run-p -u demo_user
```
(按提示选择要运行的工作流 ID)

## 4. 常见问题

- **Windows 下乱码或报错**：确保安装了 `pyreadline`。
- **API Key 错误**：检查 `.env` 文件配置。
- **Browser 工具超时**：首次运行时 Playwright 需要下载浏览器内核，可能较慢。