from typing import Any

from contextlib import AsyncExitStack
from functools import lru_cache
from .accounts_client import read_accounts_resource, read_strategy_resource
from agents import Agent, Tool, Runner, OpenAIChatCompletionsModel, trace
from .database import write_log
from .rate_limits import rate_limit_message
from .tracers import make_trace_id
from openai import AsyncOpenAI
from dotenv import load_dotenv
import os
import json
from .templates import (
    researcher_instructions,
    trader_instructions,
    trade_message,
    rebalance_message,
    research_tool,
)
from .mcp_servers import trader_mcp_servers, researcher_mcp_servers

load_dotenv(override=True)

deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
google_api_key = os.getenv("GOOGLE_API_KEY")
grok_api_key = os.getenv("GROK_API_KEY")
openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
GROK_BASE_URL = "https://api.x.ai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

MAX_TURNS = 30

# OpenRouter's free-models router. Not a single model: for each request OpenRouter picks
# at random from the free models available at that moment, keeping only those that support
# what the request needs - tool calling, in our case, since the traders are useless without
# it. Every trader runs on this by default, so the project costs nothing to run with just
# an OPENROUTER_API_KEY. See backend/trading_floor.py for the trade-offs.
FREE_MODEL_ROUTER = "openrouter/free"


# One OpenAI-compatible client per provider, built on first use and then reused.
# They are deliberately not created at import time: AsyncOpenAI raises when its key is
# missing, so building all four eagerly would stop the default FREE_MODEL_ROUTER path from
# running for anyone who only has an OpenRouter key set.
@lru_cache(maxsize=None)
def get_client(base_url: str, api_key: str | None, provider: str) -> AsyncOpenAI:
    if not api_key:
        raise RuntimeError(
            f"No API key for {provider}; set it in .env or use the default "
            f"'{FREE_MODEL_ROUTER}' model (USE_MANY_MODELS=false)."
        )
    return AsyncOpenAI(base_url=base_url, api_key=api_key)


def get_model(model_name: str):
    # A "provider/model" slug is an OpenRouter one - this is the branch FREE_MODEL_ROUTER
    # takes, and it must stay first, since slugs like "deepseek/deepseek-chat" name a
    # provider that also has a direct endpoint below.
    if "/" in model_name:
        return OpenAIChatCompletionsModel(
            model=model_name,
            openai_client=get_client(
                OPENROUTER_BASE_URL, openrouter_api_key, "OpenRouter"),
        )
    # Bare model names go direct to the provider that serves them, each over its own
    # OpenAI-compatible endpoint. Used when USE_MANY_MODELS=true.
    elif "deepseek" in model_name:
        return OpenAIChatCompletionsModel(
            model=model_name,
            openai_client=get_client(
                DEEPSEEK_BASE_URL, deepseek_api_key, "DeepSeek"),
        )
    elif "grok" in model_name:
        return OpenAIChatCompletionsModel(
            model=model_name,
            openai_client=get_client(GROK_BASE_URL, grok_api_key, "Grok"),
        )
    elif "gemini" in model_name:
        return OpenAIChatCompletionsModel(
            model=model_name,
            openai_client=get_client(
                GEMINI_BASE_URL, google_api_key, "Gemini"),
        )
    # Anything else is an OpenAI model name; the Agents SDK's own client handles it.
    else:
        return model_name


async def get_researcher(mcp_servers, model_name) -> Agent:
    researcher = Agent[Any](
        name="Researcher",
        instructions=researcher_instructions(),
        model=get_model(model_name),
        mcp_servers=mcp_servers,
    )
    return researcher


async def get_researcher_tool(mcp_servers, model_name) -> Tool:
    researcher = await get_researcher(mcp_servers, model_name)
    return researcher.as_tool(tool_name="Researcher", tool_description=research_tool())


class Trader:
    def __init__(self, name: str, lastname="Trader", model_name=FREE_MODEL_ROUTER):
        self.name = name
        self.lastname = lastname
        self.agent = None
        self.model_name = model_name
        self.do_trade = True

    async def create_agent(self, trader_mcp_servers, researcher_mcp_servers) -> Agent:
        tool = await get_researcher_tool(researcher_mcp_servers, self.model_name)
        self.agent = Agent(
            name=self.name,
            instructions=trader_instructions(self.name),
            model=get_model(self.model_name),
            tools=[tool],
            mcp_servers=trader_mcp_servers,
        )
        return self.agent

    async def get_account_report(self) -> str:
        account = await read_accounts_resource(self.name)
        account_json = json.loads(account)
        account_json.pop("portfolio_value_time_series", None)
        return json.dumps(account_json)

    async def run_agent(self, trader_mcp_servers, researcher_mcp_servers):
        self.agent = await self.create_agent(trader_mcp_servers, researcher_mcp_servers)
        account = await self.get_account_report()
        strategy = await read_strategy_resource(self.name)
        message = (
            trade_message(self.name, strategy, account)
            if self.do_trade
            else rebalance_message(self.name, strategy, account)
        )
        await Runner.run(self.agent, message, max_turns=MAX_TURNS)

    async def run_with_mcp_servers(self):
        async with AsyncExitStack() as stack:
            trader_servers = [
                await stack.enter_async_context(server) for server in trader_mcp_servers()
            ]
            researcher_servers = [
                await stack.enter_async_context(server)
                for server in researcher_mcp_servers(self.name)
            ]
            await self.run_agent(trader_servers, researcher_servers)

    async def run_with_trace(self):
        trace_name = f"{self.name}-trading" if self.do_trade else f"{self.name}-rebalancing"
        trace_id = make_trace_id(f"{self.name.lower()}")
        with trace(trace_name, trace_id=trace_id):
            await self.run_with_mcp_servers()

    async def run(self):
        try:
            await self.run_with_trace()
        except Exception as e:
            # A 429 is the one failure the dashboard should explain rather than just record:
            # nothing is broken, the provider's limit is spent until the time it reported.
            message = rate_limit_message(e)
            if message:
                write_log(self.name, "rate_limit", message)
            else:
                message = f"Error running trader {self.name}: {e}"
            print(message)
        self.do_trade = not self.do_trade
