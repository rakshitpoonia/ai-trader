# Backend Architecture — Interview Revision Guide

Everything worth being able to say about this codebase: the architecture and why it is shaped that
way, the OOP design, the patterns that are unusual enough to be worth pointing at, and the
trade-offs behind each decision.

For the runtime narrative — what an agent actually does during a cycle, turns, tool calls — see
[AGENT_CYCLE.md](AGENT_CYCLE.md). This file is the _structure_; that one is the _behaviour_.

---

## 0. The 60-second pitch

> Four LLM traders, each with its own account, strategy, and persistent memory, run on a scheduler
> and trade a simulated portfolio. Each one is an agent built on the OpenAI Agents SDK, and every
> capability it has — reading its balance, buying shares, searching the web, remembering what it
> learned last week — is exposed as an **MCP server running as a separate child process**, spoken to
> over stdin/stdout. The agent orchestrator never imports the domain model; they communicate only
> across that process boundary. Traders alternate between a _trade_ phase and a _rebalance_ phase,
> and each one has a Researcher sub-agent that is wrapped as a single tool so its noisy web searches
> never pollute the trader's context. Everything the agents do is traced into SQLite, and a Gradio
> dashboard reads that database live.

**If they ask "what's the most interesting engineering decision?"** — the process boundary between
the agent and the money (§2). That one choice explains most of the rest of the design.

**If they ask "what did you learn?"** — that in an agent system the type annotations and docstrings
are not documentation, they are _prompt surface_. A wrong return-type annotation on an MCP tool is a
lie told to the model (§5.2).

### At a glance

|                     |                                                                                              |
| ------------------- | -------------------------------------------------------------------------------------------- |
| Stack               | Python 3.12, `openai-agents` SDK, MCP over stdio, FastMCP, Pydantic v2, SQLite, Gradio, `uv` |
| Agents              | 4 traders × 1 Researcher sub-agent each                                                      |
| Processes per cycle | 1 scheduler + 4 traders × 6 MCP servers ≈ 25                                                 |
| Concurrency         | one event loop, `asyncio.gather` over 4 traders                                              |
| Turn cap            | `MAX_TURNS = 13` per run (`traders.py:43`), plus `RESEARCHER_MAX_TURNS = 6` nested           |
| Classes we define   | 5 (plus 2 in the dashboard)                                                                  |
| Persistence         | one SQLite file, two tables, account stored as a single JSON blob                            |
| Cost to run         | $0 — default path is OpenRouter's free-model router                                          |

---

## 1. Map of the codebase

```
                       ┌──────────────────────────────────────────┐
  entry points         │ trading_floor.py    reset.py             │
                       │ app.py → demo/ui.py     api.py           │
                       └──────────────────┬───────────────────────┘
                                          │
  agent orchestration  ┌──────────────────▼───────────────────────┐
                       │ traders.py    templates.py               │
                       │ mcp_servers.py    tracers.py             │
                       └──────────────────┬───────────────────────┘
                                          │ MCP / stdio  ← process boundary
  capability servers   ┌──────────────────▼───────────────────────┐
                       │ accounts_server.py  market_server.py     │
                       │ push_server.py      accounts_client.py   │
                       └──────────────────┬───────────────────────┘
                                          │
  domain model         ┌──────────────────▼───────────────────────┐
                       │ accounts.py  (Account, Transaction)      │
                       └──────────────────┬───────────────────────┘
                                          │
  infrastructure       ┌──────────────────▼───────────────────────┐
                       │ database.py   market.py                  │
                       │               market_simulator.py        │
                       └──────────────────────────────────────────┘
```

