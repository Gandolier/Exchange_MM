from .config import SimConfig
from .orders import Side

_EPS = 1e-12


class Account:
    """Inventory + PnL accounting.

    Realized PnL uses the average-cost method. The invariant maintained
    (asserted in tests):

        equity(mark) - initial_cash
            == realized - fees + funding + pos * (mark - avg_px)
    """

    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.initial_cash = cfg.initial_cash
        self.cash = cfg.initial_cash
        self.pos = 0.0          # signed ETH
        self.avg_px = 0.0       # avg entry price of open position
        self.realized = 0.0     # gross of fees
        self.fees = 0.0
        self.funding = 0.0      # cumulative funding PnL (signed)

    def apply_fill(self, side: Side, price: float, qty: float, fee: float) -> None:
        q = float(side) * qty
        old = self.pos
        if old * q >= 0.0:
            tot = abs(old) + qty
            if tot > _EPS:
                self.avg_px = (self.avg_px * abs(old) + price * qty) / tot
        else:
            close = min(qty, abs(old))
            self.realized += close * (price - self.avg_px) * (1.0 if old > 0 else -1.0)
            if qty > abs(old) + _EPS:  # position flips through zero
                self.avg_px = price
        self.pos = old + q
        if abs(self.pos) < _EPS:
            self.pos = 0.0
            self.avg_px = 0.0
        self.cash += -q * price - fee
        self.fees += fee

    def apply_funding(self, rate: float, dt_ns: int, mark: float) -> None:
        """Continuous accrual: positive rate means longs pay shorts."""
        if dt_ns <= 0 or self.pos == 0.0:
            return
        amt = -self.pos * mark * rate * (dt_ns / self.cfg.funding_interval_ns)
        self.cash += amt
        self.funding += amt

    def unrealized(self, mark: float) -> float:
        return self.pos * (mark - self.avg_px)

    def equity(self, mark: float) -> float:
        return self.cash + self.pos * mark
