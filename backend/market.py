"""Share prices from the Massive market data API, or a simulator when no key is set.

Massive API uses live data. Without it, prices come from market_simulator
making trades on simulated prices. This still runs out of the box.
"""


import os
import time
from dotenv import load_dotenv
from massive import RESTClient

from .market_simulator import simulated_price

load_dotenv(override=True)

massive_api_key = os.getenv("MASSIVE_API_KEY")


def _last_trade(client: RESTClient, symbol: str) -> float:
    return float(client.get_last_trade(symbol).price)


def _snapshot(client: RESTClient, symbol: str) -> float:
    snapshot = client.get_snapshot_ticker("stocks", symbol)
    return float(snapshot.min.close or snapshot.prev_day.close)


def _previous_close(client: RESTClient, symbol: str) -> float:
    return float(client.get_previous_close_agg(symbol)[0].close)


price_methods = [_last_trade, _snapshot, _previous_close]
plan_tier = 0


# A symbol must keep the same price source for the whole process. Real and simulated prices
# for one ticker can differ by a factor of two, so a position bought at a simulated price and
# later valued at a real one shows a large invented profit or loss. Massive's cheaper plans
# also rate limit, which made the source flip mid-run; that is what these two caches prevent.
PRICE_TTL_SECONDS = 60.0
_price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, monotonic time)
_simulated_symbols: set[str] = set()  # symbols Massive never priced; simulated from now on


def get_share_price(symbol: str) -> float:
    """Return the current price for a symbol, from Massive or the simulator.

    Repeated lookups inside PRICE_TTL_SECONDS reuse the cached price, which keeps a single
    agent turn self-consistent and keeps four traders from exhausting the rate limit.
    """
    symbol = symbol.upper()
    cached = _price_cache.get(symbol)
    if cached and time.monotonic() - cached[1] < PRICE_TTL_SECONDS:
        return cached[0]

    if massive_api_key and symbol not in _simulated_symbols:
        try:
            price = get_share_price_massive(symbol)
            _price_cache[symbol] = (price, time.monotonic())
            return price
        except Exception as e:
            # Serve the last real price rather than crossing over to simulated ones. Stale
            # is better than inconsistent: a rate limit lasts seconds, the distortion lasts
            # for as long as the position is held.
            if cached:
                print(f"Massive API unavailable ({e}); reusing last price for {symbol}")
                return cached[0]
            print(f"Massive API unavailable ({e}); {symbol} is now priced by the simulator")
            _simulated_symbols.add(symbol)

    price = simulated_price(symbol)
    _price_cache[symbol] = (price, time.monotonic())
    return price


# Best price first, previous close last. Lower tier plans reject the earlier calls
# so we remember the first tier that works and start there next time.
def get_share_price_massive(symbol: str) -> float:
    """Best price the plan allows, remembering the working tier to avoid repeat failures."""
    global plan_tier
    client = RESTClient(massive_api_key)
    for tier in range(plan_tier, len(price_methods)):
        try:
            price = price_methods[tier](client, symbol)
            plan_tier = tier
            return price
        except Exception:
            continue
    raise RuntimeError(f"No Massive price available for {symbol}")


def is_market_open() -> bool:
    """Whether the US market is open; True on simulated data or if Massive is unreachable."""
    if not massive_api_key:
        return True
    try:
        client = RESTClient(massive_api_key)
        return client.get_market_status().market == "open"
    except Exception:
        return True