| module                 | imports                                                  | role                                       |
| ---------------------- | -------------------------------------------------------- | ------------------------------------------ |
| `trading_floor.py`     | `traders`, `tracers`, `market`                           | scheduler, roster, model selection         |
| `traders.py`           | `accounts_client`, `templates`, `mcp_servers`, `tracers` | **not** `accounts` — see §2                |
| `mcp_servers.py`       | `market` (key only)                                      | builds the six server configs              |
| `templates.py`         | `market` (key only)                                      | all agent-facing prose                     |
| `tracers.py`           | `database`                                               | turns SDK spans into log rows              |
| `accounts_client.py`   | —                                                        | spawns `accounts_server` to read resources |
| `accounts_server.py`   | `accounts`                                               | the MCP face of the domain model           |
| `accounts.py`          | `market`, `database`                                     | the domain model                           |
| `market.py`            | `market_simulator`                                       | pricing with fallback                      |
| `api.py`, `demo/ui.py` | `accounts`, `database`, `trading_floor`                  | read-only frontends                        |

Dependency arrows only ever point downward, with one deliberate exception: `templates.py` and
`mcp_servers.py` both reach into `market.py` for the single flag `massive_api_key`. Prompts and
server configuration _must_ agree about which market tools exist — if the prompt described tools the
server does not expose, the model would call tools that don't exist. Sharing one flag from one module
is what keeps them in step, and it is why a file of pure prose imports from the pricing layer.

Two more things worth being able to explain from that table:

**`api.py` and `demo/ui.py` import `trading_floor`** — not for the scheduler, for `names`,
`lastnames` and `short_model_names`. The roster is defined once at module scope and the read-only
frontends import it so they cannot drift out of sync with the process that actually runs the traders.
Safe because the loop is behind `if __name__ == "__main__"`; only the constants execute.

**The frontends bypass MCP entirely.** They import `Account` and read `accounts.db` in-process. That
is correct — they are readers, not agents, so the process boundary buys them nothing.

There is no `__init__.py`; `backend` works as a namespace package with relative imports, which is why
everything launches as `uv run -m backend.<module>` from the project root.

---

## 2. The headline decision: a process boundary between the agent and the money

**The single most important structural fact: `backend/traders.py` never imports
`backend/accounts.py`.** The orchestrator that runs the LLM and the domain model that holds the
balances live in _different operating-system processes_, joined only by MCP over stdio.

```
┌─ scheduler process ─────────────────────┐   ┌─ child process ──────────────┐
│ trading_floor.py                        │   │ accounts_server.py           │
│   └─ traders.py                         │   │   └─ accounts.py  (Account)  │
│        └─ accounts_client.py ───stdio───┼──►│        └─ database.py        │
│        └─ mcp_servers.py                │   │        └─ market.py          │
└─────────────────────────────────────────┘   └───────────────┬──────────────┘
                                                              ▼
                                                        accounts.db
```

A direct function call would have been a hundred times cheaper. What the boundary buys:

- **A uniform capability surface.** The trader's tools, the researcher's tools, and third-party tools
  (Tavily, fetch, libsql, Massive) are all MCP servers. Adding a capability never touches
  `traders.py` — it adds an entry to `mcp_servers.py`. Our own `accounts_server` and a package
  someone else wrote are wired up identically, because to the SDK they are the same kind of thing.
- **Blast radius.** A crash inside a tool kills a child process, not the scheduler. A hung tool hits
  `client_session_timeout_seconds=120` rather than wedging the event loop.
- **Language independence.** Three of the six servers per run are Node (`npx`) or `uvx` packages, not
  our code. The contract is a program on stdin/stdout, not a Python import.
- **A real security story.** The agent cannot reach the database except through the five tools we
  chose to expose. There is no object graph for a confused model to walk.

The cost is equally real, and stating it unprompted is the good move in an interview: every account
read is a subprocess spawn plus a handshake, and four traders × six servers means ~25 live child
processes per cycle.

---

## 3. Concurrency and lifetime

```python
# backend/trading_floor.py:71
while True:
    if RUN_EVEN_WHEN_MARKET_IS_CLOSED or is_market_open():
        await asyncio.gather(*[trader.run() for trader in traders])
    else:
        print("Market is closed, skipping run")
    await asyncio.sleep(RUN_EVERY_N_MINUTES * 60)
```

Four traders run **concurrently on a single event loop**. Not threads, not processes — the work is
almost entirely I/O (model calls, HTTP, subprocess pipes), so `asyncio` is the right tool and there
is no shared mutable state between traders to protect.

