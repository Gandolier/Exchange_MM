"""Performance analysis per the assignment: total/daily PnL, inventory stats,
fill stats, risk metrics — plus markouts (adverse selection), PnL attribution
(spread capture vs fees vs funding), turnover and quote uptime.
"""
import numpy as np
import polars as pl

from .backtest import BacktestResult

MINUTES_PER_YEAR = 365 * 1440
MARKOUT_HORIZONS_S = (1, 5, 30, 60)


def compute_metrics(r: BacktestResult) -> dict:
    m: dict = {"days": r.days}
    s = r.samples
    init = r.cfg.initial_cash

    # ---------- PnL ----------
    eq = s["equity"].to_numpy()
    last = s.tail(1)
    total = float(last["equity"][0]) - init
    m["pnl"] = {
        "total_net": total,
        "realized_gross": float(last["realized"][0]),
        "unrealized_end": float(last["unrealized"][0]),
        "fees": float(last["fees"][0]),
        "funding": float(last["funding"][0]),
        "return_on_capital_pct": 100.0 * total / init,
    }

    daily = (s.group_by(s["datetime"].dt.date().alias("date"))
             .agg(pl.col("equity").last().alias("eq_close"),
                  pl.col("realized").last(), pl.col("fees").last(),
                  pl.col("funding").last(), pl.col("unrealized").last())
             .sort("date"))
    eq_close = daily["eq_close"].to_numpy()
    eq_open = np.concatenate([[init], eq_close[:-1]])
    m["daily"] = {
        "date": [str(d) for d in daily["date"]],
        "pnl": (eq_close - eq_open).tolist(),
        "realized_gross": np.diff(np.concatenate([[0.0], daily["realized"].to_numpy()])).tolist(),
        "fees": np.diff(np.concatenate([[0.0], daily["fees"].to_numpy()])).tolist(),
        "funding": np.diff(np.concatenate([[0.0], daily["funding"].to_numpy()])).tolist(),
        "d_unrealized": np.diff(np.concatenate([[0.0], daily["unrealized"].to_numpy()])).tolist(),
    }

    # ---------- Inventory (1 Hz samples => time-weighted) ----------
    pos = s["pos"].to_numpy()
    mid = s["mid"].to_numpy()
    m["inventory"] = {
        "mean": float(pos.mean()),
        "std": float(pos.std()),
        "max_long": float(pos.max()),
        "max_short": float(pos.min()),
        "mean_abs": float(np.abs(pos).mean()),
        "max_abs_notional": float(np.max(np.abs(pos) * mid)),
        "pct_time_flat": 100.0 * float((pos == 0).mean()),
        "pct_time_long": 100.0 * float((pos > 0).mean()),
        "pct_time_short": 100.0 * float((pos < 0).mean()),
        "end_pos": float(pos[-1]),
    }

    # ---------- Fills ----------
    f = r.fills
    n_fills = len(f)
    orders = r.orders
    n_touched = int((orders["filled"] > 0).sum()) if len(orders) else 0
    volume = float(f["qty"].sum()) if n_fills else 0.0
    notional = float((f["qty"] * f["price"]).sum()) if n_fills else 0.0
    m["fills"] = {
        "n_fills": n_fills,
        "orders_placed": r.n_placed,
        "orders_canceled": r.n_canceled,
        "orders_rejected": r.n_rejected,
        "orders_filled_any": n_touched,
        "fill_rate_pct": 100.0 * n_touched / r.n_placed if r.n_placed else 0.0,
        "volume_eth": volume,
        "notional_usd": notional,
        "maker_share_pct": (100.0 * float(f.filter(pl.col("maker"))["qty"].sum()) / volume
                            if volume else 0.0),
        "avg_fill_eth": volume / n_fills if n_fills else 0.0,
        "buy_volume_eth": float(f.filter(pl.col("side") == 1)["qty"].sum()) if n_fills else 0.0,
        "sell_volume_eth": float(f.filter(pl.col("side") == -1)["qty"].sum()) if n_fills else 0.0,
        "pnl_per_eth_traded": (m["pnl"]["total_net"] / volume) if volume else 0.0,
        "quote_uptime_two_sided_pct": 100.0 * float((s["has_bid"] & s["has_ask"]).mean()),
    }

    # ---------- Risk ----------
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    eq_1m = (s.group_by_dynamic("datetime", every="1m").agg(pl.col("equity").last())
             ["equity"].to_numpy())
    r1m = np.diff(eq_1m)
    sharpe = float(r1m.mean() / r1m.std() * np.sqrt(MINUTES_PER_YEAR)) if len(r1m) > 1 and r1m.std() > 0 else 0.0
    # target-relative downside deviation over ALL returns (target = 0)
    downside_dev = float(np.sqrt(np.mean(np.minimum(r1m, 0.0) ** 2))) if len(r1m) > 1 else 0.0
    sortino = float(r1m.mean() / downside_dev * np.sqrt(MINUTES_PER_YEAR)) if downside_dev > 0 else 0.0
    m["risk"] = {
        "max_drawdown_usd": float(dd.min()),
        "max_drawdown_pct_of_capital": 100.0 * float(dd.min()) / init,
        "sharpe_1m_annualized": sharpe,
        "sortino_1m_annualized": sortino,
        "pnl_std_1m": float(r1m.std()) if len(r1m) > 1 else 0.0,
        "worst_1m_pnl": float(r1m.min()) if len(r1m) else 0.0,
        "best_1m_pnl": float(r1m.max()) if len(r1m) else 0.0,
    }

    # ---------- Markouts (adverse selection) ----------
    m["markouts_bps"] = _markouts(r)
    return m


