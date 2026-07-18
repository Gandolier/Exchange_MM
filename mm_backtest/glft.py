"""Guéant–Lehalle–Fernandez-Tapia market-making strategy.

Closed-form approximation of the optimal MM quotes (Guéant–Lehalle–
Fernandez-Tapia; "Dealing with the inventory risk", eq. 4.6/4.7), following
the hftbacktest tutorial implementation:

    c1 = 1/(xi*D) * ln(1 + xi*D/k)
    c2 = sqrt( gamma / (2*A*D*k) * (1 + xi*D/k)^(k/(xi*D) + 1) )
    half_spread = adj1 * (c1 + D/2 * sigma * c2)      [ticks]
    skew        = adj2 * sigma * c2                    [ticks per unit q]
    bid = fair - half_spread - skew*q,  ask = fair + half_spread - skew*q

with q = position / order_size, D = 1, xi = gamma.

Adjustment factors and grid trading follow the same tutorial. `adj1` scales
the whole half-spread (quote wider/tighter than the raw optimum); `adj2`
scales the inventory skew (turn inventory over faster/slower). Grid trading
rests `grid_num` orders per side, spaced `grid_interval` ticks apart and
snapped to that grid so quotes stop churning on every recalibration: the
innermost pair is the GLFT quote, deeper levels sit farther from mid and fill
only on larger excursions (better markout, lower fill rate). grid_num=1
reduces to a single skewed two-sided quote.

Calibration (rolling, every 5 s over a 10 min window of 100 ms buckets):
- sigma: std of 100 ms mid changes (ticks) * sqrt(10)  ->  ticks/sqrt(s)
- (A, k): trade-arrival intensity lambda(delta) = A*exp(-k*delta). Each
  bucket's max trade depth d (ticks from prevailing mid) increments all bins
  <= d; lambda(delta) = counts(delta)/window_s; log-linear fit over bins with
  arrivals (first 70 ticks).

Funding usage: the fair price is tilted by the current funding-rate snapshot,
fair -= funding_mult * rate * mid_ticks. A positive rate (longs pay) lowers
both quotes -> the book leans short, collecting funding; vice versa when
negative. funding_mult = 1 prices in one full settlement interval of funding.
"""
import math

import numpy as np

from .orders import Side
from .strategy import GridQuoter

BUCKET_NS = 100_000_000        # 100 ms
WINDOW_BUCKETS = 6000          # 10 min
RECALIB_BUCKETS = 50           # 5 s
MIN_BUCKETS = 3000             # 5 min warm-up before first quotes
MAX_DEPTH_TICKS = 70


def compute_coeff(xi: float, gamma: float, delta: float, A: float, k: float):
    inv_k = 1.0 / k
    c1 = 1.0 / (xi * delta) * math.log(1.0 + xi * delta * inv_k)
    c2 = math.sqrt(gamma / (2.0 * A * delta * k)
                   * (1.0 + xi * delta * inv_k) ** (k / (xi * delta) + 1.0))
    return c1, c2


