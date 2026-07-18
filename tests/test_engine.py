"""Engine/accounting unit tests on hand-built synthetic scenarios.

Prices in ticks (tick = 0.1 USD): 22000 ticks = 2200.0 USD.
"""
import numpy as np
import pytest

from mm_backtest.account import Account
from mm_backtest.config import SimConfig
from mm_backtest.engine import ExchangeSim
from mm_backtest.orders import OrderStatus, Side

MS = 1_000_000  # ns per ms


def mkbook(best_bid: int, best_ask: int, bid_qty=10.0, ask_qty=10.0, levels=20):
    bid_pt = np.array([best_bid - i for i in range(levels)], np.int32)
    ask_pt = np.array([best_ask + i for i in range(levels)], np.int32)
    bq = np.full(levels, bid_qty, np.float32)
    aq = np.full(levels, ask_qty, np.float32)
    return bid_pt, bq, ask_pt, aq


def mksim(latency_ms=0.0, queue_model="proportional", maker=0.0, taker=0.0):
    cfg = SimConfig(latency_ms=latency_ms, queue_model=queue_model,
                    maker_fee_bps=maker, taker_fee_bps=taker)
    acct = Account(cfg)
    return ExchangeSim(cfg, acct), acct, cfg


def test_queue_drain_and_overflow_partial_fill():
    sim, acct, _ = mksim()
    bid_pt, bq, ask_pt, aq = mkbook(22000, 22001, bid_qty=5.0)
    sim.on_book(0, bid_pt, bq, ask_pt, aq)
    o = sim.place_limit(0, Side.BUY, 2200.0, 2.0)
    sim._advance(0)
    assert o.status is OrderStatus.OPEN and o.queue_ahead == 5.0

    # aggressor sells 3 at our price: queue 5 -> 2, no fill
    sim.on_trade(1, 22000, 3.0, -1)
    assert o.queue_ahead == 2.0 and o.filled == 0.0
    # sells 3 more: 2 drain queue, 1 overflows to us -> partial fill
    sim.on_trade(2, 22000, 3.0, -1)
    assert o.queue_ahead == 0.0
    assert o.filled == pytest.approx(1.0)
    assert o.status is OrderStatus.OPEN
    # 1 more sell fills the rest
    sim.on_trade(3, 22000, 1.0, -1)
    assert o.status is OrderStatus.FILLED
    assert acct.pos == pytest.approx(2.0)
    assert acct.cash == pytest.approx(100_000.0 - 2.0 * 2200.0)


def test_trade_through_capped_by_print_size():
    sim, acct, _ = mksim()
    sim.on_book(0, *mkbook(22000, 22001))
    o = sim.place_limit(0, Side.BUY, 2200.0, 1.5)
    sim._advance(0)
    # print strictly below our bid: we'd have been hit first, but the
    # aggressor only traded 0.1 — flow conservation caps our fill at 0.1
    sim.on_trade(1, 21998, 0.1, -1)
    assert o.filled == pytest.approx(0.1)
    assert o.status is OrderStatus.OPEN
    assert o.queue_ahead == 0.0          # the print passed our level
    assert sim.fills[0].price == pytest.approx(2200.0)  # at OUR price
    sim.on_trade(2, 21998, 2.0, -1)      # enough volume completes us
    assert o.status is OrderStatus.FILLED
    assert acct.pos == pytest.approx(1.5)


def test_sell_side_symmetry():
    sim, acct, _ = mksim()
    sim.on_book(0, *mkbook(22000, 22001, ask_qty=2.0))
    o = sim.place_limit(0, Side.SELL, 2200.1, 1.0)
    sim._advance(0)
    assert o.queue_ahead == 2.0
    sim.on_trade(1, 22001, 2.5, +1)   # aggressor buys through queue
    assert o.filled == pytest.approx(0.5)
    sim.on_trade(2, 22003, 0.5, +1)   # trade-through above our ask
    assert o.status is OrderStatus.FILLED
    assert acct.pos == pytest.approx(-1.0)


def test_cancel_race_fill_wins():
    sim, _, _ = mksim(latency_ms=10.0)
    sim.on_book(0, *mkbook(22000, 22001, bid_qty=1.0))
    o = sim.place_limit(0, Side.BUY, 2200.0, 1.0)
    sim._advance(10 * MS)                 # order active at t=10ms
    sim.cancel(11 * MS, o.id)             # cancel lands at t=21ms
    sim.on_trade(15 * MS, 22000, 5.0, -1)  # fill at t=15ms beats it
    assert o.status is OrderStatus.FILLED
    sim._advance(30 * MS)                 # late cancel is a no-op
    assert o.status is OrderStatus.FILLED
    assert sim.n_canceled == 0


def test_cancel_before_activation():
    sim, _, _ = mksim(latency_ms=10.0)
    sim.on_book(0, *mkbook(22000, 22001))
    o = sim.place_limit(0, Side.BUY, 2200.0, 1.0)
    sim.cancel(1 * MS, o.id)   # lands t=11ms, after activation t=10ms
    sim._advance(20 * MS)
    assert o.status is OrderStatus.CANCELED
    assert not sim.open_orders


