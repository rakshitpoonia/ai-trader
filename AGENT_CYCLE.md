# The Cycle of Action of a Single Agent

How one trader — say **Warren** — goes from "the scheduler woke up" to "shares are in the account".

This is the runtime story, told top-down. For how the code is organised, see
[BACKEND_ARCHITECTURE.md](BACKEND_ARCHITECTURE.md).

---

## 1. The one-paragraph version

A scheduler wakes up on an interval. It hands each of the four traders a text prompt containing
their strategy and their current account. Each trader is an LLM agent holding a list of tools. The
agent replies either with **tool calls** or with **plain text**. If it returns tool calls, the
framework executes them, appends the results to the conversation, and asks the model again — that
round trip is called a **turn**. If it returns plain text, the run is over. Somewhere in the middle
of those turns the agent calls `buy_shares`, and that writes to the database. Then the scheduler
sleeps.

Everything below is an expansion of that paragraph.

---

## 2. The two agents

Warren is not one agent. He is an agent that owns a second agent as a tool.

```
Warren  (the trader agent)
│
├── tool: Researcher ──────────► Researcher  (a full agent, hidden inside a tool)
│                                 ├── tool: tavily_search      (web search)
│                                 ├── tool: fetch              (read a URL)
│                                 └── tools: create_entities, search_nodes, …
│                                                              (private knowledge graph)
│
├── tool: get_balance          ┐
├── tool: get_holdings         │
├── tool: buy_shares           ├─ the accounts MCP server
├── tool: sell_shares          │
├── tool: change_strategy      ┘
│
├── tool: lookup_share_price     ─ the market MCP server
│
└── tool: push                   ─ the notification MCP server
```

The Researcher is wrapped as a tool, not attached as a peer or a handoff:

```python
# backend/traders.py:101
async def get_researcher_tool(mcp_servers, model_name) -> Tool:
    researcher = await get_researcher(mcp_servers, model_name)
    return researcher.as_tool(tool_name="Researcher", tool_description=research_tool())
```

`as_tool()` takes a fully-built `Agent` and reduces it to a normal callable tool: a name, a
description, string in, string out. So from Warren's point of view, calling `Researcher` looks
exactly like calling `get_balance`. He passes a request like _"Find recent news on undervalued
consumer staples"_ and receives a paragraph back.

**Why this matters (good interview answer):** research is long and noisy — several web searches,
pages of raw results. If those ran in Warren's own conversation they would fill his context window
with search dumps. Wrapping the researcher as a tool means all of that happens in a _separate
conversation with its own context_, and Warren only ever sees the final summary. It is context
isolation, and it is the main reason to use a sub-agent instead of just giving the trader a search
tool.

---

## 3. The core concept: what a **turn** is

This is the single most important idea in the whole cycle.

An LLM cannot "do" anything. It can only produce text. So an agent framework wraps it in a loop:

> **A turn = one request to the model, plus the execution of whatever tool calls came back.**

The loop, in words:

```
turn:
  1. Collect the tool list from every attached MCP server
  2. Send to the model:  instructions + full conversation so far + tool schemas
  3. Read the reply:
       - reply contains tool calls  → execute them all, append results to the
                                      conversation, go back to step 1  (next turn)
       - reply is plain text        → STOP. That text is the final answer.
```

Four things to be able to say about turns:

**A turn is not a tool call.** If the model asks for three searches in one reply, all three run and
that is still _one_ turn. Turns count _round trips to the model_, not work done.

**The conversation grows every turn.** Turn 3's request contains the prompt, the model's turn-1
reply, the turn-1 tool results, the turn-2 reply, the turn-2 results, and so on. The model has no
memory of its own — the framework re-sends the whole transcript each time. This is why the account
JSON is stripped of its long value-history before it goes into the prompt
(`backend/traders.py:125`); everything in the prompt is paid for on every subsequent turn.

**Tools are re-listed every turn.** Step 1 runs each time, not once. The framework asks all three
of Warren's MCP servers "what tools do you have?" at the top of every turn. With 3 servers and
12 turns, that is 36 tool listings — they show up in the logs as `mcp_tools` entries, and they are
_listings_, not calls.

