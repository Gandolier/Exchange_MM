from dataclasses import dataclass


@dataclass
class SimConfig:
    """All simulator assumptions live here so they are explicit and tunable.

    Defaults reflect a Hyperliquid ETH perpetual, base tier (tier 0):
      - maker 1.5 bps / taker 4.5 bps
        (https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees).
        Fees fall with 14-day volume; maker reaches 0.0 bps at tier 4 (>$500M)
        and the dedicated maker-rebate program pays down to -0.3 bps (the venue
        pays you to quote). Unlike a fee, a rebate can flip a marginal MM
        positive — select tiers via --fee-tier.
      - funding settles **hourly** (Hyperliquid), accrued continuously between
        the ~20 s rate snapshots: pnl -= pos * mid * rate * dt/1h.
      - 10 ms one-way latency for order placement, cancellation AND fill/cancel
        notifications (a colocated maker; snapshot throttling makes PnL nearly
        latency-invariant anyway — see SIMULATOR.md).
    """

    tick: float = 0.1            # price tick, USD
    lot: float = 0.001           # min size increment, ETH
    latency_ms: float = 10.0     # one-way latency: decision -> exchange
    maker_fee_bps: float = 1.5   # Hyperliquid base tier (tier 0)
    taker_fee_bps: float = 4.5   # Hyperliquid base tier (tier 0)
    funding_interval_hours: float = 1.0  # Hyperliquid settles hourly
    # queue model for cancellations at our price level:
    #   "proportional": cancels drain the queue pro-rata (incl. ahead of us)
    #   "pessimistic":  cancels always come from behind us
    # Trades always drain from the front; queue_ahead is clamped to displayed
    # qty in both modes (people ahead of us must be visible in the book).
    queue_model: str = "proportional"
    initial_cash: float = 100_000.0
    sample_interval_s: float = 1.0  # equity/inventory sampling grid

    @property
    def latency_ns(self) -> int:
        return int(self.latency_ms * 1e6)

    @property
    def funding_interval_ns(self) -> int:
        return int(self.funding_interval_hours * 3600e9)