Three consequences worth knowing:

- **`gather` waits for all four.** The cycle is as slow as the slowest trader. Since exceptions are
  caught inside `Trader.run` (§7), no trader's failure can cancel the other three.
- **There is no ambient "current trader".** Four interleaved coroutines write spans into one log
  table, so attribution has to be carried explicitly — which is exactly why the trader's name is
  encoded into the trace id (§5.4).
- **Sleep is inside the loop, after the work.** So the real period is `RUN_EVERY_N_MINUTES` _plus_
  the runtime, not a fixed cadence.

### Object lifetimes — the census

| object                        | created in                           | how many             | lifetime                                 |
| ----------------------------- | ------------------------------------ | -------------------- | ---------------------------------------- |
| `Trader` ×4                   | `trading_floor.create_traders()`     | 4                    | whole process                            |
| `LogTracer`                   | `run_every_n_minutes()`              | 1                    | whole process                            |
| `AsyncOpenAI`                 | `get_client()`                       | ≤1 per provider      | process, `lru_cache`d                    |
| `Agent` (trader + Researcher) | `Trader.create_agent()`              | 2 per run per trader | one run                                  |
| `MCPServerStdio` ×6           | `mcp_servers.*_mcp_servers()`        | 6 per run per trader | one run, via `AsyncExitStack`            |
| `FastMCP`                     | module scope of each `*_server.py`   | 1 per server process | that subprocess                          |
| `Account`                     | `Account.get()`                      | one per MCP call     | milliseconds — fetch, mutate, save, drop |
| `Transaction`                 | `Account.buy_shares` / `sell_shares` | one per trade        | persisted inside the account             |
| `demo.ui.Trader` ×4           | `create_ui()`                        | 4                    | dashboard process                        |

**The gradient is the point.** Objects near the top are long-lived and thin; near the bottom,
short-lived and thick. `Trader` holds one boolean for hours; `Account` holds a person's entire
financial state for a few milliseconds. Both are correct: the boolean must survive between runs and
cannot be recomputed, while the account must never be stale, because a child process may have
rewritten the row since you last looked.

---

## 4. OOP design

Five classes we define, plus two in the dashboard. **This is not an inheritance-heavy codebase** —
there is exactly one subclass in it. It is composition and plain functions, with classes used only
where there is genuine state or a genuine schema. Being able to say _why something is not a class_ is
usually more convincing than defending one that is.

### 4.1 `Transaction(BaseModel)` — a value object

Symbol, quantity, price, timestamp, rationale. Immutable in practice; nothing mutates one after
construction.

- **Sign encodes direction.** A sell stores `quantity=-quantity`. No `side` field, no `Buy`/`Sell`
  subclass. Consumers rely on it: `api.average_cost` filters `t.quantity > 0` to find buys, and
  `calculate_profit_loss` sums signed totals so buys and sells net out. A two-class hierarchy would
  have bought polymorphism nobody needed and cost the free arithmetic.
- **`rationale` is a required field.** The model must justify every trade in prose, and that prose is
  persisted. No computation uses it — it exists so the transaction log is readable. **A prompt-design
  decision expressed as a schema constraint**, which is a nice thing to be able to point at.

### 4.2 `Account(BaseModel)` — active record, and the only class with real invariants

```python
class Account(BaseModel):
    name: str
    balance: float
    strategy: str
    holdings: dict[str, int]
    transactions: list[Transaction]
    portfolio_value_time_series: list[tuple[str, float]]
```

The model and its persistence are the same object: `save()` calls
`write_account(self.name.lower(), self.model_dump())`, serialising the whole account — every
transaction, every historical value point — into one JSON blob in one row.

Its lifecycle is unusual and worth stating plainly:

```
Account.get(name)  →  mutate  →  save()  →  discard
```

Instances are **never held**. `accounts_server` does `Account.get(name).buy_shares(...)` on one line.
Every MCP call is a complete read–modify–write against SQLite: no in-memory cache, no identity map,
last-write-wins. Safe in practice only because each trader touches only its own row and a single
trader's turns are sequential.

