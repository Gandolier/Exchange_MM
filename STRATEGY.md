# Market-Making Strategy — Guéant–Lehalle–Fernandez-Tapia (GLFT)

Implementation: [mm_backtest/glft.py](mm_backtest/glft.py). Run:
`uv run python run_backtest.py` (defaults: GLFT, all 3 days, γ = 0.20, 1 ETH
quotes, ±5 ETH cap, 10 ms latency, Hyperliquid base-tier fees maker 1.5 / taker
4.5 bps, hourly funding).

## 1. Quoting logic

The strategy is the closed-form approximation of the **Guéant–Lehalle–
Fernandez-Tapia** optimal market-making model ([[1]](#references), [[2]](#references)). Following Guéant, *Optimal market making* ([[1]](#references), eq. 4.6–4.7), the optimal distances of the bid and ask quotes from the fair price, for inventory $q$, tick step $\Delta$ and risk aversion $\xi=\gamma$, are

$$
\delta^{b}_{\ast}(q) = c_1 + \left(\tfrac{\Delta}{2} + q\right)\sigma\,c_2,
\qquad
\delta^{a}_{\ast}(q) = c_1 + \left(\tfrac{\Delta}{2} - q\right)\sigma\,c_2,
$$

with fill-intensity coefficients

$$
c_1 = \frac{1}{\xi\Delta}\ln\!\left(1 + \frac{\xi\Delta}{k}\right),
\qquad
c_2 = \sqrt{\dfrac{\gamma}{2A\Delta k}\left(1 + \frac{\xi\Delta}{k}\right)^{\frac{k}{\xi\Delta}+1}} .
$$

The distance splits into an inventory-independent **half-spread** $h$ and an
inventory-linear **skew** $s$; two tuning factors $\mathrm{adj_1},\mathrm{adj_2}$
scale each (both $1$ at the raw optimum):

$$
h = \mathrm{adj_1}\left(c_1 + \tfrac{\Delta}{2}\,\sigma c_2\right),
\qquad
s = \mathrm{adj_2}\,\sigma c_2 .
$$

Quoting a grid of $n$ levels per side, spaced $g$ ticks apart, about a
funding-tilted fair price $p_f$:

$$
\mathrm{bid}_i = p_f - (h + s\,q) - i\,g,
\qquad
\mathrm{ask}_i = p_f + (h - s\,q) + i\,g,
\qquad i = 0,\dots,n-1,
$$

$$
p_f = m - \varphi\, r\, m,
\qquad
q = \frac{\text{position}}{\text{order size}},
$$

where $m$ is the mid, $r$ the funding-rate snapshot and $\varphi$ the
`funding_mult`. The fill intensity as a function of quote depth is
$\lambda(\delta) = A\,e^{-k\delta}$ (calibrated in §2). In code
([glft.py](mm_backtest/glft.py)), `compute_coeff` returns $c_1,c_2$ and
`on_book` builds the ladder.

The half-spread widens with volatility $\sigma$ and risk aversion $\gamma$ and
tightens with market activity ($A,k$). The skew shifts *both* quotes against the
position — long inventory lowers bid and ask together (buy less, sell sooner).
Prices are snapped to the tick grid and clamped to the touch (the spread is
pinned at 1 tick here, so quoting inside it is impossible).

On this data the fitted half-spread is $\approx 13$ ticks (at the default
$\gamma=0.20$), so even the innermost quote ($i=0$) rests ~13 ticks (1.3 USD)
behind the touch. Grid trading and the
adjustment factors — the tutorial's two performance levers — are implemented
and analysed in §5; the default is a single skewed quote per side ($n=1$).

## 2. Parameter calibration (rolling, no look-ahead)

Every 5 s over a 10-minute rolling window of 100 ms buckets, using only data
already seen:

- **$\sigma$** — std of 100 ms mid-price changes in ticks $\times\sqrt{10}\to$
  ticks/$\sqrt{s}$. This is quoting-timescale volatility: it deliberately
  includes microstructure noise (~7.8 ticks/$\sqrt{s}$ vs ~1.3 implied by
  1-min realized vol), because that noise is exactly what fills resting quotes.
- **$A,k$** — fill-intensity curve $\lambda(\delta) = A\,e^{-k\delta}$. Each
  bucket records the max trade depth $\delta$ (ticks from the prevailing mid);
  a bucket reaching depth $d$ counts as an arrival for every level $\le d$.
  Then $\lambda(\delta) = \text{counts}(\delta)/\text{window seconds}$, and a
  log-linear fit ($\ln\lambda$ vs $\delta$) over levels with arrivals gives
  $k$ (slope) and $A$ (intercept).
- Warm-up: no quotes for the first 5 minutes; degenerate fits (no trades,
  σ = 0, k ≤ 0) keep the previous parameters.

Representative last-window fit (day 3): **A ≈ 0.28 /s, k ≈ 0.23 /tick,
σ ≈ 7.8 ticks/√s → half-spread ≈ 13.0 ticks, skew ≈ 19.6 ticks per 1-ETH unit**
(at the default γ = 0.20; the c1/c2 coefficients scale with √γ).

## 3. Inventory management

Four layers:
1. the continuous GLFT **skew** (σ·c2 ≈ 19.6 ticks per 1-ETH unit — dominant);
2. the **grid inventory budget**: when grid trading is on, each side rests only
   as many ladder levels as fit before |pos| would hit the cap, so a trend
   cannot cascade-fill several deep levels through the limit;
3. the hard **±max_pos cap** (side pulled entirely);
4. **latency-realistic position** — fills reach the strategy 10 ms after they
   happen, so brief cap overshoots of ~1 ETH occur, exactly as live.

Default result (γ = 0.20): inventory mean −0.10 ETH, **std 1.52 ETH**, never
beyond ±6 — the skew does its job on two −3 % days. The funding tilt adds a
little directional variance; without it inventory is tighter still (see §4).

## 4. Funding-rate usage

Two uses:
1. **Quote tilt** — the fair price is shifted by the funding snapshot:
   `fair = mid − funding_mult · rate · mid`. Positive funding (longs pay)
   pushes both quotes down → the book leans short and collects funding;
   negative funding leans long. `funding_mult = 1` prices in one full
   settlement payment and is *interval-agnostic*, so the tilt is robust to the
   hourly-vs-other settlement assumption.
2. **PnL accrual** — funding on held inventory accrues continuously at the
   prevailing rate, Hyperliquid's **hourly** settlement (see SIMULATOR.md).

Effect: funding PnL **+18 USD** with the tilt on (vs ≈ 0 off) — the book
actively leans to the receiving side. It is small in absolute terms, and it
adds directional inventory variance; on trending days that is close to a wash,
so `funding_mult` is best treated as a risk knob (< 1 for a gentler lean).

## 5. Parameter sensitivity — γ, adjustment factors, grid: risk dials, not alpha

A one-at-a-time sweep (all 3 days, 10 ms, base fees) exposes the defining fact
of this market: **per-ETH PnL is pinned at −0.65…−0.71 across *every* parameter**
— γ, adj1, adj2, funding_mult, size, grid depth. Nothing improves fill quality;
each knob only changes *how much* you trade, and total PnL ≈ per-ETH × volume,
so the only way to lose less is to trade less.

| config (all else at default) | net PnL | per-ETH | volume | inv std | uptime |
|---|---|---|---|---|---|
| adj1 = 0.5 (tighter) | −17,467 | −0.658 | 26,530 | 2.22 | 86.6 % |
| adj2 = 3.0 | −11,074 | −0.654 | 16,925 | 1.27 | 97.8 % |
| γ = 0.05 (old default) | −6,756 | −0.666 | 10,140 | 2.18 | 86.5 % |
| **γ = 0.20 (default)** | **−4,864** | −0.679 | 7,164 | **1.52** | 95.6 % |
| γ0.20 + adj2=2 + adj1=1.5 | −2,969 | −0.660 | 4,497 | 0.94 | 99.1 % |
| adj1 = 2.0 (wider) | −1,608 | −0.706 | 2,276 | 2.29 | 87.1 % |

The bottom two rows "beat" the default only by quoting so wide they barely
trade — the limit of that direction is quoting nothing. `funding_mult` is kept
out of the recommended set: raising it cuts loss on these down-trending days,
but only by taking directional risk that inflates inventory std ~3× — it
overfits the regime, not a robust gain.

**γ = 0.20 is the chosen default**: the one Pareto move that beats the old
γ = 0.05 baseline on PnL *and* inventory std *and* drawdown *and* uptime at once
(wider quotes + harder skew), while staying a real, ~96 %-present two-sided
maker. It is not alpha — the −28 % loss is −28 % volume, at unchanged per-ETH.

**Grid trading** rests a ladder of `n` orders per side, budget-capped so a trend
cannot cascade several deep levels through the position limit. At the default γ:

| grid | net PnL | volume (ETH) | per-ETH | max \|pos\| | inv std |
|---|---|---|---|---|---|
| 1 (default) | **−4,864** | 7,164 | −0.679 | ±6.0 | 1.52 |
| 5 | −6,346 | 9,550 | −0.664 | ±7.8 | 1.57 |

Same verdict: the grid adds ~33 % volume and pushes inventory to ±7.8 at
**identical per-ETH quality** — deeper fills are *not* better-selected here, so
it strictly worsens PnL. Grid pays on venues with a spread to capture and
mean-reversion at depth; a 1-tick spread with short-horizon momentum has
neither. Grid stays available (`--grid-num`); the default is 1.

## 6. Results (default: GLFT γ = 0.20, grid 1, Hyperliquid base tier, 10 ms)

| | GLFT | naive join-best (control) |
|---|---|---|
| Net PnL | **−4,864** | −53,384 |
| PnL / ETH traded | −0.679 | −0.654 |
| Volume (ETH) | 7,164 | 81,568 |
| Fees | 2,311 | 26,245 |
| Funding PnL | +18 | +2 |
| Inventory std (ETH) | 1.52 | 3.87 |
| Max drawdown | −4,872 (−4.9 %) | −53,384 (−53.4 %) |
| Markout 60 s (qty-wtd, bps) | −1.515 | −1.523 |
| Two-sided uptime | 95.6 % | 69.2 % |

Daily net PnL (GLFT): −2,071 / −1,887 / −906 — losses scale with realized
volatility (70 % / 61 % / 35 % annualized), as adverse selection predicts.

Note GLFT's per-ETH loss (−0.679) is *not* better than naive's (−0.654): the
two have near-identical per-fill economics because the same microstructure
selects both. GLFT loses **11× less in total** because it trades **11× less**
(deep, inventory-aware quotes instead of camping the touch) and controls
inventory (std 1.52 vs 3.87, drawdown −4.9 % vs −53.4 %). Its edge is knowing
when *not* to quote, which the model derives from first principles (wide quotes
when k is low and σ high). The naive control fills ≈ the entire market tape —
the known optimistic-fill artifact of a zero-impact model (SIMULATOR.md) — so
it is a loose worst-case bound, not a realistic competitor.

## 7. Analysis — why the PnL is negative, and what it would take

Fee-tier sensitivity (default config; fills are fee-independent and 100 % maker,
so this is exact arithmetic on the $15.4 M maker notional):

| Hyperliquid tier | maker fee | fees paid | **net PnL** | per-ETH |
|---|---|---|---|---|
| base (tier 0) | +1.5 bps | −2,311 | −4,864 | −0.679 |
| tier 4 (>$500M) | 0.0 bps | 0 | −2,554 | −0.356 |
| maker-rebate (mm) | −0.3 bps | **+462** | **−2,092** | −0.292 |

The decomposition is exact. At **zero maker fee** the loss is −2,554 ≈ the
realized adverse selection alone (per-ETH −0.356 USD = −1.66 bps ≈ the −1.52 bps
60 s markout). Fees are pure additive drag on top of that.

- **The spread cannot pay for the risk.** Half the tick (0.05 USD) is the most
  a fill can capture; even the base maker fee (0.27 USD/fill) is ~5× that.
- **Adverse selection is structural** in this fill model + data. Fills happen
  only when price reaches the quote, and 03-19/03-21 trended −3 %. Per-ETH loss
  is flat (−0.65…−0.71 net) across γ, adj1, adj2, funding_mult, grid depth and
  size (§5) — depth changes volume and risk, never quality. GLFT (no alpha
  term) quotes symmetrically around mid; it manages inventory optimally but has
  no view on direction.
- **The maker rebate helps but does not flip it.** Hyperliquid's best maker
  tier pays −0.3 bps; it turns fees from −2,311 into a **+462 credit** and
  cuts the loss to −2,092 — but the ~−1.6 bps of adverse selection is ~5× the
  rebate. Break-even would need a rebate near 1.6 bps, which no tier offers.
  This is the honest ceiling of a symmetric passive quoter here.
- **Latency is not the lever**: with ~50–70 ms throttled snapshots the adverse
  move and the fill it causes arrive in the same event, so 5 ms vs 100 ms
  barely moves PnL (the default 10 ms and the old 30 ms differ by < 3 %) — a
  limitation of snapshot data, stated openly.
- **What would flip the sign**: (a) an alpha input — microprice / order-flow
  imbalance so quotes lean *ahead* of the move rather than symmetrically around
  a lagging mid (outside GLFT's model); (b) a calmer / mean-reverting regime
  where depth fills revert instead of trending through; (c) a larger maker
  rebate than any live tier provides.

GLFT delivers exactly what it promises — near-flat inventory (mean −0.10 ETH),
96 % two-sided presence, funding capture, and an 11× smaller loss than the
naive control at equal per-fill economics. But on these three trending days, at
Hyperliquid's fees, on a 1-tick market, **no symmetric passive quoter is
profitable** — even at the maker-rebate tier the residual is pure adverse
selection — and the backtest is honest about it rather than engineered to hide
it.