**Turns are capped.** The limit is the safety net that stops an agent looping forever:

```python
# backend/traders.py:43
MAX_TURNS = 13
```

```python
# backend/traders.py:193
await Runner.run(self.agent, message, max_turns=MAX_TURNS)
```

**Nested loops.** When Warren calls the `Researcher` tool, that tool _is itself_ a `Runner.run`
with its own turn loop, its own conversation, and its own budget of `RESEARCHER_MAX_TURNS = 6`,
passed to `as_tool(max_turns=...)`. The Researcher may take six turns internally; Warren spends
one. This is why `MAX_TURNS` alone never bounded the cost of a run.

### Where the loop actually lives

The turn loop is **not in this repository**. It is inside the Agents SDK, and our code enters it
with one line:

```python
# backend/traders.py:140
        await Runner.run(self.agent, message, max_turns=MAX_TURNS)
```

### Transitioning from one turn to the next

When the model's reply **does** contain tool calls, this happens, in order:

1. All tool calls in the reply are executed — in parallel if there are several.
2. Each result is wrapped as an item and **appended** to the running conversation.
3. The turn is classified `NextStepRunAgain`, and control returns to the top of the loop.
4. Tools are re-listed from every MCP server (a fresh round trip to each subprocess).
5. The turn counter increments and is checked against `max_turns`.
6. The whole conversation — original prompt plus every reply and result so far — is sent again.

**What does *not* change between turns:** the agent object, the six MCP subprocesses (still alive),
the trace, the system instructions, and every attribute on `Trader` including `do_trade`. The only
thing that grows is the conversation. A turn boundary is not a checkpoint or a reset — it is just
"ask the model again, with more context than last time".

### What happens when there are no tool calls

The model returns a message containing only text. The SDK checks whether anything is left to
execute — `if not processed_response.has_tools_or_approvals_to_run():`
(`agents/run_internal/turn_resolution.py:862`) — and since nothing is, it classifies the turn as
`NextStepFinalOutput` instead of `NextStepRunAgain`. That one branch is the entire termination
condition. Then, in strict order:

```
model returns plain text
  → turn is classified NextStepFinalOutput
  → agent span ends, task span ends
  → the while-loop returns a RunResult             ← the loop is now over
  → Runner.run returns to run_agent (traders.py:140); return value is discarded
  → AsyncExitStack unwinds — all 6 MCP subprocesses shut down
  → the trace closes  ("Ended: Warren-trading")
  → run_with_trace returns to run()
  → self.do_trade = not self.do_trade              ← only now
```

The text itself is not used for anything. It ends the loop, and that is its whole job.

## 4. Step 1 — The scheduler wakes up

You start one long-lived process:

```bash
uv run -m backend.trading_floor
```

It builds the four traders once, then loops forever:

```python
# backend/trading_floor.py:70
    traders = create_traders()
    while True:
        if RUN_EVEN_WHEN_MARKET_IS_CLOSED or is_market_open():
            await asyncio.gather(*[trader.run() for trader in traders])
        else:
            print("Market is closed, skipping run")
        await asyncio.sleep(RUN_EVERY_N_MINUTES * 60)
```

Points worth making:

- `asyncio.gather` runs **all four traders concurrently**, not one after another. They share one
  event loop; their LLM calls and subprocess I/O overlap. The scheduler waits for the slowest.
- The traders are built once and reused across cycles (`backend/trading_floor.py:51`). Each is a
  small object holding a name, a model name, and a phase flag.
- Market hours gate the run; `RUN_EVEN_WHEN_MARKET_IS_CLOSED=true` bypasses it for development.

From here on, follow just Warren. The other three are doing the same thing beside him.

---

## 5. Step 2 — Trade phase or rebalance phase?

Each trader carries one flag, set to `True` when the object is created:

```python
# backend/traders.py:106
class Trader:
    def __init__(self, name: str, lastname="Trader", model_name=FREE_MODEL_ROUTER):
        self.name = name
        self.lastname = lastname
        self.agent = None
        self.model_name = model_name
        self.do_trade = True
```

and flipped at the very end of every run:

