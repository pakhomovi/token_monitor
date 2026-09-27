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

## Сценарии LP-стратегий

```bash
python -m token_monitor analyze <CA> --strategy spot   --depth 0.7 --size 1000
python -m token_monitor analyze <CA> --strategy bidask --depth 0.7 --size 1000
```

Позиция в quote (ETH/BNB/USD) в диапазоне от цены входа до −depth: `spot` — равный капитал
по бинам, `bidask` — капитал растёт к нижней границе. Таблица по уровням цены: доля в токене,
средняя цена выкупа, PnL позиции без комиссий, комиссии/день и сколько дней стоять на этом
уровне, чтобы комиссии покрыли PnL. Стоимость v3/v4-позиции зависит только от текущей цены,
поэтому частичный откат — это подъём по той же таблице.

Допущения: ликвидность пула = full-range эквивалент TVL (оптимистично, если другие LP
сконцентрированы у цены); объём в токенах постоянен (в $ падает вместе с ценой).

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
pytest
```
