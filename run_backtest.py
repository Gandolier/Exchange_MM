"""Run the backtest. Example:

    uv run python run_backtest.py                     # naive placeholder, 3 days
    uv run python run_backtest.py --queue-model pessimistic --latency-ms 50
"""
import argparse
import pathlib
import time

from mm_backtest import Backtester, SimConfig
from mm_backtest.glft import GLFT
from mm_backtest.metrics import compute_metrics, to_markdown
from mm_backtest.strategy import NaiveJoinBest

ALL_DAYS = ["2026-03-19", "2026-03-20", "2026-03-21"]

# Hyperliquid perp fee presets (bps): maker, taker.
#   base = tier 0 (default); t2 = >$25M/14d; t4 = >$500M (0 maker);
#   mm   = top maker-rebate tier (-0.3 bp maker) + top taker.
FEE_TIERS = {"base": (1.5, 4.5), "t2": (0.8, 3.5), "t4": (0.0, 2.8),
             "mm": (-0.3, 2.4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", default=ALL_DAYS)
    ap.add_argument("--strategy", default="glft", choices=["glft", "naive"])
    ap.add_argument("--size", type=float, default=1.0, help="quote size, ETH")
    ap.add_argument("--max-pos", type=float, default=5.0, help="inventory cap, ETH")
    ap.add_argument("--latency-ms", type=float, default=10.0)
    ap.add_argument("--queue-model", default="proportional",
                    choices=["proportional", "pessimistic"])
    ap.add_argument("--fee-tier", default=None, choices=list(FEE_TIERS),
                    help="Hyperliquid fee preset -> maker/taker (overrides fee args)")
    ap.add_argument("--maker-fee-bps", type=float, default=1.5)
    ap.add_argument("--taker-fee-bps", type=float, default=4.5)
    # GLFT parameters
    ap.add_argument("--gamma", type=float, default=0.20, help="risk aversion")
    ap.add_argument("--adj1", type=float, default=1.0, help="half-spread mult")
    ap.add_argument("--adj2", type=float, default=1.0, help="skew mult")
    ap.add_argument("--funding-mult", type=float, default=1.0,
                    help="fair-price tilt: intervals of funding priced in")
    ap.add_argument("--grid-num", type=int, default=1,
                    help="orders per side (1 = single quote; >1 = grid, see STRATEGY.md)")
    ap.add_argument("--grid-interval", type=float, default=0.0,
                    help="grid spacing in ticks; 0 = auto (round(half_spread))")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    maker, taker = args.maker_fee_bps, args.taker_fee_bps
    if args.fee_tier is not None:
        maker, taker = FEE_TIERS[args.fee_tier]
    cfg = SimConfig(latency_ms=args.latency_ms, queue_model=args.queue_model,
                    maker_fee_bps=maker, taker_fee_bps=taker)
    if args.strategy == "glft":
        strategy = GLFT(size=args.size, max_pos=args.max_pos, gamma=args.gamma,
                        adj1=args.adj1, adj2=args.adj2,
                        funding_mult=args.funding_mult, grid_num=args.grid_num,
                        grid_interval=args.grid_interval)
    else:
        strategy = NaiveJoinBest(size=args.size, max_pos=args.max_pos)

    t0 = time.time()
    bt = Backtester(cfg, strategy)
    result = bt.run(args.days)
    print(f"replay done in {time.time() - t0:.1f}s: "
          f"{result.n_placed:,} orders, {len(result.fills):,} fills")
    if isinstance(strategy, GLFT) and strategy.half_spread is not None:
        print(f"last calibration: A={strategy.A:.3f}/s k={strategy.k:.3f}/tick "
              f"sigma={strategy.sigma:.2f} ticks/sqrt(s) "
              f"half_spread={strategy.half_spread:.2f} ticks "
              f"skew={strategy.skew:.2f} ticks/q")

    metrics = compute_metrics(result)
    report = to_markdown(metrics, title=f"Backtest report — {args.strategy}")
    print()
    print(report)

    out = pathlib.Path(args.out)
    out.mkdir(exist_ok=True)
    (out / "report.md").write_text(report)
    result.samples.write_parquet(out / "samples.parquet")
    result.fills.write_parquet(out / "fills.parquet")
    result.orders.write_parquet(out / "orders.parquet")
    print(f"written: {out}/report.md, samples.parquet, fills.parquet, orders.parquet")


if __name__ == "__main__":
    main()
