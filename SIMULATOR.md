# Backtesting Simulator — design and assumptions

Event-driven replay of the historical L2 book, trade tape and funding stream,
with an exchange-style matching model for our own orders. The strategy is a
plug-in ([mm_backtest/strategy.py](mm_backtest/strategy.py)); everything else
is strategy-agnostic.

```
data/*.parquet ──► data.py (int-tick numpy arrays)
                      │
backtest.py ──► k-way merge of book / trades / funding, ts order
   │                (ties: trades → book → funding)
   ├─► engine.py    ExchangeSim: latency queue, matching, queue position
   ├─► account.py   inventory, avg-cost realized PnL, fees, funding accrual
   ├─► strategy.py  Strategy callbacks (on_book / on_trade / on_fill / …)
   └─► metrics.py   performance analysis + markdown report
```

Run: `uv run python run_backtest.py` (GLFT strategy, see
[STRATEGY.md](STRATEGY.md); `--strategy naive` for the join-best control;
~30 s for 3 days). Tests: `uv run --group dev pytest` — synthetic
matching/accounting scenarios.

## Matching model

The data pins the spread at 1 tick with ~32 ETH displayed at L1 against
~0.2–0.5 ETH median trades (see [DATA.md](DATA.md)), so *whether* a quote
fills is decided almost entirely by queue position. The engine models it
explicitly:

1. **Placement.** Orders and cancels take effect `latency_ms` (default 10 ms)
   after the decision; fills can race an in-flight cancel, as on a real venue.
   A new order joins **behind** all displayed quantity at its price (beyond
   L20, behind the worst visible level's qty). Post-only orders that would
   cross on arrival are rejected; non-post-only marketable orders execute as
   taker against the current snapshot and the remainder rests with queue 0.
   Fill, reject **and cancel-confirmation** notifications — and therefore both
   the position and the set of live orders the strategy believes it has —
   arrive one-way latency after the venue event. The quoter tracks its own
   book purely from these delayed acks, never from live engine state, so it
   cannot react to a fill before the confirmation could physically reach it.
2. **Queue position.** Trades at our price drain the queue from the front;
   volume overflowing our queue position fills us (partially if needed).
   Between snapshots, displayed-qty drops in excess of traded volume are
   cancellations: `proportional` mode (default) removes them pro-rata from
   ahead/behind us, `pessimistic` mode assumes they all came from behind.
   In both modes `queue_ahead` is clamped to displayed qty — anyone ahead of
   us must be visible in the book.
3. **Flow conservation.** One historical print of size *s* fills at most *s*
   across all our orders — the aggressor's volume is redirected to us, never
   duplicated. A print strictly beyond our price (trade-through) fills us at
   *our* price, capped by the print size, chained across our orders in
   price/arrival priority.
4. **Book-cross without a print.** Opposite best strictly THROUGH our price
   (gap moves): our remainder fills at our price — any order displayed
   through our level must have matched our invisible order first. Opposite
   best exactly AT our price (locked book): we fill only up to the displayed
   opposite quantity, with a per-order counter ensuring repeated locked
   snapshots never re-count the same demand.
5. **Timestamp ties.** Trade rows sharing a timestamp are one sweep and are
   processed sequentially before the book snapshot bearing the same ts
   (snapshots reflect post-trade state).

## Accounting

- Average-cost realized PnL; flips through zero re-anchor the entry price.
- Fees per fill: maker 1.5 bps / taker 4.5 bps — **Hyperliquid base tier**
  ([schedule](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)).
  Fees fall with 14-day volume; `--fee-tier {base,t2,t4,mm}` selects presets,
  down to the maker-rebate tier **mm = −0.3 bps maker** (the venue pays you to
  quote). A rebate — unlike a fee — can flip a marginal passive quoter, so the
  tier is the single biggest PnL lever here.
- Funding: the feed is a ~20 s **rate snapshot**, not a settlement event.
  We accrue continuously between snapshots at Hyperliquid's **hourly settlement
  interval**: `pnl -= pos × mid × rate × Δt/1h`. Positive rate ⇒ longs pay.
- Invariant (unit-tested): `equity(mark) − initial_cash ≡ realized − fees +
  funding + pos × (mark − avg_px)`.

## Deliberate assumptions and their bias

| Assumption | Direction of bias |
|---|---|
| No market impact: our fills never mutate the historical book/tape | Optimistic on fill volume — a quoter camped at best on both sides can "capture" volume comparable to the whole tape. Mitigated by keeping quoted size ≪ L1 depth and not resting at best permanently. |
| Trade-through fills capped by print size | Flow-conserving; still pessimistic in selection (those fills are adverse by construction). |
| Locked-book fills bounded by displayed opposite qty | Bounded by observable demand; still generous for large quotes when L1 is deep. |
| Queue join at the back, cancels pro-rata | Middle-of-road; `pessimistic` mode available for a lower bound. |
| Funding settles hourly (Hyperliquid), accrued at the previous snapshot's rate | Small over 3 days (±30 USD). The directional tilt in the strategy prices the next settlement payment, so its sign/usage is robust to this choice. |
| Fees = Hyperliquid base tier (1.5/4.5 bps) | Conservative: a live MM trades a lower tier, down to a −0.3 bps maker rebate. Tier is the single biggest PnL lever — see `--fee-tier`. |
| Latency constant 10 ms both ways (orders, cancels, notifications) | Colocated maker; no queueing/jitter; sensitivity checkable via `--latency-ms`. Note: with ~50–70 ms throttled snapshots, an adverse move and the fill it causes arrive in the same event, so sub-snapshot latency advantages are invisible by construction (10 ms vs 30 ms moves PnL < 3 %). |

Sanity evidence the model behaves like a real venue: a naive join-best 1 ETH
quoter (no alpha) loses ≈ 3.0 bps per ETH traded — 60 s markout ≈ −1.5 bps
(pure adverse selection) plus the 1.5 bp base maker fee, against ~0 captured
spread (it camps the touch). Passive flow at best is *supposed* to lose money
without alpha; the engine reproduces that. GLFT (see [STRATEGY.md](STRATEGY.md))
loses 7.7× less by quoting deep and trading 8× less, not by better per-fill
selection — the honest limit of a symmetric quoter on a 1-tick market.

## Performance analysis ([mm_backtest/metrics.py](mm_backtest/metrics.py))

- **PnL**: total and per-day, decomposed into realized (gross), unrealized,
  fees, funding; return on capital.
- **Inventory**: mean/std, max long/short, mean abs, max abs notional,
  % time long/short/flat, end position (1 Hz time-weighted samples).
- **Fills**: orders placed/canceled/rejected, fill rate, fill events, volume
  (ETH/USD, buy/sell), maker share, avg fill size, net PnL per ETH traded,
  two-sided quote uptime.
- **Risk**: max drawdown (USD, % of capital), annualized Sharpe and Sortino on
  1-minute PnL, per-minute PnL std, worst/best minute.
- **Markouts**: signed mid move vs fill price at 0/1/5/30/60 s horizons
  (equal- and qty-weighted) — the direct adverse-selection measure.

Artifacts per run (`out/`): `report.md`, `samples.parquet` (1 Hz state),
`fills.parquet`, `orders.parquet` (full audit trail).
