"""Event-loop driver: merges book/trade/funding streams in timestamp order,
feeds the exchange simulator and the strategy, records time series.

Tie-break at equal timestamps: trades first (the book snapshot at t reflects
post-trade state), then book, then funding.
"""
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from .account import Account
from .config import SimConfig
from .data import DayData, load_day
from .engine import ExchangeSim
from .orders import Fill, Order, Side
from .strategy import Strategy

_INF = np.iinfo(np.int64).max
_MAX_FUNDING_GAP_NS = int(120e9)  # skip accrual across data gaps (> 2 cadences)


@dataclass
class BacktestResult:
    cfg: SimConfig
    days: list[str]
    samples: pl.DataFrame           # 1 Hz state series
    fills: pl.DataFrame
    orders: pl.DataFrame            # one row per order (audit trail)
    mid_ts: np.ndarray              # full-resolution mid series (for markouts)
    mid_px: np.ndarray
    n_placed: int = 0
    n_canceled: int = 0
    n_rejected: int = 0
    final_equity: float = 0.0


class Ctx:
    """Strategy-facing facade over the running backtest."""

    def __init__(self, bt: "Backtester"):
        self._bt = bt

    @property
    def ts(self) -> int:
        return self._bt.now

    @property
    def book(self):
        return self._bt.engine.book

    @property
    def mid(self) -> float:
        return self._bt.engine.book.mid

    @property
    def position(self) -> float:
        """Position as known to the strategy: each fill is reflected one-way
        latency after it happens at the venue (confirmation delay)."""
        return self._bt.known_pos

    @property
    def equity(self) -> float:
        return self._bt.account.equity(self._bt.engine.book.mid)

    @property
    def open_orders(self) -> list[Order]:
        return list(self._bt.engine.open_orders.values())

    def order(self, order_id: int) -> Order | None:
        return self._bt.engine.orders.get(order_id)

    def place_limit(self, side: Side, price: float, qty: float,
                    post_only: bool = True) -> Order:
        return self._bt.engine.place_limit(self._bt.now, side, price, qty, post_only)

    def cancel(self, order_id: int) -> None:
        self._bt.engine.cancel(self._bt.now, order_id)

    def cancel_all(self) -> None:
        self._bt.engine.cancel_all(self._bt.now)