class GLFT(GridQuoter):
    def __init__(self, size: float = 1.0, max_pos: float = 5.0,
                 gamma: float = 0.20, adj1: float = 1.0, adj2: float = 1.0,
                 funding_mult: float = 1.0, grid_num: int = 1,
                 grid_interval: float = 0.0):
        super().__init__()
        self.size = size
        self.max_pos = max_pos
        self.gamma = gamma
        self.adj1 = adj1
        self.adj2 = adj2
        self.funding_mult = funding_mult
        self.grid_num = max(1, int(grid_num))
        # grid spacing in ticks; 0 -> auto (round(half_spread), tutorial style)
        self.grid_interval = float(grid_interval)

        self._mids: list[float] = []      # 100 ms mid series, ticks (window)
        self._depths: list[float] = []    # per-bucket max trade depth, -1 = none
        self._cur_depth = -1.0
        self._bucket_end: int | None = None
        self._steps_since_calib = 0
        self._rate = 0.0                  # latest funding-rate snapshot

        self.half_spread: float | None = None   # ticks
        self.skew = 0.0                          # ticks per unit q
        self.A = self.k = self.sigma = float("nan")

    # ------------------------------------------------------------------
    # calibration machinery
    # ------------------------------------------------------------------
    def _roll(self, ts: int, book) -> None:
        """Close every 100 ms bucket elapsed up to ts (ZOH across gaps)."""
        if self._bucket_end is None:
            self._bucket_end = ts + BUCKET_NS
            return
        mid_t = (int(book.bid_pt[0]) + int(book.ask_pt[0])) / 2.0
        while ts >= self._bucket_end:
            self._mids.append(mid_t)
            self._depths.append(self._cur_depth)
            self._cur_depth = -1.0
            self._bucket_end += BUCKET_NS
            self._steps_since_calib += 1
            if len(self._mids) > WINDOW_BUCKETS:
                del self._mids[:-WINDOW_BUCKETS]
                del self._depths[:-WINDOW_BUCKETS]
            if (self._steps_since_calib >= RECALIB_BUCKETS
                    and len(self._mids) >= MIN_BUCKETS):
                self._steps_since_calib = 0
                self._calibrate()

    def _calibrate(self) -> None:
        mids = np.asarray(self._mids)
        sigma = float(np.diff(mids).std()) * math.sqrt(10.0)  # ticks/sqrt(s)
        depths = np.asarray(self._depths)
        traded = depths >= 0.0
        if traded.sum() < 30 or sigma <= 0.0:
            return
        bins = np.clip(depths[traded].astype(np.int64), 0, MAX_DEPTH_TICKS - 1)
        hist = np.bincount(bins, minlength=MAX_DEPTH_TICKS)
        counts = hist[::-1].cumsum()[::-1]          # arrivals reaching >= delta
        lam = counts / (len(depths) * 0.1)          # per second
        mask = lam > 0.0
        x = np.arange(MAX_DEPTH_TICKS, dtype=np.float64)[mask]
        y = np.log(lam[mask])
        if len(x) < 3:
            return
        slope, intercept = np.polyfit(x, y, 1)
        k = -float(slope)
        A = float(np.exp(intercept))
        if k <= 1e-8 or not math.isfinite(A) or A <= 0.0:
            return
        c1, c2 = compute_coeff(self.gamma, self.gamma, 1.0, A, k)
        self.half_spread = self.adj1 * (c1 + 0.5 * sigma * c2)
        self.skew = self.adj2 * sigma * c2
        self.A, self.k, self.sigma = A, k, sigma

    # ------------------------------------------------------------------
    # event handlers
    # ------------------------------------------------------------------
    def on_trade(self, ctx, ts: int, price: float, size: float, direction: int) -> None:
        book = ctx.book
        if book.ts < 0:
            return
        self._roll(ts, book)
        mid_t = (int(book.bid_pt[0]) + int(book.ask_pt[0])) / 2.0
        depth = abs(price / book.tick - mid_t)
        if depth > self._cur_depth:
            self._cur_depth = depth

    def on_funding(self, ctx, ts: int, rate: float) -> None:
        self._rate = rate

    def on_book(self, ctx) -> None:
        book = ctx.book
        self._roll(book.ts, book)
        if self.half_spread is None:            # still warming up
            return
        best_bid = int(book.bid_pt[0])
        best_ask = int(book.ask_pt[0])
        mid_t = (best_bid + best_ask) / 2.0
        fair = mid_t - self.funding_mult * self._rate * mid_t
        pos = ctx.position
        q = pos / self.size
        # GLFT skew shifts both depths against inventory; adj factors are baked
        # into half_spread/skew at calibration time (see _calibrate).
        bid_depth = self.half_spread + self.skew * q
        ask_depth = self.half_spread - self.skew * q
        # grid spacing (ticks); snap the innermost quote to it so the ladder
        # only moves when the snapped level changes -> far less order churn
        gi = self.grid_interval if self.grid_interval > 0.0 else max(1.0, round(self.half_spread))
        gi = int(max(1, round(gi)))
        bid0 = int(math.floor((fair - bid_depth) / gi) * gi)
        ask0 = int(math.ceil((fair + ask_depth) / gi) * gi)
        # spread is pinned at 1 tick: never rest inside/through the touch
        bid0 = min(bid0, best_bid)
        ask0 = max(ask0, best_ask)
        # size the ladder to the REMAINING inventory budget on each side, so a
        # trend can't cascade-fill several deep levels through the cap: rest
        # only as many levels as fit before |pos| would hit max_pos.
        n_bid = max(0, min(self.grid_num,
                           int(math.floor((self.max_pos - pos) / self.size + 1e-9))))
        n_ask = max(0, min(self.grid_num,
                           int(math.floor((self.max_pos + pos) / self.size + 1e-9))))
        bids = [(bid0 - i * gi, self.size) for i in range(n_bid)]
        asks = [(ask0 + i * gi, self.size) for i in range(n_ask)]
        self.apply_grid(ctx, {Side.BUY: bids, Side.SELL: asks})