def _markouts(r: BacktestResult) -> dict:
    """Signed mid move after our fills: side * (mid(t+h) - fill_px), in bps.
    Negative = adverse selection (price moves against the inventory we just
    acquired). Mids are as-of (last snapshot at/before t+h), so h=0 uses the
    post-event book on timestamp ties — the prevailing mark right after the
    fill, not the pre-trade touch."""
    f = r.fills
    if len(f) == 0 or len(r.mid_ts) == 0:
        return {}
    ts = f["ts"].to_numpy()
    px = f["price"].to_numpy()
    side = f["side"].to_numpy().astype(np.float64)
    qty = f["qty"].to_numpy()
    out = {}
    for h in (0, *MARKOUT_HORIZONS_S):
        target = ts + int(h * 1e9)
        idx = np.searchsorted(r.mid_ts, target, side="right") - 1
        idx = np.clip(idx, 0, len(r.mid_px) - 1)
        mo = side * (r.mid_px[idx] - px) / px * 1e4
        w = qty / qty.sum()
        out[f"{h}s"] = {"mean": float(mo.mean()),
                        "qty_weighted": float((mo * w).sum())}
    return out


# ----------------------------------------------------------------------
# Markdown report
# ----------------------------------------------------------------------
def _usd(x: float) -> str:
    return f"{x:+,.2f}"


def to_markdown(m: dict, title: str = "Backtest report") -> str:
    p, inv, fl, rk = m["pnl"], m["inventory"], m["fills"], m["risk"]
    L = [f"# {title}", "",
         f"Period: {m['days'][0]} … {m['days'][-1]}", "",
         "## PnL",
         "| metric | USD |", "|---|---|",
         f"| **Total net PnL** | **{_usd(p['total_net'])}** |",
         f"| Realized (gross, trading) | {_usd(p['realized_gross'])} |",
         f"| Unrealized at end | {_usd(p['unrealized_end'])} |",
         f"| Fees paid | {_usd(-p['fees'])} |",
         f"| Funding PnL | {_usd(p['funding'])} |",
         f"| Return on capital | {p['return_on_capital_pct']:+.3f}% |",
         "",
         "## Daily PnL",
         "| date | net PnL | realized gross | fees | funding | Δunrealized |",
         "|---|---|---|---|---|---|"]
    d = m["daily"]
    for i, date in enumerate(d["date"]):
        L.append(f"| {date} | {_usd(d['pnl'][i])} | {_usd(d['realized_gross'][i])} | "
                 f"{_usd(-d['fees'][i])} | {_usd(d['funding'][i])} | "
                 f"{_usd(d['d_unrealized'][i])} |")
    L += ["",
          "## Inventory",
          "| metric | value |", "|---|---|",
          f"| Mean / std (ETH) | {inv['mean']:+.3f} / {inv['std']:.3f} |",
          f"| Max long / max short (ETH) | {inv['max_long']:+.3f} / {inv['max_short']:+.3f} |",
          f"| Mean abs (ETH) | {inv['mean_abs']:.3f} |",
          f"| Max abs notional (USD) | {inv['max_abs_notional']:,.0f} |",
          f"| % time long / short / flat | {inv['pct_time_long']:.1f} / {inv['pct_time_short']:.1f} / {inv['pct_time_flat']:.1f} |",
          f"| End position (ETH) | {inv['end_pos']:+.3f} |",
          "",
          "## Fills",
          "| metric | value |", "|---|---|",
          f"| Orders placed / canceled / rejected | {fl['orders_placed']:,} / {fl['orders_canceled']:,} / {fl['orders_rejected']:,} |",
          f"| Orders with ≥1 fill (fill rate) | {fl['orders_filled_any']:,} ({fl['fill_rate_pct']:.2f}%) |",
          f"| Fill events | {fl['n_fills']:,} |",
          f"| Volume | {fl['volume_eth']:,.2f} ETH / {fl['notional_usd']:,.0f} USD |",
          f"| Buy / sell volume (ETH) | {fl['buy_volume_eth']:,.2f} / {fl['sell_volume_eth']:,.2f} |",
          f"| Maker share | {fl['maker_share_pct']:.1f}% |",
          f"| Avg fill size (ETH) | {fl['avg_fill_eth']:.3f} |",
          f"| Net PnL per ETH traded | {fl['pnl_per_eth_traded']:+.3f} USD |",
          f"| Two-sided quote uptime (any price, 1 Hz sampled) | {fl['quote_uptime_two_sided_pct']:.1f}% |",
          "",
          "## Risk",
          "| metric | value |", "|---|---|",
          f"| Max drawdown | {_usd(rk['max_drawdown_usd'])} ({rk['max_drawdown_pct_of_capital']:.2f}% of capital) |",
          f"| Sharpe (1-min, annualized) | {rk['sharpe_1m_annualized']:.2f} |",
          f"| Sortino (1-min, annualized) | {rk['sortino_1m_annualized']:.2f} |",
          f"| PnL std per minute | {rk['pnl_std_1m']:.2f} USD |",
          f"| Worst / best minute | {_usd(rk['worst_1m_pnl'])} / {_usd(rk['best_1m_pnl'])} |",
          ""]
    if m.get("markouts_bps"):
        L += ["## Markouts after our fills (bps, signed: negative = adverse)",
              "| horizon | mean | qty-weighted |", "|---|---|---|"]
        for h, v in m["markouts_bps"].items():
            L.append(f"| {h} | {v['mean']:+.3f} | {v['qty_weighted']:+.3f} |")
        L.append("")
    return "\n".join(L)