Behaviour worth knowing (all good "did you think about X?" answers):

- **`SPREAD = 0.002` is charged against the trader on both sides** — buys at `price * 1.002`, sells
  at `price * 0.998`. A position is ~0.2% underwater the moment it opens, so small negative P&L right
  after a buy is correct, not a bug. It also means an agent that churns positions bleeds money, which
  is a real signal in the results.
- **`report()` is not a pure getter.** It appends to `portfolio_value_time_series`, calls `save()`,
  and writes a log row — every time. Since `buy_shares` and `sell_shares` both end with `report()`,
  the value series is a record of _observations clustered around activity_, not an even time series.
- **`Account.get(name)` never raises.** It lowercases the name and mints a fresh $10k account if the
  row is missing. A typo silently creates a new trader.

### 4.3 `Trader` — the deliberate contrast

```python
class Trader:
    def __init__(self, name, lastname="Trader", model_name=FREE_MODEL_ROUTER):
        self.name = name; self.lastname = lastname
        self.agent = None; self.model_name = model_name
        self.do_trade = True
```

`Account` is a heavyweight persistent object created fresh per operation. `Trader` is a
near-stateless object that lives for the whole process. It holds exactly one piece of mutable state
that matters — `do_trade`, which alternates trade → rebalance → trade — and `self.agent`, which is
really a scratch variable (assigned in `create_agent`, used in the same call chain, overwritten next
run; it could be a local).

**The method chain is the design detail to point at:**

```
run()                    ← error containment      (traders.py:159)
  └── run_with_trace()   ← observability          (traders.py:153)
      └── run_with_mcp_servers()  ← resource lifetime  (traders.py:142)
          └── run_agent()         ← business logic     (traders.py:131)
```

**Each layer adds exactly one cross-cutting concern**, and the ordering is forced, not aesthetic:
tracing must wrap the servers so server spans carry the trace id; the exit stack must wrap
`run_agent` so subprocesses die even when the run throws. Read top-down it is four one-line methods;
that is the whole point.

### 4.4 `LogTracer(TracingProcessor)` — the one inheritance

The only place we subclass a framework abstraction. It implements the SDK's tracing hooks
(`on_trace_start`, `on_span_start`, …) and writes every span into the `logs` table.

It has **no state at all**. What it needs — which trader a span belongs to — it recovers by parsing
the trace id, because the SDK hands it a span and nothing else, and four traders are interleaved on
one event loop. Hence:

```python
def make_trace_id(tag: str) -> str:
    tag += "0"                                # separator
    pad = 32 - len(tag)
    return f"trace_{tag}{''.join(secrets.choice(ALPHANUM) for _ in range(pad))}"
```

Attribution is smuggled through the one field the framework agreed to carry for us. The 32-character
total is the SDK's required format. This is a coupling no type checker can protect: **a trace id
built any other way yields `None` and its spans vanish from the log.**

### 4.5 The dashboard pair — and a name collision

`demo/ui.py` defines a second class also called `Trader`, unrelated to `traders.Trader`. They never
meet, but it is a trap when reading. The pair is a textbook Model–View split:

- `demo.ui.Trader` is a **view-model**: wraps an `Account`, exposes formatted things
  (`get_holdings_df()`, `get_portfolio_value_chart()`, `get_logs()`), refreshes via `reload()`. This
  is the _one_ place an `Account` is held across time rather than fetched-and-dropped — safe because
  the dashboard only reads.
- `TraderView` owns the Gradio widgets and the two timers (120s for portfolio data, 0.5s for logs)
  and knows nothing about accounts.

`api.py`, by contrast, has **no classes at all** — functions returning dicts. A stateless HTTP
handler has nothing to keep between calls, so a class would be pure ceremony.

### 4.6 What is deliberately _not_ a class

- **Persistence** — `database.py` is four module-level functions, not a `Repository`. There is no
  connection to hold: every function opens a fresh `sqlite3.connect(DB)` in a `with` block. That is
  precisely what makes it safe to call from the scheduler and from three different child processes.
