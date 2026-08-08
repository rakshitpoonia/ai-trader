from .traders import Trader, FREE_MODEL_ROUTER
from typing import List
import asyncio
from .tracers import LogTracer
from agents import add_trace_processor
from .market import is_market_open
from dotenv import load_dotenv
import os

load_dotenv(override=True)


RUN_EVERY_N_MINUTES = int(os.getenv("RUN_EVERY_N_MINUTES", "60"))
RUN_EVEN_WHEN_MARKET_IS_CLOSED = (
    os.getenv("RUN_EVEN_WHEN_MARKET_IS_CLOSED",
              "false").strip().lower() == "true"
)
USE_MANY_MODELS = os.getenv(
    "USE_MANY_MODELS", "false").strip().lower() == "true"

names = ["Warren", "George", "Ray", "Cathie"]
lastnames = ["Patience", "Bold", "Systematic", "Crypto"]

# USE_MANY_MODELS=true pits four different frontier models against each other, one per
# trader, each called directly on its own provider's endpoint. Needs a paid API key for
# every provider listed here (see get_model in traders.py).
if USE_MANY_MODELS:
    model_names = [
        "gpt-5.5",
        "deepseek-v4-flash",
        "gemini-3.5-flash",
        "grok-4.3",
    ]
    short_model_names = ["GPT 5.5", "DeepSeek V4",
                         "Gemini 3.5 Flash", "Grok 4.3"]
# The default: all four traders run on OpenRouter's free-models router, so the only key
# needed is OPENROUTER_API_KEY. "openrouter/free" resolves per request to whichever free
# model is available at that moment and supports tool calling, which means:
#   - the model behind a trader changes between runs, so differences in their results
#     reflect their strategies plus whatever model answered, not the strategies alone;
#   - free models share a daily request cap on OpenRouter, and four traders each taking up
#     to MAX_TURNS turns per cycle can burn through it, so keep RUN_EVERY_N_MINUTES
#     generous - a run that stops early on a rate limit is caught and logged by Trader.run.
# For a repeatable comparison, pin a specific slug here instead.
else:
    model_names = [FREE_MODEL_ROUTER] * 4
    short_model_names = ["OpenRouter Free"] * 4


def create_traders() -> List[Trader]:
    traders = []
    for name, lastname, model_name in zip(names, lastnames, model_names):
        traders.append(Trader(name, lastname, model_name))
    return traders


async def run_every_n_minutes():

    add_trace_processor(LogTracer())
    traders = create_traders()
    while True:
        if RUN_EVEN_WHEN_MARKET_IS_CLOSED or is_market_open():
            await asyncio.gather(*[trader.run() for trader in traders])
        else:
            print("Market is closed, skipping run")
        await asyncio.sleep(RUN_EVERY_N_MINUTES * 60)


if __name__ == "__main__":
    print(f"Starting scheduler to run every {RUN_EVERY_N_MINUTES} minutes")
    asyncio.run(run_every_n_minutes())
