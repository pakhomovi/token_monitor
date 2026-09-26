# token_monitor

Скрининг пулов и ресерч токенов для LP на BSC, Base, Robinhood Chain.

## Воронка

1. **Screener**: один запрос к индексатору → top-N пулов *(этап 2)*
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
- **SKIP**: всё остальное.

Пороги задаются в `.env` через `PARAM_*` (см. `.env.example`).

## Статусы кандидатов

`pending → approved → collected → researched`, либо `pending → rejected`.
Переходы атомарные: повторное нажатие кнопки или гонка процессов не ломает состояние.

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
pytest
```