def test_post_only_reject_when_crossing():
    rejected = []
    sim, _, _ = mksim()
    sim.on_reject = rejected.append
    sim.on_book(0, *mkbook(22000, 22001))
    o = sim.place_limit(0, Side.BUY, 2200.1, 1.0)  # = best ask -> would cross
    sim._advance(0)
    assert o.status is OrderStatus.REJECTED and rejected == [o]


def test_marketable_taker_walk_and_rest():
    sim, acct, cfg = mksim(taker=3.5)
    bid_pt, bq, ask_pt, aq = mkbook(22000, 22001)
    aq[0], aq[1] = 1.0, 0.5   # 1.0 @ 2200.1, 0.5 @ 2200.2
    sim.on_book(0, bid_pt, bq, ask_pt, aq)
    o = sim.place_limit(0, Side.BUY, 2200.2, 2.0, post_only=False)
    sim._advance(0)
    assert o.filled == pytest.approx(1.5)
    assert o.status is OrderStatus.OPEN and o.queue_ahead == 0.0
    prices = [f.price for f in sim.fills]
    assert prices == [pytest.approx(2200.1), pytest.approx(2200.2)]
    assert all(not f.maker for f in sim.fills)
    notional = 1.0 * 2200.1 + 0.5 * 2200.2
    assert acct.fees == pytest.approx(notional * 3.5e-4)
    # remainder rests at 2200.2 with queue 0 -> next sell there fills us
    sim.on_trade(1, 22002, 0.5, -1)
    assert o.status is OrderStatus.FILLED


def test_queue_models_on_cancellation():
    # scenario chosen so the models actually diverge: queue_ahead (4) is
    # BELOW displayed (10) when cancels (4) hit, so the clamp is inactive
    # and only the proportional model attributes a share ahead of us.
    for model, expect in (("proportional", 2.4), ("pessimistic", 4.0)):
        sim, _, _ = mksim(queue_model=model)
        bid_pt, bq, ask_pt, aq = mkbook(22000, 22001, bid_qty=10.0)
        sim.on_book(0, bid_pt, bq, ask_pt, aq)
        o = sim.place_limit(0, Side.BUY, 2200.0, 1.0)
        sim._advance(0)
        assert o.queue_ahead == 10.0
        sim.on_trade(1, 22000, 6.0, -1)   # trades drain front: queue 4
        assert o.queue_ahead == pytest.approx(4.0)
        bq2 = bq.copy(); bq2[0] = 10.0    # refills behind us: queue stays 4
        sim.on_book(2, bid_pt, bq2, ask_pt, aq)
        assert o.queue_ahead == pytest.approx(4.0)
        bq3 = bq.copy(); bq3[0] = 6.0     # 4 cancels, no trades
        sim.on_book(3, bid_pt, bq3, ask_pt, aq)
        # proportional: 4 - 4*(4/10) = 2.4 ; pessimistic: cancels behind -> 4
        assert o.queue_ahead == pytest.approx(expect)


def test_pessimistic_keeps_queue_when_cancels_behind():
    sim, _, _ = mksim(queue_model="pessimistic")
    bid_pt, bq, ask_pt, aq = mkbook(22000, 22001, bid_qty=10.0)
    sim.on_book(0, bid_pt, bq, ask_pt, aq)
    o = sim.place_limit(0, Side.BUY, 2200.0, 1.0)
    sim._advance(0)
    bq2 = bq.copy(); bq2[0] = 7.0   # cancels only, disp 7 > queue_ahead? no: 10 -> clamp
    sim.on_book(1, bid_pt, bq2, ask_pt, aq)
    assert o.queue_ahead == pytest.approx(7.0)  # clamped: ahead must be displayed
    bq3 = bq.copy(); bq3[0] = 9.0   # adds behind us: queue unchanged
    sim.on_book(2, bid_pt, bq3, ask_pt, aq)
    assert o.queue_ahead == pytest.approx(7.0)


def test_book_cross_fills_resting_order():
    sim, acct, _ = mksim()
    sim.on_book(0, *mkbook(22000, 22001))
    o = sim.place_limit(0, Side.SELL, 2200.5, 1.0)
    sim._advance(0)
    # gap move: best bid jumps to 22007, STRICTLY through our 22005 ask
    sim.on_book(1, *mkbook(22007, 22008))
    assert o.status is OrderStatus.FILLED
    assert sim.fills[0].price == pytest.approx(2200.5)  # at OUR price
    assert acct.pos == pytest.approx(-1.0)


