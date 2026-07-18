from dataclasses import dataclass
from enum import IntEnum


class Side(IntEnum):
    BUY = 1
    SELL = -1


class OrderStatus(IntEnum):
    PENDING = 0    # sent, still in flight (latency)
    OPEN = 1       # resting on the book
    FILLED = 2
    CANCELED = 3
    REJECTED = 4   # post-only would have crossed


@dataclass
class Order:
    id: int
    side: Side
    price_ticks: int
    qty: float
    ts_created: int   # ns, decision time
    ts_active: int    # ns, ts_created + latency
    post_only: bool = True
    status: OrderStatus = OrderStatus.PENDING
    filled: float = 0.0
    # queue-position state (valid while OPEN)
    queue_ahead: float = 0.0        # displayed qty in front of us at our price
    last_disp_qty: float = 0.0      # displayed qty at our price, last snapshot
    traded_since_book: float = 0.0  # volume traded at our price since snapshot
    lock_consumed: float = 0.0      # opp. displayed qty already used by lock-fills

    @property
    def remaining(self) -> float:
        return self.qty - self.filled

    def price(self, tick: float) -> float:
        return self.price_ticks * tick


@dataclass(frozen=True)
class Fill:
    order_id: int
    ts: int          # ns
    side: Side       # our side
    price: float
    qty: float
    maker: bool
    fee: float
