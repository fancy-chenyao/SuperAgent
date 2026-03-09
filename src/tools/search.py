import logging
import datetime
import functools
import re
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.tools import BaseTool
from .decorators import create_logged_tool

TAVILY_MAX_RESULTS = 5
logger = logging.getLogger(__name__)


# Templates for English and Chinese
FORMAT_TEMPLATE = {
    "en": "Now is {CURRENT_TIME}, {query}",
    "zh": "当前时间是: {CURRENT_TIME}, {query}",
}

def contains_chinese(text: str) -> bool:
    """Checks if the string contains at least one Chinese character (U+4E00-U+9FFF)."""
    if not text: 
        return False
    return bool(re.search(r'[\u4e00-\u9fff]', text))

def inject_current_time(tool_cls: type[BaseTool]) -> type[BaseTool]:
    """
    Class decorator to inject the current time into the input dictionary of a LangChain tool's invoke/ainvoke methods.
    Selects different formatting templates based on the query language (Chinese/English).
    Logs a warning or debug message if the input is not a dictionary or lacks the 'query' key.
    """
    original_invoke = getattr(tool_cls, 'invoke', None)
    original_ainvoke = getattr(tool_cls, 'ainvoke', None)

    if original_invoke:
        @functools.wraps(original_invoke)
        def invoke(self, input: dict | str, config=None, **kwargs):
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:00")
            processed_input = input

            if isinstance(input, dict):
                original_query = input.get('query')
                if original_query:
                    processed_input = input.copy()
                    processed_input['current_time'] = current_time

                    # Determine language and select template
                    lang = 'zh' if contains_chinese(original_query) else 'en'
                    template = FORMAT_TEMPLATE.get(lang, FORMAT_TEMPLATE['en'])
                    processed_input['query'] = template.format(
                        CURRENT_TIME=current_time, query=original_query
                    )
                    logger.debug(f"Injected time using '{lang}' template, now processed_input={processed_input} into invoke input") # Updated log message
                else:
                    logger.debug(f"Input dictionary {input} lacks 'query' key or value is empty, skipping time injection.")
            else:
                logger.warning(
                    f"Input type {type(input)} for invoke is not a dictionary. "
                    f"Cannot inject 'current_time'."
                )
            return original_invoke(self, processed_input, config=config, **kwargs)
        setattr(tool_cls, 'invoke', invoke)

    if original_ainvoke:
        @functools.wraps(original_ainvoke)
        async def ainvoke(self, input: dict | str, config=None, **kwargs):
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:00")
            processed_input = input 

            if isinstance(input, dict):
                original_query = input.get('query')
                if original_query:
                    processed_input = input.copy()
                    processed_input['current_time'] = current_time

                    # Determine language and select template
                    lang = 'zh' if contains_chinese(original_query) else 'en'
                    template = FORMAT_TEMPLATE.get(lang, FORMAT_TEMPLATE['en'])

                    processed_input['query'] = template.format(
                        CURRENT_TIME=current_time, query=original_query
                    )
                    logger.debug(f"Injected time using '{lang}' template, now processed_input={processed_input} into ainvoke input") # Updated log message
                else:
                    logger.debug(f"Input dictionary {input} lacks 'query' key or value is empty, skipping time injection.")
            else:
                logger.warning(
                    f"Input type {type(input)} for ainvoke is not a dictionary. "
                    f"Cannot inject 'current_time'."
                )
            return await original_ainvoke(self, processed_input, config=config, **kwargs)
        setattr(tool_cls, 'ainvoke', ainvoke)

    return tool_cls

TimeInjectedTavily = inject_current_time(TavilySearchResults)
LoggedTimeInjectedTavily = create_logged_tool(TimeInjectedTavily)
tavily_tool = LoggedTimeInjectedTavily(name="tavily_tool", max_results=TAVILY_MAX_RESULTS)