```python
# backend/traders.py:159
    async def run(self):
        try:
            await self.run_with_trace()
        except Exception as e:
            print(f"Error running trader {self.name}: {e}")
        self.do_trade = not self.do_trade
```

**Exactly when does it flip?** On the last line of `run()` — *after* `run_with_trace()` has
returned, which means after the turn loop finished, after the MCP subprocesses were torn down, and
after the trace closed. It is the final statement of the entire run.

Three things this is *not*:

- **Not tied to turns.** The turn loop never sees `do_trade`. Warren can take 3 turns or 30; the
  flag flips once, afterwards, either way.
- **Not caused by "no more tool calls".** That condition ends the *loop*. The flip happens later,
  during the unwind (see §3), and happens on *every* exit path — including a turn-limit hit or a
  crash, since the assignment sits outside the `try`.
- **Not read during the run.** The model is never told which phase it is in. `do_trade` is used at
  exactly two places, both *before* the loop starts: choosing the prompt (`traders.py:135`) and
  naming the trace (`traders.py:154`). By the time the agent is running, its only effect is the
  prompt text already sent.

So the ordering is: turn ends → loop ends → run ends → **then** the flag flips, deciding what the
*next* cycle will be.

So Warren's life alternates:

| cycle | `do_trade` | phase         | what he is asked to do                      |
| ----- | ---------- | ------------- | ------------------------------------------- |
| 1     | `True`     | **trade**     | find _new_ opportunities and open positions |
| 2     | `False`    | **rebalance** | examine what he already holds and adjust    |
| 3     | `True`     | **trade**     | … and so on                                 |

The flag chooses which of two prompts gets sent:

```python
# backend/traders.py:135
        message = (
            trade_message(self.name, strategy, account)
            if self.do_trade
            else rebalance_message(self.name, strategy, account)
        )
```

**The difference between the two prompts** (both in `backend/templates.py`):

_Trade_ (`backend/templates.py:53`) says — look for new opportunities, research news consistent
with your strategy, execute trades. And explicitly: _"You do not need to rebalance your portfolio;
you will be asked to do so later."_

_Rebalance_ (`backend/templates.py:74`) says — examine your existing portfolio, research news
affecting **what you already hold**, and adjust. It also opens a door the trade prompt does not:

```python
# backend/templates.py:83
You also have a tool to change your strategy. Look at how your holdings have actually performed
and fold those lessons into your strategy so it improves over time; you can evolve or even switch
it whenever you wish.
```

So the agent is invited to rewrite its own strategy on rebalance cycles. That rewritten strategy is
saved to the database via `change_strategy` and becomes the strategy injected into every future
prompt. **This is the feedback loop that makes the system more than a stateless bot** — a good
thing to be able to point at in an interview.

The two phases also produce different trace names, which is how you tell them apart in the logs:

```python
# backend/traders.py:153
    async def run_with_trace(self):
        trace_name = f"{self.name}-trading" if self.do_trade else f"{self.name}-rebalancing"
```

---

## 6. Step 3 — Setting up the run: four nested layers

`Trader.run()` is a chain of four methods. Each adds exactly one concern, and the nesting order is
deliberate:

```
run()                        catches exceptions, flips do_trade
└── run_with_trace()         opens a trace so every step is attributable to Warren
    └── run_with_mcp_servers()   starts the tool servers, guarantees they are shut down
        └── run_agent()          builds the agent, assembles the prompt, runs the loop
```

### 6a. Tracing — tagging every log line with "Warren"

Four traders run concurrently on one event loop, and the framework's tracing hooks receive a span
that knows its trace _id_ but not which trader made it. So the name is encoded **into** the id:

```python
# backend/tracers.py:9
def make_trace_id(tag: str) -> str:
    """Return a string of the form 'trace_<tag><random>' ... total 32 chars after 'trace_'."""
    tag += "0"
    pad_len = 32 - len(tag)
    random_suffix = ''.join(secrets.choice(ALPHANUM) for _ in range(pad_len))
    return f"trace_{tag}{random_suffix}"
```

`make_trace_id("warren")` produces `trace_warren0<random padding>`. The log processor reads it back
out by splitting on `_` and then on `0`:

