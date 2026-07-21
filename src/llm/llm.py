from __future__ import annotations

import os
from typing import Optional

try:
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover
    ChatOpenAI = None  # type: ignore

try:
    from langchain_deepseek import ChatDeepSeek
except Exception:  # pragma: no cover
    ChatDeepSeek = None  # type: ignore

from src.service.env import (
    REASONING_MODEL,
    REASONING_BASE_URL,
    REASONING_API_KEY,
    BASIC_MODEL,
    BASIC_BASE_URL,
    BASIC_API_KEY,
    VL_MODEL,
    VL_BASE_URL,
    VL_API_KEY,
    CODE_MODEL,
    CODE_BASE_URL,
    CODE_API_KEY,
)
from src.llm.agents import LLMType


def create_openai_llm(
    model: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.0,
    **kwargs,
) -> ChatOpenAI:
    """
    Create a ChatOpenAI instance with the specified configuration
    """
    if ChatOpenAI is None:
        raise RuntimeError(
            "langchain-openai is not installed. Run: pip install langchain-openai"
        )
    # Only include base_url in the arguments if it's not None or empty
    llm_kwargs = {"model": model, "temperature": temperature, **kwargs}

    if base_url:  # This will handle None or empty string
        llm_kwargs["base_url"] = base_url

    if api_key:  # This will handle None or empty string
        llm_kwargs["api_key"] = api_key

    return ChatOpenAI(**llm_kwargs)


def create_deepseek_llm(
    model: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.0,
    **kwargs,
) -> ChatDeepSeek:
    """
    Create a ChatDeepSeek instance with the specified configuration
    """
    if ChatDeepSeek is None:
        raise RuntimeError(
            "langchain-deepseek is not installed. Run: pip install langchain-deepseek"
        )
    # Only include base_url in the arguments if it's not None or empty
    llm_kwargs = {"model": model, "temperature": temperature, **kwargs}

    if base_url:  # This will handle None or empty string
        llm_kwargs["api_base"] = base_url

    if api_key:  # This will handle None or empty string
        llm_kwargs["api_key"] = api_key

    return ChatDeepSeek(**llm_kwargs)


# Cache for LLM instances
_llm_cache: dict[LLMType, ChatOpenAI | ChatDeepSeek] = {}


_PLACEHOLDER_MARKERS = (
    "your_",
    "replace_",
    "replace-me",
    "placeholder",
    "changeme",
)


def _is_configured(value: Optional[str]) -> bool:
    """Return whether a configuration value is non-empty and not a placeholder."""
    if not value or not str(value).strip():
        return False
    normalized = str(value).strip().lower()
    return not any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def _config_for_type(llm_type: LLMType) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if llm_type == "reasoning":
        return REASONING_MODEL, REASONING_BASE_URL, REASONING_API_KEY
    if llm_type == "code":
        return CODE_MODEL, CODE_BASE_URL, CODE_API_KEY
    if llm_type == "basic":
        return BASIC_MODEL, BASIC_BASE_URL, BASIC_API_KEY
    if llm_type == "vision":
        return VL_MODEL, VL_BASE_URL, VL_API_KEY
    raise ValueError(f"Unknown LLM type: {llm_type}")


def get_llm_configuration_status() -> dict:
    """Return secret-free model readiness information for diagnostics and the UI."""
    details = {}
    for llm_type in ("basic", "reasoning", "code", "vision"):
        model, base_url, api_key = _config_for_type(llm_type)  # type: ignore[arg-type]
        effective_key = api_key or os.getenv("OPENAI_API_KEY")
        missing = []
        if not _is_configured(model):
            missing.append("model")
        if not _is_configured(effective_key):
            missing.append("api_key")
        details[llm_type] = {
            "configured": not missing,
            "model": model or None,
            "base_url_configured": _is_configured(base_url),
            "missing": missing,
        }

    required_types = ("basic", "reasoning")
    return {
        "configured": all(details[item]["configured"] for item in required_types),
        "required_types": list(required_types),
        "details": details,
    }


def get_llm_by_type(llm_type: LLMType) -> ChatOpenAI | ChatDeepSeek:
    """
    Get LLM instance by type. Returns cached instance if available.
    """
    if llm_type in _llm_cache:
        return _llm_cache[llm_type]

    model, base_url, api_key = _config_for_type(llm_type)
    effective_key = api_key or os.getenv("OPENAI_API_KEY")
    missing = []
    if not _is_configured(model):
        missing.append("model")
    if not _is_configured(effective_key):
        missing.append("api_key")
    if missing:
        raise RuntimeError(
            f"LLM '{llm_type}' is not configured: missing {', '.join(missing)}"
        )

    if llm_type == "reasoning":
        llm = create_openai_llm(
            model=model,
            base_url=base_url,
            api_key=effective_key,
        )
    elif llm_type == "code":
        llm = create_openai_llm(
            model=model,
            base_url=base_url,
            api_key=effective_key,
        )
    elif llm_type == "basic":
        llm = create_openai_llm(
            model=model,
            base_url=base_url,
            api_key=effective_key,
        )
    elif llm_type == "vision":
        llm = create_openai_llm(
            model=model,
            base_url=base_url,
            api_key=effective_key,
        )
    else:
        raise ValueError(f"Unknown LLM type: {llm_type}")

    _llm_cache[llm_type] = llm
    return llm


if __name__ == "__main__":
    reasoning_llm = get_llm_by_type("reasoning")
    basic_llm = get_llm_by_type("basic")
    vl_llm = get_llm_by_type("vision")
    stream = reasoning_llm.stream("what is mcp?")
    full_response = ""
    for chunk in stream:
        full_response += chunk.content
    print(full_response)

    basic_llm.invoke("Hello")
    vl_llm.invoke("Hello")
