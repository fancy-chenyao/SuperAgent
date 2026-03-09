@echo off
REM Reproduction script for Cooragent on Windows

echo [1/6] Setup Python environment with uv
call uv python install 3.12
call uv venv --python 3.12

echo [2/6] Activate virtual environment
call .venv\Scripts\activate

echo [3/6] Install dependencies
call uv sync
call pip install pyreadline
call playwright install

echo [4/6] Configure environment variables
if not exist .env (
    copy .env.example .env
    echo [IMPORTANT] .env file created. You MUST edit it and add your API keys (OPENAI_API_KEY, TAVILY_API_KEY, etc.) now.
    echo Press any key after you have edited the .env file...
    pause
)

echo [5/6] Capability Demo 1: Create a single agent (Agent Factory)
echo Creating a simple stock analysis agent...
call uv run cli.py run-l -t agent_factory -u demo_user -m "创建一个股票分析专家 agent. 查看过去一个月的小米股票走势。"

echo [6/6] Capability Demo 2: Create a multi-agent workflow (Agent Workflow)
echo Creating a travel plan workflow...
call uv run cli.py run-l -t agent_workflow -u demo_user -m "规划一个 2025 年五一期间去云南旅游的行程。"

echo Done!
pause