class Backtester:
    def __init__(self, cfg: SimConfig, strategy: Strategy, data_root: str = "data"):
        self.cfg = cfg
        self.strategy = strategy
        self.data_root = data_root
        self.account = Account(cfg)
        self.engine = ExchangeSim(cfg, self.account, on_fill=self._on_fill,
                                  on_reject=self._on_reject, on_cancel=self._on_cancel)
        self.ctx = Ctx(self)
        self.now = 0
        self._samples: dict[str, list] = {k: [] for k in
            ("ts", "mid", "pos", "cash", "equity", "realized", "fees",
             "funding", "unrealized", "has_bid", "has_ask")}
        self._mid_ts: list[np.ndarray] = []
        self._mid_px: list[np.ndarray] = []
        self._next_sample = None
        self._prev_funding: tuple[int, float] | None = None
        self._notif: deque = deque()
        self.known_pos = 0.0  # position as confirmed to the strategy

    # ---- engine callbacks (queued; delivered latency later) ----
    def _on_fill(self, fill: Fill, order: Order) -> None:
        self._notif.append((fill.ts + self.cfg.latency_ns, "fill", fill, order))

    def _on_reject(self, order: Order) -> None:
        self._notif.append((order.ts_active + self.cfg.latency_ns,
                            "reject", order, None))

    def _on_cancel(self, order: Order, effective_ts: int) -> None:
        self._notif.append((effective_ts + self.cfg.latency_ns,
                            "cancel", order, None))

    def _deliver(self, ts: int) -> None:
        """Hand due venue notifications to the strategy (one-way latency).

        `self.now` is advanced to each notification's receipt time before its
        callback runs, so orders the strategy places/cancels in response are
        stamped at receipt time — not the previous market event's timestamp."""
        while self._notif and self._notif[0][0] <= ts:
            due, kind, a, b = self._notif.popleft()
            self.now = max(self.now, due)
            if kind == "fill":
                self.known_pos += float(a.side) * a.qty
                self.strategy.on_fill(self.ctx, a, b)
            elif kind == "reject":
                self.strategy.on_reject(self.ctx, a)
            else:
                self.strategy.on_cancel(self.ctx, a)

    # ---- main loop ----
    def run(self, days: list[str]) -> BacktestResult:
        self.strategy.on_start(self.ctx)
        for day in days:
            d = load_day(self.data_root, day, self.cfg.tick)
            self._run_day(d)
            self.strategy.on_day_end(self.ctx, day)
            mids = (d.bid_pt[:, 0].astype(np.float64)
                    + d.ask_pt[:, 0]) * self.cfg.tick * 0.5
            self._mid_ts.append(d.ob_ts.copy())
            self._mid_px.append(mids)
            del d
        return self._finalize(days)

    def _run_day(self, d: DayData) -> None:
        eng, strat, acct, ctx = self.engine, self.strategy, self.account, self.ctx
        sample_ns = int(self.cfg.sample_interval_s * 1e9)
        ib = it = if_ = 0
        nb, nt, nf = len(d.ob_ts), len(d.tr_ts), len(d.fu_ts)
        while True:
            tb = d.ob_ts[ib] if ib < nb else _INF
            tt = d.tr_ts[it] if it < nt else _INF
            tf = d.fu_ts[if_] if if_ < nf else _INF
            if tb == _INF and tt == _INF and tf == _INF:
                break
            tmin = min(tb, tt, tf)
            self._deliver(tmin)
            # zero-order-hold sampling: grid points strictly before the next
            # event get the state that actually prevailed until then
            if self._next_sample is not None:
                while self._next_sample < tmin:
                    self._record_sample(self._next_sample)
                    self._next_sample += sample_ns
            if tt <= tb and tt <= tf:
                self.now = tt
                eng.on_trade(tt, int(d.tr_pt[it]), float(d.tr_size[it]), int(d.tr_dir[it]))
                strat.on_trade(ctx, tt, d.tr_pt[it] * self.cfg.tick,
                               float(d.tr_size[it]), int(d.tr_dir[it]))
                it += 1
            elif tb <= tf:
                self.now = tb
                eng.on_book(tb, d.bid_pt[ib], d.bid_q[ib], d.ask_pt[ib], d.ask_q[ib])
                strat.on_book(ctx)
                if self._next_sample is None:
                    self._record_sample(tb)
                    self._next_sample = tb + sample_ns
                ib += 1
            else:
                self.now = tf
                eng._advance(tf)
                rate = float(d.fu_rate[if_])
                # accrue the elapsed interval at the rate that PREVAILED
                # during it (the previous snapshot), not the one just revealed
                if self._prev_funding is not None and eng.book.ts >= 0:
                    prev_ts, prev_rate = self._prev_funding
                    dt = int(tf - prev_ts)
                    if dt <= _MAX_FUNDING_GAP_NS:
                        acct.apply_funding(prev_rate, dt, eng.book.mid)
                self._prev_funding = (tf, rate)
                strat.on_funding(ctx, tf, rate)
                if_ += 1
        # flush the sampling grid to the day's end (ZOH of the final state)
        if self._next_sample is not None and nb:
            last_ts = int(d.ob_ts[-1])
            while self._next_sample <= last_ts:
                self._record_sample(self._next_sample)
                self._next_sample += sample_ns

    def _record_sample(self, ts: int) -> None:
        mid = self.engine.book.mid
        a = self.account
        s = self._samples
        s["ts"].append(ts)
        s["mid"].append(mid)
        s["pos"].append(a.pos)
        s["cash"].append(a.cash)
        s["equity"].append(a.equity(mid))
        s["realized"].append(a.realized)
        s["fees"].append(a.fees)
        s["funding"].append(a.funding)
        s["unrealized"].append(a.unrealized(mid))
        has_bid = has_ask = False
        for o in self.engine.open_orders.values():
            if o.side is Side.BUY:
                has_bid = True
            else:
                has_ask = True
        s["has_bid"].append(has_bid)
        s["has_ask"].append(has_ask)

    def _finalize(self, days: list[str]) -> BacktestResult:
        eng = self.engine
        samples = pl.DataFrame(self._samples).with_columns(
            pl.from_epoch("ts", time_unit="ns").alias("datetime"))
        fills = pl.DataFrame({
            "order_id": [f.order_id for f in eng.fills],
            "ts": [f.ts for f in eng.fills],
            "side": [int(f.side) for f in eng.fills],
            "price": [f.price for f in eng.fills],
            "qty": [f.qty for f in eng.fills],
            "maker": [f.maker for f in eng.fills],
            "fee": [f.fee for f in eng.fills],
        }, schema={"order_id": pl.Int64, "ts": pl.Int64, "side": pl.Int8,
                   "price": pl.Float64, "qty": pl.Float64, "maker": pl.Boolean,
                   "fee": pl.Float64})
        orders = pl.DataFrame({
            "id": [o.id for o in eng.orders.values()],
            "side": [int(o.side) for o in eng.orders.values()],
            "price": [o.price(self.cfg.tick) for o in eng.orders.values()],
            "qty": [o.qty for o in eng.orders.values()],
            "filled": [o.filled for o in eng.orders.values()],
            "ts_created": [o.ts_created for o in eng.orders.values()],
            "status": [int(o.status) for o in eng.orders.values()],
        }, schema={"id": pl.Int64, "side": pl.Int8, "price": pl.Float64,
                   "qty": pl.Float64, "filled": pl.Float64,
                   "ts_created": pl.Int64, "status": pl.Int8})
        mid_ts = np.concatenate(self._mid_ts) if self._mid_ts else np.empty(0, np.int64)
        mid_px = np.concatenate(self._mid_px) if self._mid_px else np.empty(0)
        final_eq = self.account.equity(float(mid_px[-1])) if len(mid_px) else \
            self.account.cash
        return BacktestResult(cfg=self.cfg, days=days, samples=samples,
                              fills=fills, orders=orders,
                              mid_ts=mid_ts, mid_px=mid_px,
                              n_placed=eng.n_placed, n_canceled=eng.n_canceled,
                              n_rejected=eng.n_rejected, final_equity=final_eq)
