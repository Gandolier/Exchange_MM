"""Strategy interface + shared grid-quoting machinery.

`ctx` is the Backtester facade: read state (ts, book, position, mid, equity)
and act (place_limit / cancel / cancel_all). All actions incur latency, and
fill/reject/cancel notifications (and the position they imply) arrive latency
later — the quoter below tracks its own working orders purely from those
notifications, never from live venue state, so the latency model is honest.
"""
from abc import ABC

from .orders import Fill, Order, Side

EPS = 1e-9


class Strategy(ABC):
    def on_start(self, ctx) -> None: ...
    def on_book(self, ctx) -> None: ...
    def on_trade(self, ctx, ts: int, price: float, size: float, direction: int) -> None: ...
    def on_fill(self, ctx, fill: Fill, order: Order) -> None: ...
    def on_reject(self, ctx, order: Order) -> None: ...
    def on_cancel(self, ctx, order: Order) -> None: ...
    def on_funding(self, ctx, ts: int, rate: float) -> None: ...
    def on_day_end(self, ctx, day: str) -> None: ...


class GridQuoter(Strategy):
    """Maintains a set of resting orders per side (one price level each) and
    reconciles it against a desired grid every requote.

    An order is considered *working* from the moment it is placed until a
    delivered notification retires it: fully-filled (cumulative acked fills
    reach its size), rejected, or cancel-confirmed. Liveness is never read
    from the engine — only from notifications that have cleared the latency
    queue — so the strategy's view of its own book lags the venue exactly as
    it would live. At most one working order sits at any (side, price)."""

    def __init__(self):
        self.working: dict[int, Order] = {}       # order_id -> Order we believe live
        self.at: dict[tuple[Side, int], int] = {}  # (side, price_ticks) -> order_id
        self.canceling: set[int] = set()
        self._acked: dict[int, float] = {}         # order_id -> cumulative filled qty

    # ---- notification-driven bookkeeping ----
    def _retire(self, order_id: int) -> None:
        o = self.working.pop(order_id, None)
        if o is not None and self.at.get((o.side, o.price_ticks)) == order_id:
            del self.at[(o.side, o.price_ticks)]
        self.canceling.discard(order_id)
        self._acked.pop(order_id, None)

    def on_fill(self, ctx, fill: Fill, order: Order) -> None:
        acc = self._acked.get(order.id, 0.0) + fill.qty
        self._acked[order.id] = acc
        if acc >= order.qty - EPS:          # fully filled -> gone from the book
            self._retire(order.id)

    def on_reject(self, ctx, order: Order) -> None:
        self._retire(order.id)

    def on_cancel(self, ctx, order: Order) -> None:
        self._retire(order.id)

    # ---- grid reconciliation ----
    def apply_grid(self, ctx, targets: dict[Side, list[tuple[int, float]]]) -> None:
        """targets[side] = list of (price_ticks, qty) to rest on that side.
        Cancels working orders whose price left the set; places new prices we
        do not already have working. A price whose order is mid-cancel is left
        alone until its cancel confirms (avoids a duplicate at that price)."""
        tick = ctx.book.tick
        for side in (Side.BUY, Side.SELL):
            want: dict[int, float] = {}
            for pt, qty in targets.get(side, ()):   # first qty wins on collision
                want.setdefault(int(pt), qty)
            for (s, pt), oid in list(self.at.items()):
                if s is side and pt not in want and oid not in self.canceling:
                    ctx.cancel(oid)
                    self.canceling.add(oid)
            for pt, qty in want.items():
                if (side, pt) in self.at:            # already working (or canceling) here
                    continue
                o = ctx.place_limit(side, pt * tick, qty, post_only=True)
                self.working[o.id] = o
                self.at[(side, pt)] = o.id


class NaiveJoinBest(GridQuoter):
    """Engine-validation placeholder, NOT the deliverable strategy.

    Joins best bid and best ask with fixed size; pulls a side when the
    inventory cap would be breached; requotes when best price moves."""

    def __init__(self, size: float = 1.0, max_pos: float = 5.0):
        super().__init__()
        self.size = size
        self.max_pos = max_pos

    def on_book(self, ctx) -> None:
        book = ctx.book
        pos = ctx.position
        self.apply_grid(ctx, {
            Side.BUY: [(int(book.bid_pt[0]), self.size)] if pos < self.max_pos else [],
            Side.SELL: [(int(book.ask_pt[0]), self.size)] if pos > -self.max_pos else [],
        })
