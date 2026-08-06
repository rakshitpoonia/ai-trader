import os
from pathlib import Path
from dotenv import load_dotenv
from agents.mcp import MCPServerStdio, create_static_tool_filter
from .market import massive_api_key

load_dotenv(override=True)

PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
tavily_env = {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY")}
TIMEOUT = 120


# The market data server for the trader.
# With a key, hand the agent Massive's own market data server, run locally over stdio.
# Without one, use simulated prices.
if massive_api_key:
    market_params = {
        "command": "uvx",
        "args": ["--from", "git+https://github.com/massive-com/mcp_massive@v0.10.0", "mcp_massive"],
        "env": {"MASSIVE_API_KEY": massive_api_key},
    }
else:
    market_params = {"command": "uv", "args": [
        "run", "-m", "backend.market_server"], "cwd": PROJECT_DIR}


# The trader's MCP servers: our Accounts server, Push Notification and Market data.
def trader_mcp_servers() -> list[MCPServerStdio]:
    """The trader's MCP servers: our Accounts server, Push Notification and Market data."""
    params = [
        {"command": "uv", "args": [
            "run", "-m", "backend.accounts_server"], "cwd": PROJECT_DIR},
        {"command": "uv", "args": ["run", "-m",
                                   "backend.push_server"], "cwd": PROJECT_DIR},
        market_params,
    ]
    return [MCPServerStdio(p, client_session_timeout_seconds=TIMEOUT) for p in params]