- **Pricing** — `market.py` is functions plus module-level dicts as caches. A `PriceService` class
  would give each process its own instance, which is exactly wrong: the caches must be _per-process
  singletons_, and module state already is one.
- **Prompts** — `templates.py` is functions returning f-strings; functions rather than constants
  because they interpolate the current datetime at call time.

---

## 5. The pattern worth leading with: passing classes and functions, not objects and results

This recurs in five distinct forms, and the root reason is always the same: **the recipient must
decide _when_ or _how many times_ to produce the value, or it needs the value's _description_ rather
than the value.** Handing over an object or a result would take that decision away.

This is the section to reach for when an interviewer asks something open-ended about design.

### 5.1 The class as the receiver — `@classmethod`

```python
@classmethod
def get(cls, name: str):
    fields = read_account(name.lower())
    if not fields:
        fields = { ... $10,000 defaults ... }
        write_account(name, fields)
    return cls(**fields)
```

`cls` **is** the class `Account`, passed as the first argument. Not an instance — there isn't one
yet, and producing one is the entire job. Why not a module-level `get_account(name)`:

1. **There is no object to call it on.** An instance method is impossible; a `@staticmethod` would
   work but would have to name `Account(...)` literally.
2. **`cls(**fields)`follows subclasses.**`SubAccount.get("warren")`returns a`SubAccount`.
Hard-coding the class name would silently return the wrong type. *This is the mechanical reason
`@classmethod` exists as a language feature\* — good line to have ready.
3. **It keeps the lookup-or-create policy attached to the type.** "$10k if unknown" is a fact about
   what an `Account` is.

This is the **alternative-constructor pattern**, and it is why the codebase never writes
`Account(...)` directly — every account comes through `Account.get`.

### 5.2 A class as a schema — Pydantic models as annotations

```python
class PushModelArgs(BaseModel):
    message: str = Field(description="A brief message to push")

@mcp.tool()
def push(args: PushModelArgs):
    """Send a push notification with this brief message"""
```

`PushModelArgs` is handed to FastMCP **as a class, in an annotation**. FastMCP inspects it — fields,
types, `Field(description=...)` — and generates the JSON Schema shipped to the LLM in the tool list.

An instance could not do this job. An instance has `message="hello"`; it does not know that `message`
is a string, that it is required, or what it means. Only the class carries that. **The class is the
documentation, and the documentation is what the model reads.**

The same mechanism runs across all three of our servers, on both sides of the signature:

```python
async def get_holdings(name: str) -> dict[str, int]:
async def buy_shares(name: str, symbol: str, quantity: int, rationale: str) -> float:
```

Parameter types → input schema. Return annotation → output schema. Docstring → tool description.

**This is why the wrong `-> float` on `buy_shares` (which actually returns the report string) is not
cosmetic**: that annotation is transmitted to the model as a promise about what it will get back. In
an agent system, _type annotations and docstrings are prompt surface_. That reframing is the single
most interview-worthy insight in this codebase.

### 5.3 Functions in a list — the strategy pattern without a strategy class

```python
price_methods = [_last_trade, _snapshot, _previous_close]   # best first, uncalled
plan_tier = 0

for tier in range(plan_tier, len(price_methods)):
    try:
        price = price_methods[tier](client, symbol)
        plan_tier = tier          # remember what worked
        return price
    except Exception:
        continue
```

Storing the functions rather than their results is what makes fallback possible at all — you cannot
try-and-fall-back on values already computed. And because they are ordered, "which tier works on this
Massive plan" collapses to a single integer memoised in module state.

The problem is concrete: cheaper Massive plans reject `_last_trade` and `_snapshot` with
`NOT_AUTHORIZED`. Without `plan_tier`, every lookup would burn two doomed HTTP calls before the third
succeeded, against an endpoint that is itself rate-limited.

### 5.4 Callables handed to a framework — deferred evaluation

```python
self.portfolio_value = gr.HTML(self.trader.get_portfolio_value)     # note: no ()
self.chart           = gr.Plot(self.trader.get_portfolio_value_chart, ...)
self.holdings_table  = gr.Dataframe(value=self.trader.get_holdings_df, ...)
```

