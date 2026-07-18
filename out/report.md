# Backtest report — glft

Period: 2026-03-19 … 2026-03-21

## PnL
| metric | USD |
|---|---|
| **Total net PnL** | **-4,864.32** |
| Realized (gross, trading) | -2,572.84 |
| Unrealized at end | +0.83 |
| Fees paid | -2,310.53 |
| Funding PnL | +18.21 |
| Return on capital | -4.864% |

## Daily PnL
| date | net PnL | realized gross | fees | funding | Δunrealized |
|---|---|---|---|---|---|
| 2026-03-19 | -2,070.84 | -1,128.84 | -945.01 | +3.16 | -0.15 |
| 2026-03-20 | -1,887.00 | -963.45 | -927.00 | +5.00 | -1.56 |
| 2026-03-21 | -906.48 | -480.54 | -438.53 | +10.05 | +2.54 |

## Inventory
| metric | value |
|---|---|
| Mean / std (ETH) | -0.101 / 1.523 |
| Max long / max short (ETH) | +5.931 / -5.975 |
| Mean abs (ETH) | 1.014 |
| Max abs notional (USD) | 12,870 |
| % time long / short / flat | 53.6 / 46.0 / 0.4 |
| End position (ETH) | +0.075 |

## Fills
| metric | value |
|---|---|
| Orders placed / canceled / rejected | 106,580 / 99,495 / 0 |
| Orders with ≥1 fill (fill rate) | 7,290 (6.84%) |
| Fill events | 8,416 |
| Volume | 7,164.47 ETH / 15,403,519 USD |
| Buy / sell volume (ETH) | 3,582.27 / 3,582.20 |
| Maker share | 100.0% |
| Avg fill size (ETH) | 0.851 |
| Net PnL per ETH traded | -0.679 USD |
| Two-sided quote uptime (any price, 1 Hz sampled) | 95.6% |

## Risk
| metric | value |
|---|---|
| Max drawdown | -4,871.59 (-4.87% of capital) |
| Sharpe (1-min, annualized) | -306.32 |
| Sortino (1-min, annualized) | -286.25 |
| PnL std per minute | 2.67 USD |
| Worst / best minute | -44.63 / +8.05 |

## Markouts after our fills (bps, signed: negative = adverse)
| horizon | mean | qty-weighted |
|---|---|---|
| 0s | -0.153 | -0.300 |
| 1s | -1.212 | -1.135 |
| 5s | -1.469 | -1.394 |
| 30s | -1.701 | -1.574 |
| 60s | -1.663 | -1.515 |
