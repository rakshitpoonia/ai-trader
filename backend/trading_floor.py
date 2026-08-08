from .traders import Trader
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

if USE_MANY_MODELS:
    model_names = [
        "gpt-5.5",
        "deepseek-v4-flash",
        "gemini-3.5-flash",
        "grok-4.3",
    ]
    short_model_names = ["GPT 5.5", "DeepSeek V4",
                         "Gemini 3.5 Flash", "Grok 4.3"]
else:
    model_names = ["gpt-5.4-mini"] * 4
    short_model_names = ["GPT 5.4 mini"] * 4
