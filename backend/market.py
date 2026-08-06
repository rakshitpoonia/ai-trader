"""Share prices from the Massive market data API, or a simulator when no key is set.

Massive API uses live data. Without it, prices come from market_simulator
making trades on simulated prices. This still runs out of the box.
"""


import os
from dotenv import load_dotenv
from massive import RESTClient

load_dotenv(override=True)

massive_api_key = os.getenv("MASSIVE_API_KEY")
