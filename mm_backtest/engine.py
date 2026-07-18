"""Exchange simulator: replays the historical book/tape and matches our
resting limit orders against it, exchange-style.

Model (assumptions documented in SIMULATOR.md):
- No market impact: our fills never mutate the historical book or tape.
- Queue position: a new order joins BEHIND all displayed qty at its price.
  Trades at our price drain the queue from the front; the overflow past our
  queue position fills us (partially if needed). Cancellations are inferred
  from snapshot-to-snapshot qty drops net of trades and handled per
  SimConfig.queue_model. queue_ahead is always clamped to displayed qty.
- Flow conservation: one historical print of size s fills at most s across
  all our orders. A print strictly beyond our price (trade-through) fills us
  at OUR limit price, capped by the print size, with price/arrival priority
  chaining across our own orders.
- Book-cross without a print: opposite best strictly THROUGH our price fills
  our remainder; opposite best exactly AT our price (locked book) fills only
  up to the displayed opposite qty, tracked so repeated locked snapshots
  never re-count the same demand.
- Latency: placements and cancels take effect latency_ns after the decision;
  fills can race an in-flight cancel, exactly as on a real venue.
- Marketable non-post-only orders execute as taker against the current
  snapshot (walking levels up to the limit price); the remainder rests.
"""
import heapq

from .account import Account
from .config import SimConfig
from .orders import Fill, Order, OrderStatus, Side

EPS = 1e-9


class BookView:
    """Current historical L2 snapshot (never includes our own orders)."""

    __slots__ = ("ts", "bid_pt", "bid_q", "ask_pt", "ask_q", "tick")

    def __init__(self, tick: float):
        self.tick = tick
        self.ts = -1
        self.bid_pt = self.bid_q = self.ask_pt = self.ask_q = None

    def set(self, ts, bid_pt, bid_q, ask_pt, ask_q):
        self.ts = ts
        self.bid_pt, self.bid_q = bid_pt, bid_q
        self.ask_pt, self.ask_q = ask_pt, ask_q

    @property
    def best_bid(self) -> float:
        return self.bid_pt[0] * self.tick

    @property
    def best_ask(self) -> float:
        return self.ask_pt[0] * self.tick

    @property
    def mid(self) -> float:
        return (int(self.bid_pt[0]) + int(self.ask_pt[0])) * self.tick * 0.5

    @property
    def spread_ticks(self) -> int:
        return int(self.ask_pt[0]) - int(self.bid_pt[0])

    def disp_at(self, side: Side, price_ticks: int) -> float | None:
        """Displayed qty at an exact price level.

        Returns None when the price is beyond the visible 20 levels (state
        there is unobservable); 0.0 for a visible-range price with no resting
        qty (an empty level inside or ahead of the book)."""
        pts, qs = (self.bid_pt, self.bid_q) if side is Side.BUY else (self.ask_pt, self.ask_q)
        if side is Side.BUY:
            if price_ticks < int(pts[-1]):
                return None
        elif price_ticks > int(pts[-1]):
            return None
        return float(qs[pts == price_ticks].sum())

    def deep_queue_estimate(self, side: Side) -> float:
        """Queue guess for a price beyond L20: the worst visible level's qty
        (better than assuming front-of-queue at a level that surely held size)."""
        qs = self.bid_q if side is Side.BUY else self.ask_q
        return float(qs[-1])


