"""Share prices from the Massive market data API, or a simulator when no key is set.

Massive API uses live data. Without it, prices come from market_simulator
making trades on simulated prices. This still runs out of the box.
"""


import os
from dotenv import load_dotenv
from massive import RESTClient

load_dotenv(override=True)

massive_api_key = os.getenv("MASSIVE_API_KEY")


def _last_trade(client: RESTClient, symbol: str) -> float:
    return float(client.get_last_trade(symbol).price)


def _snapshot(client: RESTClient, symbol: str) -> float:
    snapshot = client.get_snapshot_ticker("stocks", symbol)
    return float(snapshot.min.close or snapshot.prev_day.close)


def _previous_close(client: RESTClient, symbol: str) -> float:
    return float(client.get_previous_close_agg(symbol)[0].close)
