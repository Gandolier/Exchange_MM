"""Parquet loaders -> numpy arrays ready for the event loop.

Prices are converted to integer ticks once, vectorized, so the hot loop never
does float price comparisons.
"""
from dataclasses import dataclass

import numpy as np
import polars as pl

N_LEVELS = 20


@dataclass
class DayData:
    day: str
    # orderbook
    ob_ts: np.ndarray      # int64 ns epoch, (N,)
    bid_pt: np.ndarray     # int32 ticks, (N, 20), level 1 first
    bid_q: np.ndarray      # float32 ETH, (N, 20)
    ask_pt: np.ndarray
    ask_q: np.ndarray
    # trades
    tr_ts: np.ndarray      # int64
    tr_pt: np.ndarray      # int32 ticks
    tr_size: np.ndarray    # float64 ETH
    tr_dir: np.ndarray     # int8: +1 aggressor bought, -1 aggressor sold
    # fundings
    fu_ts: np.ndarray      # int64
    fu_rate: np.ndarray    # float64 (decimal fraction per settlement interval)


def _ts_ns(df: pl.DataFrame) -> np.ndarray:
    return df.select(pl.col("datetime").cast(pl.Int64)).to_numpy().ravel().astype(np.int64)


def load_day(root: str, day: str, tick: float) -> DayData:
    ob = pl.read_parquet(f"{root}/orderbook/{day}.parquet")
    ob_ts = _ts_ns(ob)
    cols = {}
    for side in ("bid", "ask"):
        px = ob.select([f"{side}_price_{i}" for i in range(1, N_LEVELS + 1)]).to_numpy()
        cols[f"{side}_pt"] = np.rint(px / tick).astype(np.int32)
        del px
        q = ob.select([f"{side}_qty_{i}" for i in range(1, N_LEVELS + 1)]).to_numpy()
        cols[f"{side}_q"] = q.astype(np.float32)
        del q
    del ob

    tr = pl.read_parquet(f"{root}/trades/{day}.parquet")
    tr_ts = _ts_ns(tr)
    tr_pt = np.rint(tr["price"].to_numpy() / tick).astype(np.int32)
    tr_size = tr["size"].to_numpy().astype(np.float64)
    tr_dir = np.where(tr["is_maker_ask"].to_numpy() == 1, 1, -1).astype(np.int8)

    fu = pl.read_parquet(f"{root}/fundings/{day}.parquet")
    fu_ts = _ts_ns(fu)
    fu_rate = fu["funding_rate"].to_numpy().astype(np.float64)

    return DayData(day=day, ob_ts=ob_ts,
                   bid_pt=cols["bid_pt"], bid_q=cols["bid_q"],
                   ask_pt=cols["ask_pt"], ask_q=cols["ask_q"],
                   tr_ts=tr_ts, tr_pt=tr_pt, tr_size=tr_size, tr_dir=tr_dir,
                   fu_ts=fu_ts, fu_rate=fu_rate)