def test_locked_book_fills_only_displayed_demand():
    sim, _, _ = mksim()
    sim.on_book(0, *mkbook(22000, 22001))
    o = sim.place_limit(0, Side.SELL, 2200.5, 1.0)
    sim._advance(0)
    # best bid rises exactly TO our ask price, showing 0.4 of demand
    bid_pt, bq, ask_pt, aq = mkbook(22005, 22006)
    bq[0] = 0.4
    sim.on_book(1, bid_pt, bq, ask_pt, aq)
    assert o.filled == pytest.approx(0.4)
    assert o.status is OrderStatus.OPEN and o.queue_ahead == 0.0
    # same locked snapshot repeated: demand already consumed, NO re-fill
    sim.on_book(2, bid_pt, bq, ask_pt, aq)
    assert o.filled == pytest.approx(0.4)
    # displayed demand grows to 0.7: only the delta (0.3) fills us
    bq2 = bq.copy(); bq2[0] = 0.7
    sim.on_book(3, bid_pt, bq2, ask_pt, aq)
    assert o.filled == pytest.approx(0.7)
    # book unlocks then locks again with fresh demand -> counter resets
    sim.on_book(4, *mkbook(22003, 22004))
    bq3 = bq.copy(); bq3[0] = 0.3
    sim.on_book(5, bid_pt, bq3, ask_pt, aq)
    assert o.status is OrderStatus.FILLED  # 0.7 + 0.3


def test_same_price_orders_share_flow_with_priority():
    sim, acct, _ = mksim()
    bid_pt, bq, ask_pt, aq = mkbook(22000, 22001, bid_qty=1.0)
    sim.on_book(0, bid_pt, bq, ask_pt, aq)
    o1 = sim.place_limit(0, Side.BUY, 2200.0, 1.0)
    o2 = sim.place_limit(0, Side.BUY, 2200.0, 1.0)
    sim._advance(0)
    # print of 1.5 at our level: 1.0 drains displayed queue, 0.5 overflows —
    # o1 (earlier) takes it all, o2 gets nothing
    sim.on_trade(1, 22000, 1.5, -1)
    assert o1.filled == pytest.approx(0.5)
    assert o2.filled == pytest.approx(0.0)
    # next print of 2.0: queues are 0 -> o1 completes (0.5), o2 gets 1.0
    sim.on_trade(2, 22000, 2.0, -1)
    assert o1.status is OrderStatus.FILLED
    assert o2.filled == pytest.approx(1.0)
    # conservation: our total fills (2.0) never exceed tape volume (3.5)
    assert acct.pos == pytest.approx(2.0)


def test_latency_order_inactive_before_arrival():
    sim, _, _ = mksim(latency_ms=30.0)
    sim.on_book(0, *mkbook(22000, 22001, bid_qty=0.5))
    o = sim.place_limit(0, Side.BUY, 2200.0, 1.0)
    sim.on_trade(10 * MS, 21990, 5.0, -1)  # huge sweep before activation
    assert o.filled == 0.0 and o.status is OrderStatus.PENDING
    sim._advance(30 * MS)
    assert o.status is OrderStatus.OPEN


def test_account_identity_and_avg_cost():
    cfg = SimConfig(maker_fee_bps=1.0)
    a = Account(cfg)
    a.apply_fill(Side.BUY, 2200.0, 2.0, fee=0.44)
    a.apply_fill(Side.BUY, 2100.0, 2.0, fee=0.42)
    assert a.avg_px == pytest.approx(2150.0)
    a.apply_fill(Side.SELL, 2200.0, 3.0, fee=0.66)   # realize 3*(2200-2150)
    assert a.realized == pytest.approx(150.0)
    a.apply_fill(Side.SELL, 2000.0, 3.0, fee=0.60)   # close 1 (-150), flip short 2 @2000
    assert a.realized == pytest.approx(0.0)
    assert a.pos == pytest.approx(-2.0) and a.avg_px == pytest.approx(2000.0)
    mark = 1990.0
    lhs = a.equity(mark) - a.initial_cash
    rhs = a.realized - a.fees + a.funding + a.unrealized(mark)
    assert lhs == pytest.approx(rhs)
    assert a.unrealized(mark) == pytest.approx(20.0)


def test_funding_sign_and_accrual():
    cfg = SimConfig(funding_interval_hours=1.0)
    a = Account(cfg)
    a.apply_fill(Side.BUY, 2000.0, 1.0, fee=0.0)
    # long 1 ETH, +1bp rate over half an hour: longs PAY half a bp of notional
    a.apply_funding(rate=1e-4, dt_ns=int(1800e9), mark=2000.0)
    assert a.funding == pytest.approx(-0.1)
    a.apply_fill(Side.SELL, 2000.0, 2.0, fee=0.0)  # now short 1
    a.apply_funding(rate=1e-4, dt_ns=int(1800e9), mark=2000.0)
    assert a.funding == pytest.approx(0.0)  # short RECEIVES the same


def test_fees_split_maker_taker():
    sim, acct, _ = mksim(maker=1.0, taker=3.5)
    sim.on_book(0, *mkbook(22000, 22001, bid_qty=0.0))
    o = sim.place_limit(0, Side.BUY, 2200.0, 1.0)
    sim._advance(0)
    sim.on_trade(1, 22000, 1.0, -1)
    assert o.status is OrderStatus.FILLED
    assert sim.fills[0].maker and sim.fills[0].fee == pytest.approx(2200.0 * 1e-4)
