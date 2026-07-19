# Market Making — ETH Perpetual (Take-Home)

A GLFT (Guéant–Lehalle–Fernandez-Tapia) market-making strategy and an
event-driven backtester for an ETH perpetual, run over three calendar days of
L2 order-book, trade and funding data (2026-03-19 … 2026-03-21).

The writeups live in two docs: **[STRATEGY.md](STRATEGY.md)** (the model,
calibration, results), **[SIMULATOR.md](SIMULATOR.md)** (the matching engine and
its assumptions).

## Project structure

```
.
├── mm_backtest/            # the library
│   ├── data.py             # parquet -> int-tick numpy arrays (load_day / DayData)
│   ├── config.py           # SimConfig: fees, latency, funding, queue model
│   ├── orders.py           # Order / Fill / Side / OrderStatus types
│   ├── engine.py           # ExchangeSim: latency queue, matching, queue position
│   ├── account.py          # inventory, avg-cost realized PnL, fees, funding accrual
│   ├── strategy.py         # Strategy base + GridQuoter + NaiveJoinBest control
│   ├── glft.py             # GLFT strategy: rolling calibration, quoting, funding tilt
│   ├── backtest.py         # Backtester: k-way event merge, drives engine + strategy
│   └── metrics.py          # performance analysis + markdown report
├── run_backtest.py         # CLI entry point
├── tests/                  # pytest unit tests (engine + accounting scenarios)
├── data/                   # input parquet, one file per day:
│   ├── orderbook/          #   20-level bid/ask snapshots
│   ├── trades/             #   trades (price, size, aggressor side)
│   └── fundings/           #   funding-rate snapshots (~20 s)
├── out/  ·  out_glft_d3/   # generated report.md + parquet audit trails
├── STRATEGY.md · SIMULATOR.md · DATA.md · Description.md
└── pyproject.toml          # dependencies (managed with uv)
```

Data flows one way: `data.py` loads the parquet into integer-tick numpy arrays,
`backtest.py` merges the three streams in timestamp order and feeds each event to
`engine.py` (matches our orders) and the `strategy.py` plug-in, `account.py`
tracks PnL/inventory, and `metrics.py` turns the run into a report.

## Setup

Requires [**uv**](https://docs.astral.sh/uv/) and Python ≥ 3.11. Dependencies
(`polars`, `numpy`, and `pytest` for tests) are declared in `pyproject.toml` and
pinned in `uv.lock` — there is **no manual install step**: `uv run` provisions an
isolated environment on first use. To materialise it up front:

```bash
uv sync            # creates .venv with polars + numpy (+ pytest)
```

The three days of parquet are expected under `data/orderbook/`, `data/trades/`
and `data/fundings/`, named `YYYY-MM-DD.parquet` (already in place here).

## Baseline model — usage tutorial

**1. Run the default backtest** (baseline GLFT: γ = 0.20, single quote per side,
10 ms latency, Hyperliquid base fees, all 3 days):

```bash
uv run python run_backtest.py
```

It prints the full report and writes `out/report.md` plus `samples.parquet`,
`fills.parquet` and `orders.parquet` (a complete audit trail). The baseline
headline is **−4,864 USD** over the 3 days; see [STRATEGY.md](STRATEGY.md) §6 for
the analysis of why a symmetric passive quoter loses on this market.

**2. Common variations** (everything is a flag; `--help` lists them all):

```bash
uv run python run_backtest.py --strategy naive       # naive join-best control
uv run python run_backtest.py --gamma 0.05           # lower risk aversion (more volume)
uv run python run_backtest.py --grid-num 5           # enable grid (ladder) quoting
uv run python run_backtest.py --fee-tier mm          # maker-rebate fee tier
uv run python run_backtest.py --latency-ms 30 --out out_30ms
```

**3. Drive the library directly** (the same run, programmatically):

```python
from mm_backtest import Backtester, SimConfig
from mm_backtest.glft import GLFT
from mm_backtest.metrics import compute_metrics, to_markdown

cfg = SimConfig()                 # Hyperliquid base tier, 10 ms latency, hourly funding
strat = GLFT()                    # baseline model (gamma=0.20, grid off, funding tilt on)
result = Backtester(cfg, strat).run(["2026-03-19", "2026-03-20", "2026-03-21"])

metrics = compute_metrics(result)
print(to_markdown(metrics))
print(result.fills.head())        # per-fill audit trail as a polars DataFrame
```

Swap `GLFT()` for `NaiveJoinBest()` (from `mm_backtest.strategy`) to run the
control, or subclass `Strategy` to plug in your own quoting logic — the engine
and accounting are strategy-agnostic.

**4. Run the tests:**

```bash
uv run --group dev pytest         # engine matching + accounting unit tests
```
