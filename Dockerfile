FROM python:3.12-slim as builder
ENV REASONING_API_KEY=sk-***
ENV REASONING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ENV REASONING_MODEL=deepseek-r1
ENV BASIC_API_KEY=sk-***
ENV BASIC_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ENV BASIC_MODEL=qwen-max-latest
ENV CODE_API_KEY=sk-***
ENV CODE_BASE_URL=https://api.deepseek.com/v1
ENV CODE_MODEL=deepseek-chat
ENV Generate_avatar_API_KEY=sk-***
ENV Generate_avatar_BASE_URL=https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis
ENV Generate_avatar_MODEL=wanx2.0-t2i-turbo
ENV VL_API_KEY=sk-***
ENV VL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ENV VL_MODEL=qwen2.5-vl-72b-instruct
ENV APP_ENV=development
ENV TAVILY_API_KEY=tvly-dev-***
ENV ANONYMIZED_TELEMETRY=false
ENV SLACK_USER_TOKEN=***

# -------------- Internal Network Environment Configuration --------------
# Set fixed timezone (commonly used in internal networks)
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# -------------- Build Phase  --------------
# Install the internally customized uv tool (specific version + internal network source)
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv

# -------------- Project Preparation --------------
WORKDIR /app
COPY pyproject.toml .
COPY . /app
COPY .env /app/.env

ENV http_proxy=**
ENV https_proxy=**
ENV NO_PROXY=**

# -------------- Virtual Environment Setup --------------
# Create a virtual environment (specify internal Python 3.12)
RUN uv python install 3.12
RUN uv venv --python 3.12
ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# Activate the environment and install dependencies (internal mirror source)
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN uv sync

EXPOSE 9000

# Startup command (internal network listening configuration)
CMD ["uv", "run", "src/service/app.py","--port", "9000"]