The missing parentheses pass the **bound method object**, not its return value — and that is the
entire behaviour of the dashboard. `gr.HTML(self.trader.get_portfolio_value())` would evaluate once,
at UI construction, and freeze that number into the page forever. Passing the method lets Gradio call
it per session and on every timer tick.

**The caller decides _what_ to compute; the framework decides _when_.**

`get_logs` shows a refinement: it takes the previously rendered HTML as input and returns
`gr.update()` — a no-op sentinel — when nothing changed, so a 0.5-second poll doesn't repaint the
panel 120 times a minute.

The decorator form is the same idea inverted: `@mcp.tool()` hands the _function_ to FastMCP for
registration, and FastMCP calls it later, when the model asks.

### 5.5 Configured-but-unstarted objects — lifecycle handed to the caller

```python
def trader_mcp_servers() -> list[MCPServerStdio]:
    return [MCPServerStdio(p, client_session_timeout_seconds=TIMEOUT) for p in params]
```

These _are_ objects, but inert ones — constructing an `MCPServerStdio` spawns nothing. The subprocess
starts only when the caller enters it as an async context:

```python
async with AsyncExitStack() as stack:
    trader_servers = [await stack.enter_async_context(s) for s in trader_mcp_servers()]
```

The factory returns _potential_ servers; the caller supplies the lifetime. That is what lets
`AsyncExitStack` guarantee all six subprocesses are torn down in reverse order **on any exit path,
including an exception mid-run**, and — importantly — that it unwinds exactly the ones already
started when a later one fails to launch. If the factory had started them, the factory would own
cleanup and there would be no single place to unwind them together.

`get_client` is the same reasoning on a different resource:

```python
@lru_cache(maxsize=None)
def get_client(base_url, api_key, provider) -> AsyncOpenAI:
    if not api_key:
        raise RuntimeError(f"No API key for {provider}; ...")
    return AsyncOpenAI(base_url=base_url, api_key=api_key)
```

Four provider clients could have been module-level constants. Deliberately not: `AsyncOpenAI` raises
when its key is missing, so eager construction would break the default free-router path for anyone
holding only an `OPENROUTER_API_KEY`. **Lazy construction plus caching instead of eager
construction** — `lru_cache` restores exactly the singleton behaviour you wanted from a constant.

### 5.6 The related case: an agent collapsed into a tool

```python
return researcher.as_tool(tool_name="Researcher", tool_description=research_tool())
```

`as_tool()` takes a fully-formed `Agent` — its own model, its own three MCP servers — and reduces it
to a callable with a name, a description, and a string-in/string-out contract. The trader's tool list
then contains `Researcher` sitting beside `buy_shares` **with no structural distinction**.

Two things this buys, and the first is the one to lead with:

- **Context isolation.** The researcher's ten noisy search results and fetched pages never enter the
  trader's conversation. The trader sees one summarised string. Compare this to a handoff, where the
  whole transcript would be shared.
- **Uniformity.** The trader's prompt can say "use the research tool" without explaining what a
  sub-agent is.

---

## 6. Model routing and graceful degradation

```python
if "/" in model_name:      → OpenRouter        # must be first
elif "deepseek" in ...     → DeepSeek
elif "grok" in ...         → xAI
elif "gemini" in ...       → Google
else:                      → SDK default (OpenAI)
```

Substring dispatch, with one ordering constraint that is worth mentioning because it is exactly the
kind of bug that ships: **the `/` branch must come first**, since a slug like
`deepseek/deepseek-chat` is an _OpenRouter_ model but would otherwise be caught by the DeepSeek
branch and sent to the wrong endpoint with the wrong key.

Every non-OpenAI provider is reached through `OpenAIChatCompletionsModel` over its OpenAI-compatible
endpoint, so four vendors are supported without four SDKs.

Two modes: `USE_MANY_MODELS=false` (default) puts all four traders on OpenRouter's free router — the
project costs nothing to run and needs one key. `USE_MANY_MODELS=true` gives each trader a different
frontier model and turns the project into a **model bake-off with money as the scoreboard**. The
honest caveat, and it is a good one to volunteer: on the free router the model behind a trader varies
between runs, so differences in results reflect strategy _plus_ whichever model answered — pin a slug
for a controlled comparison.