```python
# backend/tracers.py:22
    def get_name(self, trace_or_span: Trace | Span) -> str | None:
        trace_id = trace_or_span.trace_id
        name = trace_id.split("_")[1]
        if '0' in name:
            return name.split("0")[0]
```

That is why every log row in `accounts.db` has a `name` column filled in correctly even though
four agents were writing to it at once.

### 6b. Starting the tool servers

Every tool Warren has is an **MCP server running as a child process**, spoken to over stdin/stdout.
They are started fresh for this run and shut down at the end:

```python
# backend/traders.py:142
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
```

`trader_mcp_servers()` and `researcher_mcp_servers(name)` return **configured but not yet started**
server objects. Entering each as an async context is what actually spawns the subprocess and
performs the MCP handshake. `AsyncExitStack` collects all six exits so a single `async with` tears
all of them down, in reverse order, on every exit path.

**Why subprocesses, and why an exit stack?** Subprocesses because MCP's contract is a program on
stdin/stdout, not a Python import — that lets tools be written in any language (three of the six here
are Node or uvx packages, not our code), keeps a crashing or hanging tool out of the trader's process,
and gives each of the four traders its own isolated server instances. The exit stack because six
processes are opened one at a time and any one of them can fail mid-setup; `AsyncExitStack` unwinds
exactly the ones already started, on every exit path — success, exception, or turn-limit — so no run
can leave orphaned processes behind.

Six servers per run:

