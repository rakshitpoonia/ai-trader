# Trade-offs: Making Four Agents Fit a Free Tier

Four LLM traders run continuously against a free-tier model key and a Tavily plan of 1,000
searches a month. The constraint is fixed and the trader count is not negotiable — the system
exists to trade extensively — so the only thing left to change was how much each run costs.

## The problem

The system worked, but it was expensive in a way that wasn't visible until I measured it.

Every span the Agents SDK emits is already written to a `logs` table for the dashboard, and one
of those span types — `generation` — is emitted exactly once per model request. That made the
dashboard's log table double as a request meter, without adding any instrumentation. Reading
eight completed runs back out of it turned "this feels expensive" into three specific causes:

**1. Research was unbounded, and it dominated.** The researcher is a sub-agent exposed to the
trader through `agent.as_tool()`. That is a nested agent loop: it takes its own turns, and each
one is its own model request. Two things followed from that, neither obvious from reading the
code:

- `MAX_TURNS`, which I had set on the trader, doesn't reach it. The researcher ran at the SDK's
  own default.
- Nothing limited how many *times* a trader could call it. The prompt said "use the research
  tool"; it never said "once".

The result was a long tail. Most runs called the researcher once and cost about six requests. A
couple called it three or four times and cost over twenty. Research alone was around 40% of
every model request the system made.

**2. The turn cap was decorative.** `MAX_TURNS` was 30. Almost every run finished in under five
trader turns. A limit that high doesn't bound anything; it just fails to.

**3. Two phases were paying for work they didn't need.** The rebalance run asked for a fresh
research cycle over positions the trading run had already researched when it opened them. And
the "send a push notification, then reply" instruction forced a whole extra turn at the end of
every run — one run sent four notifications.

## The solution

**Cap the loop you actually have, not the one you think you have.** `RESEARCHER_MAX_TURNS = 4`
is passed explicitly to `as_tool()`. `MAX_TURNS` came down from 30 to 10 — comfortably above
what a well-behaved run uses, low enough that a pathological one is cut off early.

**Enforce in code what the prompt can only request.** The prompt already asked for one research
phase and got four. So `get_researcher_tool` wraps the tool's invoke function to set a flag and
points `is_enabled` at that flag: after the first call, the SDK stops offering the tool at all.
The model isn't shown an error — the tool is simply gone, and it proceeds with what it learned.
The flag is a closure, and a fresh tool is built per run, so "once" scopes to a run naturally
with nothing to reset between cycles.

**Give the agent its budget instead of just enforcing it.** A cap the agent doesn't know about
produces truncated work. The researcher's instructions now state that it has four turns, tell
it to issue its searches in parallel within a single turn rather than one at a time, and require
that it end on a written summary. The trader is told this is its only research phase, so it asks
for everything up front rather than holding back for a follow-up that will never be available.

**Give each phase one job.** Trading researches; rebalancing doesn't. Rebalancing acts on
holdings that were already researched, market data tools still show what moved, and the next
trading run is only hours away.

**Stop paying a turn for a notification.** One push per run, and none at all when no trades were
made.

## What this costs

Nothing here is free, and two of the trade-offs are real:

- **Research is one shot.** If the first research phase misses something, the trader decides
  without it. This is why the trader is told up front that it gets one call — the mitigation is
  in the prompt, not in a retry.
- **A failed research call isn't retried.** The gate counts invocations, not successes, so a
  transient failure costs that run its research. Deliberate: the alternative is a retry loop,
  which is the exact behaviour being removed.
- **Four researcher turns is a genuine ceiling** and a couple of recorded runs wanted five. Turn
  overrun is non-fatal — the SDK converts it into a tool-result string — but the research is
  lost, which is why the budget is stated in the instructions and why it isn't set lower.
- **Rebalance runs are blind to breaking news**, and no-trade runs are silent.

The unifying judgement is that these all trade a *rare* capability for a *predictable* cost.
Under a hard daily limit, a run that reliably completes beats a run that occasionally does more
and then exhausts the budget for everything after it.

## The result

Per trader, per phase — rounded, from the measured runs:

| | requests | turns | research calls | Tavily searches |
| --- | ---: | ---: | ---: | ---: |
| **Before** — trading | ~10 | ~6 trader + ~4 researcher | 1–4 | ~3 |
| **Before** — rebalance | ~10 | same shape | 1–4 | ~3 |
| **After** — trading | ~7 | ~4 trader + ~3 researcher | 1 | ~2 |
| **After** — rebalance | ~3 | ~3 trader | 0 | 0 |

A full trade + rebalance cycle, all four traders:

| | before | after |
| --- | ---: | ---: |
| model requests per cycle | ~80 | ~40 |
| worst case per cycle | ~200 | ~96, hard-capped |
| Tavily searches per cycle | ~25 | ~9 |
| Tavily searches per month | ~520 | ~190 of 1,000 |

Roughly half the request cost, a worst case that is now bounded rather than open-ended, and
about 80% of the monthly search budget left unspent — with four traders still running
concurrently and each still getting a real research phase.

The measurement is the part worth keeping. The cap was never the interesting decision; knowing
which loop was actually spending the budget was, and that came from noticing the telemetry
already in the system could answer the question.

---

## Follow-up, 2026-08-15: the caps were too tight, and the sink had moved

Two runs after this was written showed the caps cutting into real work rather than the long tail.

**`RESEARCHER_MAX_TURNS` 4 → 6.** Three of four traders overran 4 on consecutive runs, and an
overrun loses the research entirely — the run pays for the Tavily searches and gets nothing back,
the worst possible outcome per request. The researcher had been fetching pages one per turn, so
`researcher_instructions` now demands the same parallel batching for fetches that it already
demanded for searches, and the turn budget is passed in rather than written into the prompt text
so the two cannot drift.

**`MAX_TURNS` 10 → 13.** The remaining sink was not research at all. Massive's MCP server exposes
a generic `search_endpoints` / `call_api` / `query_data` trio over ~147 REST endpoints instead of
a `get_price` tool, so a trader spent eight turns discovering how to price a stock and hit the cap
without trading. The turn bump alone would only have bought a longer search, so the actual fix is
in the prompt: the `note` in `templates.py` now names the two endpoints this plan allows —
`/v2/aggs/ticker/SYMBOL/prev` for one symbol, grouped daily aggregates with `store_as` +
`query_data` for many — and states that `/v2/last/trade/...` and `/v2/snapshot/...` return
`NOT_AUTHORIZED`, so those are never tried. Endpoint discovery is not reasoning, and paying model
requests for it was the whole loss.

**The disabled-tool trick was withdrawn.** Making the spent researcher vanish via `is_enabled`
assumed research succeeds and the trader moves on. When research instead failed on max turns, one
trader retried, found no such tool, and the "Tool not found" error ended the turn and the whole
run. The tool now stays visible and returns `research_spent()` — a refusal costs one turn; the
vanishing act cost an entire trading run.

Worst case per cycle rises from ~96 to 4 × (13 + 6) = ~76 model requests against a ~50/day free
tier, so a single pathological cycle can still exhaust the day. The bet is that naming the
endpoints removes the pathology rather than merely postponing it — which the next runs' logs,
now legible thanks to the `MCP_TOOLS` lines, will settle.