**Everything degrades rather than crashes:** no Massive key → simulated prices and market always
"open"; no Tavily key → search fails and the researcher falls back to `fetch`; no Pushover key → the
push posts and the failure is ignored.

### The pricing invariant — the subtlest bug in the project

`get_share_price` tries Massive and falls back to the simulator. The two disagree wildly (real AAPL
~313 vs simulated ~170), so **a symbol must never change source mid-process** — a position bought on
one source and valued on the other invents a profit or loss far larger than any trade. Two
module-level guards enforce it:

- a **60-second per-symbol cache**, which also keeps a single agent turn self-consistent and stops
  four traders exhausting the rate limit;
- **`_simulated_symbols`**, so a symbol Massive could never price stays simulated forever. And if
  Massive priced it once and later fails, the _last real price_ is served rather than a simulated one
  — **stale beats inconsistent**.

The health check: P&L immediately after trading should equal the spread cost
(`quantity * price * SPREAD`). Anything larger means the source flipped.

---

## 7. Failure design

**Failures are contained per trader**, at the outermost layer:

```python
async def run(self):
    try:
        await self.run_with_trace()
    except Exception as e:
        print(f"Error running trader {self.name}: {e}")
    self.do_trade = not self.do_trade
```

One bad run — a rate limit, a server that won't start, a turn-limit overrun — never kills the loop
and never cancels the other three traders. Note `Exception`, not `BaseException`: `KeyboardInterrupt`
and `CancelledError` still stop the scheduler, which is what you want. And the `do_trade` flip sits
_outside_ the `try`, so a crashed trader still advances to its next phase.

**Two distinct levels of error handling**, and knowing the difference is the point:

