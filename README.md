# Market Making — Take-Home

## Solution

- [STRATEGY.md](STRATEGY.md) — GLFT market-making strategy: quoting logic,
  calibration, inventory management, funding usage, results and conclusions
- [SIMULATOR.md](SIMULATOR.md) — backtesting engine design and assumptions
  (queue-position fill model, latency, PnL accounting, metrics)
- [DATA.md](DATA.md) — data profiling and microstructure facts driving the design
- Code: [mm_backtest/](mm_backtest/) · runner: [run_backtest.py](run_backtest.py)
  (`uv run python run_backtest.py`) · tests: [tests/](tests/)
  (`uv run --group dev pytest`) · report artifacts: `out/`

## Задание

Спроектировать **market making** бота и **отбэктестить** его на **3 календарных дня** данных (2026-03-19 … 2026-03-21).

Ожидаемый результат: описание стратегии, симулятор (fills, инвентарь, PnL), метрики за период и краткие выводы.

## Данные

Инструмент — **ETH perpetual** (~2200 USD), один рынок, без колонки символа.

| Папка | Файлы | Содержимое |
|-------|--------|------------|
| `data/orderbook/` | по файлу на день | Снимки стакана: 20 уровней bid/ask (`bid_price_i`, `ask_price_i`, `bid_qty_i`, `ask_qty_i`), `datetime` (наносекунды) |
| `data/trades/` | по файлу на день | Сделки: `datetime`, `price`, `size`, `is_maker_ask` (1 — агрессор покупатель, 0 — агрессор продавец) |
| `data/fundings/` | по файлу на день | Прогноз/ставка фандинга: `datetime`, `funding_rate` (~каждые 20 с) |
