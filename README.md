# token_monitor

Скрининг пулов и ресерч токенов для LP на BSC, Base, Robinhood Chain.

## Воронка

1. **Screener** (Codex `filterPairs`): один запрос → top-N пулов по всем сетям
2. **`classify_market`**: математика, 0 запросов → FAST / SLOW / SKIP
3. **Security** (GMGN) → `security_flags` *(этап 3)*
4. **Ручное подтверждение** в Telegram *(этап 4)*
5. **Deep research**: X, Fomo, LP Agent, LLM-сводка через Ollama *(этап 5)*

## Логика вердикта

Решение принимается не по APR, а по времени, за которое комиссии покрывают
потерю LP-позиции при стресс-падении цены (full-range: `1 − √(1 − drop)`).

- **SLOW**: объём стабилен (`min_persistence ≤ vol_1h·24 / vol_24h ≤ max_persistence`),
  и комиссии за 24ч покрывают стресс за `slow_horizon_days`.
- **FAST**: при текущем темпе (последний час) стресс покрывается за `fast_horizon_hours`.
- **SKIP**: всё остальное, а также накрученный объём (wash), даже если математика проходит:
  средняя сделка > `max_trade_share` ликвидности, < `min_unique_wallets` кошельков за 24ч
  или > `max_fresh_wallets` свопов с кошельков моложе суток. Комиссии с такого объёма
  реальны, но это приманка и он исчезает вместе с ботами.

Пороги задаются в `.env` через `PARAM_*` (см. `.env.example`).

## Статусы кандидатов

`pending → approved → collected → researched`, либо `pending → rejected`.
Переходы атомарные: повторное нажатие кнопки или гонка процессов не ломает состояние.

## Запуск

```bash
python -m token_monitor screen            # FAST/SLOW, отсортировано по скорости окупаемости
python -m token_monitor screen --all      # вместе со SKIP
python -m token_monitor analyze 0x…      # все пулы токена + риск-оценка Codex
```

Screener берёт `CODEX_API_KEY`, `SCREEN_LIMIT` (≤ 200), `SCREEN_RANK_BY`,
`SCREEN_MIN_VOLUME_24H`, `SCREEN_MIN_FEE_BPS`; порог ликвидности — общий `PARAM_MIN_LIQUIDITY`,
фильтруется на стороне Codex.

Комиссия пула: `poolFeeBps` → `pair.fee` (v3/v4, миллионные доли) → известная v2-фабрика.
Пулы с динамической комиссией и неизвестные v2-форки отбрасываются. v4-пулы с `fee = 0`
(лаунчпады, комиссию забирает хук) получают SKIP `no LP fee`.
Серверный `SCREEN_MIN_FEE_BPS` работает по `poolFeeBps` и отсекает пулы, где он не заполнен;
`analyze` этот фильтр не использует.

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
pytest
```