| level                                                             | who catches    | what happens                                                                                                                    |
| ----------------------------------------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| inside a tool (`ValueError("Insufficient funds")`)                | the Agents SDK | the exception text is returned to the model _as the tool result_ — the model reads it and self-corrects, e.g. buys fewer shares |
| the run itself (HTTP 429, `MaxTurnsExceeded`, server won't start) | `Trader.run`   | logged, run abandoned, other traders unaffected                                                                                 |

That first row is a genuinely interesting property of agent systems: **an exception becomes feedback
rather than a failure**, because the model is in the loop and can react to prose.

**Observability compensates for the swallowing.** A failed run looks identical to an idle one from
the outside, so the log table is the source of truth — `LogTracer` writes every span, and account
actions write their own rows from the child process. Both call `write_log`, which opens its own
SQLite connection and stamps `datetime('now')` server-side, so **two processes interleave correctly
in one timeline** — which is why `Bought 20 of AMD` appears nested inside the `function buy_shares`
span pair when you read the log back.

### The dependency trap, and why it is worth mentioning

`openai-agents` requires `mcp<2`, so `pyproject.toml` pins `mcp>=1.24.0,<2`. Raising the cap silently
resolves `openai-agents` back to **0.0.7** — which still installs, but lacks
`create_static_tool_filter`, `tool_filter` and `client_session_timeout_seconds`, so
`mcp_servers.py` fails to import. A resolver quietly downgrading a library by two years is a good war
story.

The same pin is pushed _down_ into the throwaway environments `uvx` builds (`MCP_PIN = "mcp<2"` with
`--with`), because those servers request `mcp` unpinned, resolve 2.x, and die on import — surfacing
to the agent only as `Connection closed`. **The debugging lesson: run the server by hand; the
subprocess's stderr holds the real traceback and the agent never sees it.**

---

## 8. Trade-offs, stated as trade-offs

Bring these up before you're asked. Every row is a decision with a real cost.

| decision                                            | buys                                                                                                | costs                                                                   |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| MCP process boundary between agent and domain model | uniform tool surface, crash isolation, language independence, no object graph for the model to walk | a subprocess spawn per account read; ~25 children per cycle             |
| `Account` as active record, fetched per operation   | never stale across processes                                                                        | full-blob rewrite on every save; last-write-wins                        |
| Whole account as one JSON column                    | no migrations, trivially model-shaped                                                               | cannot query holdings in SQL; the row grows without bound               |
| Exceptions swallowed per trader                     | one bad run never kills the loop                                                                    | failure looks identical to idleness; only the log knows                 |
| Trader name encoded in the trace id                 | four concurrent traders attributable from one flat table                                            | breaks silently if a trace id is built any other way                    |
| Servers rebuilt every run                           | no leaked processes, no stale connections                                                           | startup cost every cycle; 120s timeouts required                        |
| Researcher as a tool, not a handoff                 | trader's context stays clean                                                                        | the trader cannot see _how_ a conclusion was reached                    |
| Prompts isolated in `templates.py`                  | agent-facing text reviewable in one file                                                            | prompt and tool schema can drift apart                                  |
| Frontends read the DB directly                      | no API layer needed for a read-only view                                                            | P&L logic duplicated between `api.py` and `demo/ui.py`                  |
| Free-model router by default                        | costs nothing; one key to run                                                                       | the model varies per request, so results aren't a controlled comparison |

---

## 9. Likely questions, with the short answer

**"Why MCP instead of just calling functions?"** — Uniformity and isolation. Every capability,
whether ours or a third party's, is the same kind of thing; adding one never touches the
orchestrator. A crashing tool takes down a child process, not the scheduler. And the agent's reach is
limited to five explicitly exposed tools. The cost is a subprocess spawn per call, which is fine at
one cycle an hour.

**"Why is the researcher a tool and not a handoff?"** — Context isolation. A handoff shares the
conversation; `as_tool` returns one string. The trader never sees ten search results.

**"How do you attribute logs when four agents run concurrently?"** — The name is encoded in the trace
id by `make_trace_id`, and `LogTracer` parses it back out. There is no ambient context on a single
event loop, so attribution has to travel inside the one field the framework carries.

**"What stops an agent looping forever?"** — `max_turns=13`. Exceeding it raises `MaxTurnsExceeded`,
which is caught per trader; the run is abandoned and the next cycle starts clean. The Researcher
sub-agent has its own nested budget of 6, which `MAX_TURNS` does not cover.

**"Where's the state between runs?"** — Only two places: SQLite (balances, holdings, transactions,
logs) and each trader's private libsql knowledge graph in `memory/<name>.db`. The agent itself is
rebuilt from scratch every cycle and holds nothing. The graph belongs to the **researcher**, which
is the only agent holding the memory server — trading writes to SQLite, never to `memory/`, so the
two can disagree completely about how busy a trader has been.

**"How would you scale this?"** — The process boundary is already the seam. The MCP servers become
network services instead of stdio children, `Account` gets row-level locking or optimistic
concurrency instead of last-write-wins, and the JSON blob splits into real tables once you need to
query holdings. None of that touches `traders.py`.

**"What would you fix first?"** — The `-> float` annotation on `buy_shares` (§5.2), because it is a
lie told directly to the model. Then tests around `Account`, which is where the invariants live.

---

## 10. Known rough edges

Owning these is better than being caught by them.

- `Account.get_profit_loss()` calls `calculate_profit_loss()` without its required `portfolio_value`
  argument — it would raise `TypeError`, but nothing references it.
- `buy_shares` / `sell_shares` in `accounts_server.py` are annotated `-> float` but return a string —
  and that annotation is part of the schema the model sees.
- `push_server.push` ignores the Pushover HTTP response, so a bad token looks like success.
- `Account.get(name)` never raises; a typo mints a new $10k trader.
- `accounts.db` and `memory/` are untracked but not git-ignored.
- **There are no tests.** The smoke test is `uv run python -c "import backend.trading_floor"` (catches
  SDK/dependency breakage), then `uv run -m backend.reset` (exercises the DB layer), then reading one
  account back through `read_accounts_resource` — that last call touches the FastMCP server, the
  stdio subprocess, `Account.report()` and pricing in a single call.