| owner      | server                    | gives the agent                                                               |
| ---------- | ------------------------- | ----------------------------------------------------------------------------- |
| Warren     | `backend.accounts_server` | `get_balance`, `get_holdings`, `buy_shares`, `sell_shares`, `change_strategy` |
| Warren     | `backend.push_server`     | `push`                                                                        |
| Warren     | market data               | `lookup_share_price` (or Massive's richer tool set)                           |
| Researcher | `mcp-server-fetch`        | `fetch`                                                                       |
| Researcher | `tavily-mcp`              | `tavily_search`                                                               |
| Researcher | `mcp-memory-libsql`       | knowledge-graph tools over `memory/Warren.db`                                 |

```python
# backend/mcp_servers.py:45
def trader_mcp_servers() -> list[MCPServerStdio]:
    """The trader's MCP servers: our Accounts server, Push Notification and Market data."""
    params = [
        {"command": "uv", "args": ["run", "-m", "backend.accounts_server"], "cwd": PROJECT_DIR},
        {"command": "uv", "args": ["run", "-m", "backend.push_server"], "cwd": PROJECT_DIR},
        market_params,
    ]
    return [MCPServerStdio(p, client_session_timeout_seconds=TIMEOUT) for p in params]
```

The Tavily server is filtered down to a single tool so the researcher reaches for plain search
rather than heavier crawl or deep-research tools:

```python
# backend/mcp_servers.py:69
    search = MCPServerStdio(
        {"command": "npx", "args": ["-y", "tavily-mcp@latest"], "env": tavily_env},
        client_session_timeout_seconds=TIMEOUT,
        tool_filter=create_static_tool_filter(
            allowed_tool_names=["tavily_search"]),
    )
```

And the memory server is **per trader** — Warren's knowledge graph is a different SQLite file from
Cathie's, so their accumulated research never mixes:

```python
# backend/mcp_servers.py:77
    memory = MCPServerStdio(
        {
            "command": "npx",
            "args": ["-y", "mcp-memory-libsql"],
            "env": {"LIBSQL_URL": f"file:./memory/{name}.db"},
            "cwd": PROJECT_DIR,
        },
        client_session_timeout_seconds=TIMEOUT,
    )
```

---

## 7. Step 4 — Building the agent and the prompt

```python
# backend/traders.py:131
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
```

Four things happen here, in order.

### 7a. Build the Researcher, then build Warren around it

```python
# backend/traders.py:114
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
```

Note the asymmetry: `tools=[tool]` holds exactly one Python-level tool — the Researcher. Everything
else Warren can do arrives through `mcp_servers=`. The framework merges both into one flat tool
list before sending it to the model, so the model cannot tell them apart.

`instructions=` is Warren's **system prompt** — his standing identity, sent on every turn:

```python
# backend/templates.py:36
def trader_instructions(name: str):
    return f"""
You are {name}, a trader on the stock market. Your account is under your name, {name}.
You actively manage your portfolio according to your strategy.
You have access to tools including a researcher to research online for news and opportunities...
Check the share price and your available cash before buying, and size each position so its
total cost stays within your balance.
...
After you've completed trading, send a push notification with a brief summary of activity,
then reply with a 2-3 sentence appraisal.
"""
```

That last line is important — it is what tells the model how to _finish_. Come back to it in §10.

### 7b. Read the account and the strategy

```python
# backend/traders.py:125
    async def get_account_report(self) -> str:
        account = await read_accounts_resource(self.name)
        account_json = json.loads(account)
        account_json.pop("portfolio_value_time_series", None)
        return json.dumps(account_json)
```

Both reads go over MCP as **resources**, not tools — `accounts://accounts_server/warren` and
`accounts://strategy/warren`, served by `backend/accounts_server.py:64`. Resources are MCP's
read-only, addressable data (the agent doesn't choose to call them; our code does), whereas tools
are actions the model chooses.

The long value history is stripped before it becomes prompt text, because it would be re-sent on
every turn.

### 7c. Assemble the prompt

The message Warren receives is the phase instruction + his strategy verbatim + his account JSON +
the current datetime:

```python
# backend/templates.py:53
def trade_message(name, strategy, account):
    return f"""Based on your investment strategy, you should now look for new opportunities.
Use the research tool to find news and opportunities consistent with your strategy.
...
Your investment strategy:
{strategy}
Here is your current account:
{account}
Here is the current datetime:
{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Now, carry out analysis, make your decision and execute trades. Your account name is {name}.
"""
```

Worth internalising: **there is no conversation history between cycles.** Warren starts each run
with a blank transcript. What carries forward is (a) the account state, injected into the prompt,
(b) the strategy, injected into the prompt, and (c) the knowledge graph in `memory/Warren.db`,
which he must actively query. The account _is_ the memory.

### 7d. Hand it to the runner

`Runner.run(agent, message, max_turns=13)` starts the loop from §3.

---

## 8. Step 5 — The loop, walked through

Below is an **illustrative** trade-phase run for Warren. The content is invented; the _structure_
— what nests inside what, what repeats each turn — is exactly what the framework produces.

Warren's strategy is value investing, so his prompt is the trade message.

---

**Before turn 1** — the framework asks all three of Warren's servers for their tools:
`accounts_server` returns 5, market returns 1, push returns 1. Seven tools, plus `Researcher`
from `tools=[...]`. Eight tool schemas go into the request.

---

### Turn 1 — delegate the research

Model receives: system instructions + trade message + 8 tool schemas.
Model replies with a tool call:

```
Researcher("Find recent news on undervalued large-cap consumer staples with
            strong free cash flow and durable competitive advantages")
```

The framework executes it — and because that tool _is an agent_, a whole nested run begins:

```
Warren, turn 1
└── function: Researcher                    ← Warren spends ONE turn here
    │
    └── nested Runner.run  (the Researcher's own loop)
        │
        ├── Researcher turn 1
        │     generation → replies with 3 tool calls, issued together:
        │       tavily_search("undervalued consumer staples 2026")
        │       tavily_search("consumer staples free cash flow leaders")
        │       search_nodes("consumer staples")        ← its own knowledge graph
        │     all three execute in parallel → 3 results appended
        │
        ├── Researcher turn 2
        │     generation → calls create_entities(...) to save what it learned
        │                  into memory/Warren.db for future runs
        │
        └── Researcher turn 3
              generation → plain text, no tool calls → nested loop STOPS
              "Three names stand out: ... trading at 14x earnings ..."
```

That final paragraph becomes the return value of the `Researcher` tool call. It is appended to
**Warren's** conversation as a single tool result. Warren never sees the three searches.

**When is the Researcher called?** Whenever the model decides to — but in practice it is called
early and often, because both prompts open with an explicit instruction to do so
(`backend/templates.py:54`: _"Use the research tool to find news and opportunities consistent with
your strategy"_). Typically the first turn, and again later when the model wants to dig into a
specific name.

---

### Turn 2 — check prices

Tools are re-listed. Conversation now = prompt + turn-1 tool call + researcher summary.

```
lookup_share_price("KO")
lookup_share_price("PG")
```

Two calls, one turn. Both hit the market MCP server, which returns numbers.

---

### Turn 3 — check the wallet

```
get_balance("Warren")   →  10000.0
```

The system instructions asked for this explicitly — _"Check the share price and your available cash
before buying, and size each position so its total cost stays within your balance"_
(`backend/templates.py:43`). Position sizing is an instruction, not code.

---

### Turn 4 — go deeper on one name

```
Researcher("Detail on KO: recent earnings, dividend history, any regulatory risk")
```

Another nested run, another one-paragraph result. This is the normal shape: research → narrow →
research again.

---

### Turn 5 — the trade

```
buy_shares(name="Warren", symbol="KO", quantity=60, rationale="Trading below intrinsic
           value on a 14x forward multiple with 60 years of dividend growth; fits the
           long-horizon value mandate.")
```

This is the moment the run stops being a conversation and starts being real. The call lands in the
accounts MCP server:

```python
# backend/accounts_server.py:27
@mcp.tool()
async def buy_shares(name: str, symbol: str, quantity: int, rationale: str) -> float:
    """Buy shares of a stock.

    Args:
        name: The name of the account holder
        symbol: The symbol of the stock
        quantity: The quantity of shares to buy
        rationale: The rationale for the purchase and fit with the account's strategy
    """
    return Account.get(name).buy_shares(symbol, quantity, rationale)
```

which runs the domain logic:

```python
# backend/accounts.py:79
    def buy_shares(self, symbol: str, quantity: int, rationale: str) -> str:
        """ Buy shares of a stock if sufficient funds are available. """
        price = get_share_price(symbol)
        buy_price = price * (1 + SPREAD)
        total_cost = buy_price * quantity

        if total_cost > self.balance:
            raise ValueError("Insufficient funds to buy shares.")
        ...
        self.holdings[symbol] = self.holdings.get(symbol, 0) + quantity
        ...
        self.balance -= total_cost
        self.save()
        write_log(self.name, "account", f"Bought {quantity} of {symbol}")
        return "Completed. Latest details:\n" + self.report()
```

Three things to notice:

- **The trade is committed the instant the tool runs.** `self.save()` writes to `accounts.db`.
  Nothing later in the conversation can undo it.
- **`rationale` is a required argument.** The model must justify every trade in prose, and that
  prose is stored on the transaction. The docstring and the argument names in
  `accounts_server.py` are what the model reads to decide how to fill this in — MCP tool
  docstrings are prompt surface.
- **The return value is the full updated account report**, so the model immediately sees its new
  balance and holdings without needing another call.

Note also `SPREAD = 0.002` (`backend/accounts.py:12`) — buys execute at `price * 1.002` and sells
at `price * 0.998`, so the simulation charges a realistic transaction cost on both sides.

---

### Turn 6 — notify

```
push({"message": "Warren: opened 60 KO at ~$62. Portfolio $10,020, 96% deployed."})
```

Handled by `backend/push_server.py:22`, which posts to Pushover.

---

### Turn 7 — finish

Model replies with **plain text and no tool calls**:

> "I've initiated a position in Coca-Cola at a valuation I consider comfortably below intrinsic
> value, consistent with my long-horizon mandate. The portfolio remains concentrated but the
> underlying business generates durable cash flows. I expect to hold through short-term
> volatility."

**No tool calls → the loop stops.** That is the entire termination condition.

---

## 9. The rebalance cycle — same loop, different intent

Next time the scheduler comes round, `do_trade` is `False` and Warren gets the rebalance message.
Structurally the run is identical — same agent, same tools, same turn loop. What changes is what
the model does with it:

### Turn 1 — look at what he already owns

```
get_holdings("Warren")   →  {"KO": 60}
lookup_share_price("KO")  →  64.10
```

### Turn 2 — research the position, not the market

```
Researcher("Any news on KO since last month? Earnings, guidance changes,
            sector rotation out of staples?")
```

The prompt steers this — _"Use the research tool to find news and opportunities affecting your
existing portfolio... You do not need to identify new investment opportunities at this time"_
(`backend/templates.py:76`).

### Turn 3 — trim or hold

```
sell_shares(name="Warren", symbol="KO", quantity=10,
            rationale="Trimming 17% of the position after a 3% run to restore
                       cash reserve; thesis unchanged.")
```

### Turn 4 — update the strategy (rebalance only)

```
change_strategy(name="Warren", strategy="<the original value-investing mandate,
                plus: 'Maintain at least 10% cash so I can add to positions on
                drawdowns rather than being fully deployed.'>")
```

```python
# backend/accounts_server.py:53
@mcp.tool()
async def change_strategy(name: str, strategy: str) -> str:
    """At your discretion, if you choose to, call this to change your investment strategy
    for the future.
    ...
    """
    return Account.get(name).change_strategy(strategy)
```

That new text is saved to the account and will be injected into **every future prompt** — the
strategy the agent reads next cycle is the one it wrote this cycle. This is the self-improvement
loop, and it only opens on rebalance cycles.

### Turn 5 — push, then plain text, done.

Then `do_trade` flips back to `True` and the next cycle is a trade cycle.

---

## 10. How a run ends

Three ways, and it is worth being able to name all three:

1. **Naturally** — the model returns a message with no tool calls. There is no "finish" tool and
   no terminal state. The system instructions ask for a 2–3 sentence appraisal after trading
   (`backend/templates.py:48`), so producing that prose _is_ the stop signal. The framework returns
   it to `run_agent`, which has no further use for it.
2. **Turn limit** — 30 turns without a plain-text reply raises `MaxTurnsExceeded`.
3. **An error** — a provider error, a tool failure, a dead subprocess.

Cases 2 and 3 land in the same place:

```python
# backend/traders.py:159
    async def run(self):
        try:
            await self.run_with_trace()
        except Exception as e:
            print(f"Error running trader {self.name}: {e}")
        self.do_trade = not self.do_trade
```

The exception is contained at the trader level, so one trader's bad run does not stop the other
three and does not stop the scheduler loop. Note the flip is outside the `try` — the phase advances
either way.

### Exactly what reaches that `except`

`run_with_trace()` awaits the whole chain — servers, prompt assembly, and the turn loop — so
anything raised anywhere below it lands here. In practice there are four groups:

**1. Setup failures, before the model is ever called.**

- A tool server fails to start in `run_with_mcp_servers` (`backend/traders.py:142`) — `uv`, `uvx`
  or `npx` not on PATH, or the MCP handshake exceeding `client_session_timeout_seconds=120`.
  This is the `Connection closed` family.
- Reading the account or strategy fails in `run_agent` (`backend/traders.py:133`), since both
  spawn their own `accounts_server` subprocess.
- No API key for the configured provider — `get_client` raises deliberately rather than
  returning a broken client:

```python
# backend/traders.py:48
@lru_cache(maxsize=None)
def get_client(base_url: str, api_key: str | None, provider: str) -> AsyncOpenAI:
    if not api_key:
        raise RuntimeError(
            f"No API key for {provider}; set it in .env or use the default "
            f"'{FREE_MODEL_ROUTER}' model (USE_MANY_MODELS=false)."
        )
```

**2. Model-call failures, from inside the loop.** Rate limits (HTTP 429), authentication errors,
connection resets — anything the provider client raises during a generation propagates straight
out of `Runner.run`.

**3. `MaxTurnsExceeded`** — 30 turns without a plain-text reply, raised at `agents/run.py:1126`.

**4. Teardown failures**, while `AsyncExitStack` is shutting the six subprocesses down.

### What does _not_ reach it — the important half

**A tool raising is not an error the agent run dies on.** The SDK catches it and feeds the message
back to the model as the tool's result, so the model can read it and try again. That is why
`Account.buy_shares` can afford to be strict:

```python
# backend/accounts.py:85
        if total_cost > self.balance:
            raise ValueError("Insufficient funds to buy shares.")
        elif price == 0:
            raise ValueError(f"Unrecognized symbol {symbol}")
```

Warren asking for 500 shares he cannot afford does not end his run — he receives
_"Insufficient funds to buy shares"_ as a tool result, and typically resizes and retries on the
next turn. **Validation errors in tools are a conversation with the model, not a crash.** This is
worth saying out loud in an interview: it is the mechanism that lets an agent self-correct.

**`KeyboardInterrupt` is not caught either.** The clause catches `Exception`, and
`KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` all inherit from `BaseException`
rather than `Exception`. So Ctrl+C stops the scheduler cleanly instead of being swallowed and
looping forever.

### When in the cycle it fires

At whatever point the exception happened — which may be before the first turn, or after Warren has
already bought. **Work already committed stays committed**, because `buy_shares` writes to
`accounts.db` the moment the tool runs (§8, turn 5); there is no transaction wrapping the run. So a
run that dies at turn 9 leaves behind the trades from turns 1–8, and the next cycle simply reads
that state back out of the database and carries on.

Then the `AsyncExitStack` unwinds: all six MCP subprocesses shut down. The trace closes. The
`Agent` objects and the conversation are discarded.

---

## 11. What survives the cycle

| what                              | where                                   | written when                                 |
| --------------------------------- | --------------------------------------- | -------------------------------------------- |
| balance, holdings, transactions   | `accounts.db` → `accounts` table        | every `buy_shares` / `sell_shares`           |
| portfolio value history           | same row, `portfolio_value_time_series` | every `Account.report()`                     |
| the strategy (possibly rewritten) | same row, `strategy`                    | `change_strategy`, rebalance cycles          |
| the audit log                     | `accounts.db` → `logs` table            | continuously, by the tracer and by `Account` |
| the researcher's knowledge graph  | `memory/Warren.db`                      | when the researcher calls `create_entities`  |

Everything else — the agent, the conversation, the six subprocesses — is gone. The next cycle is
rebuilt from the database.

---

## 12. Reading the run in the logs

Every step above writes rows into the `logs` table of `accounts.db`, so you can replay any run:

```sql
SELECT datetime, type, message FROM logs WHERE name = 'warren' ORDER BY id;
```

| `type`       | what it means                                               |
| ------------ | ----------------------------------------------------------- |
| `trace`      | the whole run — one Started/Ended pair per trader per cycle |
| `task`       | a `Runner.run` — appears again, nested, for the Researcher  |
| `agent`      | an agent taking control (`Warren`, then `Researcher`)       |
| `turn`       | one iteration of the loop                                   |
| `generation` | one call to the model                                       |
| `function`   | one tool call — including `Researcher` itself               |
| `mcp_tools`  | a tool _listing_ at the top of a turn, not a call           |
| `account`    | written directly by `Account` methods — `Bought 60 of KO`   |

Because the Researcher's spans inherit Warren's trace id, its `agent` / `turn` / `generation`
entries appear **between** `Started function Researcher` and `Ended function Researcher`. The
nesting from §8 is visible directly in the flat log.

---

## 13. Thirty-second recap

1. Scheduler wakes on an interval and runs all four traders concurrently.
2. Each trader alternates **trade → rebalance → trade**; a flag on the object picks the prompt.
3. Six MCP servers are started as subprocesses for this run and torn down after it.
4. The Researcher is built as an agent, then wrapped `as_tool()` so it looks like any other tool —
   giving it a separate context for noisy web research.
5. The prompt is assembled: phase instruction + strategy from the DB + account JSON + datetime.
   There is no conversation history between cycles.
6. `Runner.run` starts the **turn loop**: list tools → call the model → execute tool calls →
   repeat. A turn is one round trip to the model, not one tool call. Capped at 30.
7. A typical order is research → prices → balance → deeper research → **trade** → push.
8. `buy_shares` commits to SQLite the moment it runs.
9. The run ends when the model replies with plain text and no tool calls.
10. State persists in `accounts.db` and `memory/<Name>.db`; the flag flips; the scheduler sleeps.