class ExchangeSim:
    def __init__(self, cfg: SimConfig, account: Account, on_fill=None,
                 on_reject=None, on_cancel=None):
        self.cfg = cfg
        self.account = account
        self.on_fill = on_fill
        self.on_reject = on_reject
        self.on_cancel = on_cancel   # (order, effective_ts) when a cancel lands
        self.book = BookView(cfg.tick)
        self._pending = []   # heap of (activation_ts, seq, kind, order)
        self._seq = 0
        self.orders: dict[int, Order] = {}
        self.open_orders: dict[int, Order] = {}
        self.fills: list[Fill] = []
        self.n_placed = 0
        self.n_canceled = 0
        self.n_rejected = 0

    # ------------------------------------------------------------------
    # Strategy-facing API (ts = decision time)
    # ------------------------------------------------------------------
    def place_limit(self, ts: int, side: Side, price: float, qty: float,
                    post_only: bool = True) -> Order:
        pt = int(round(price / self.cfg.tick))
        self._seq += 1
        order = Order(id=self._seq, side=side, price_ticks=pt, qty=qty,
                      ts_created=ts, ts_active=ts + self.cfg.latency_ns,
                      post_only=post_only)
        self.orders[order.id] = order
        heapq.heappush(self._pending, (order.ts_active, self._seq, "place", order))
        self.n_placed += 1
        return order

    def cancel(self, ts: int, order_id: int) -> None:
        o = self.orders.get(order_id)
        if o is None or o.status in (OrderStatus.FILLED, OrderStatus.CANCELED,
                                     OrderStatus.REJECTED):
            return
        self._seq += 1
        heapq.heappush(self._pending, (ts + self.cfg.latency_ns, self._seq, "cancel", o))

    def cancel_all(self, ts: int) -> None:
        for o in self.orders.values():
            if o.status in (OrderStatus.PENDING, OrderStatus.OPEN):
                self.cancel(ts, o.id)

    # ------------------------------------------------------------------
    # Market events (called by the backtester in timestamp order)
    # ------------------------------------------------------------------
    def on_book(self, ts, bid_pt, bid_q, ask_pt, ask_q) -> None:
        self._advance(ts)
        self.book.set(ts, bid_pt, bid_q, ask_pt, ask_q)
        if not self.open_orders:
            return
        best_ask = int(ask_pt[0])
        best_bid = int(bid_pt[0])
        for order in list(self.open_orders.values()):
            if order.side is Side.BUY:
                through = best_ask < order.price_ticks
                locked = best_ask == order.price_ticks
                opp_disp = float(ask_q[0]) if locked else 0.0
            else:
                through = best_bid > order.price_ticks
                locked = best_bid == order.price_ticks
                opp_disp = float(bid_q[0]) if locked else 0.0
            if through:
                # opposite best strictly beyond our price with no print: any
                # order displayed through our level would have matched us first
                self._fill(order, ts, order.price_ticks * self.cfg.tick,
                           order.remaining, maker=True)
                continue
            if locked:
                # opposite best sitting AT our price: that displayed demand
                # arrived at our level and would have traded with our
                # (invisible) order — fill bounded by what is displayed,
                # never re-counting qty already consumed by earlier lock-fills
                avail = opp_disp - order.lock_consumed
                if avail > EPS:
                    take = min(order.remaining, avail)
                    order.lock_consumed += take
                    self._fill(order, ts, order.price_ticks * self.cfg.tick,
                               take, maker=True)
                if order.id in self.open_orders:
                    order.queue_ahead = 0.0
                    order.last_disp_qty = 0.0
                    order.traded_since_book = 0.0
                continue
            order.lock_consumed = 0.0
            disp = self.book.disp_at(order.side, order.price_ticks)
            if disp is None:   # beyond visible book: unobservable, keep state
                order.traded_since_book = 0.0
                continue
            expected = order.last_disp_qty - order.traded_since_book
            if expected < 0.0:
                expected = 0.0
            if (self.cfg.queue_model == "proportional"
                    and expected > EPS and disp < expected - EPS):
                frac = min(1.0, order.queue_ahead / expected)
                order.queue_ahead = max(0.0, order.queue_ahead - (expected - disp) * frac)
            if order.queue_ahead > disp:  # people ahead of us must be displayed
                order.queue_ahead = disp
            order.last_disp_qty = disp
            order.traded_since_book = 0.0

    def on_trade(self, ts: int, price_ticks: int, size: float, direction: int) -> None:
        """direction: +1 aggressor bought (hits asks), -1 aggressor sold.

        Flow conservation: one print of `size` can fill at most `size` across
        all our orders — the aggressor's volume is redirected to us, never
        duplicated. Our orders priced through the print have price priority
        and consume the pool first; orders exactly at the print price receive
        only the pool volume overflowing the displayed queue ahead of them,
        chained in arrival order.
        """
        self._advance(ts)
        if not self.open_orders:
            return
        hit_side = Side.SELL if direction > 0 else Side.BUY
        through, at_level = [], []
        for order in self.open_orders.values():
            if order.side is not hit_side:
                continue
            opt = order.price_ticks
            if (opt < price_ticks) if direction > 0 else (opt > price_ticks):
                through.append(order)
            elif opt == price_ticks:
                at_level.append(order)
        if not through and not at_level:
            return
        pool = size
        # price priority (better price first), then arrival priority
        through.sort(key=lambda o: (o.price_ticks if hit_side is Side.SELL
                                    else -o.price_ticks, o.id))
        for order in through:
            order.queue_ahead = 0.0  # the print passed our level: queue gone
            take = min(order.remaining, pool)
            if take > EPS:
                self._fill(order, ts, order.price_ticks * self.cfg.tick,
                           take, maker=True)
                pool -= take
        if not at_level:
            return
        at_level.sort(key=lambda o: o.id)
        overflow = max(0.0, pool - at_level[0].queue_ahead)
        for order in at_level:
            order.traded_since_book += pool
            order.queue_ahead = max(0.0, order.queue_ahead - pool)
        for order in at_level:
            if overflow <= EPS:
                break
            take = min(order.remaining, overflow)
            if take > EPS:
                self._fill(order, ts, order.price_ticks * self.cfg.tick,
                           take, maker=True)
                overflow -= take

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _advance(self, ts: int) -> None:
        """Activate in-flight placements/cancels whose latency has elapsed."""
        while self._pending and self._pending[0][0] <= ts:
            act_ts, _, kind, order = heapq.heappop(self._pending)
            if kind == "place":
                self._activate(order)
            elif self._do_cancel(order) and self.on_cancel:
                # cancel confirmation races back to the strategy one-way latency
                # after the venue removes the order
                self.on_cancel(order, act_ts)

    def _activate(self, order: Order) -> None:
        if order.status is not OrderStatus.PENDING:  # canceled while in flight
            return
        b = self.book
        crossing = False
        if b.ts >= 0:
            crossing = (order.price_ticks >= int(b.ask_pt[0]) if order.side is Side.BUY
                        else order.price_ticks <= int(b.bid_pt[0]))
        if crossing:
            if order.post_only:
                order.status = OrderStatus.REJECTED
                self.n_rejected += 1
                if self.on_reject:
                    self.on_reject(order)
                return
            self._take(order)
            if order.remaining > EPS:
                self._open(order, queue=0.0)
        else:
            disp = b.disp_at(order.side, order.price_ticks) if b.ts >= 0 else 0.0
            if disp is None:  # beyond L20: assume the worst visible level's qty
                disp = b.deep_queue_estimate(order.side)
            self._open(order, queue=disp)

    def _open(self, order: Order, queue: float) -> None:
        order.status = OrderStatus.OPEN
        order.queue_ahead = queue
        order.last_disp_qty = queue
        order.traded_since_book = 0.0
        order.lock_consumed = 0.0
        self.open_orders[order.id] = order

    def _take(self, order: Order) -> None:
        """Execute a marketable order against the current snapshot as taker."""
        b = self.book
        pts, qs = (b.ask_pt, b.ask_q) if order.side is Side.BUY else (b.bid_pt, b.bid_q)
        for i in range(len(pts)):
            lp = int(pts[i])
            if (order.side is Side.BUY and lp > order.price_ticks) or \
               (order.side is Side.SELL and lp < order.price_ticks):
                break
            take = min(order.remaining, float(qs[i]))
            if take > EPS:
                self._fill(order, order.ts_active, lp * self.cfg.tick, take, maker=False)
            if order.remaining <= EPS:
                break

    def _do_cancel(self, order: Order) -> bool:
        """Remove a resting/in-flight order. Returns True if it took effect
        (False if the order had already filled/canceled, e.g. a fill won the
        race), so the caller only notifies on real cancellations."""
        if order.status in (OrderStatus.OPEN, OrderStatus.PENDING):
            order.status = OrderStatus.CANCELED
            self.open_orders.pop(order.id, None)
            self.n_canceled += 1
            return True
        return False

    def _fill(self, order: Order, ts: int, price: float, qty: float, maker: bool) -> None:
        rate = (self.cfg.maker_fee_bps if maker else self.cfg.taker_fee_bps) * 1e-4
        fee = qty * price * rate
        self.account.apply_fill(order.side, price, qty, fee)
        order.filled += qty
        fill = Fill(order.id, ts, order.side, price, qty, maker, fee)
        self.fills.append(fill)
        if order.remaining <= EPS:
            order.status = OrderStatus.FILLED
            self.open_orders.pop(order.id, None)
        if self.on_fill:
            self.on_fill(fill, order)
