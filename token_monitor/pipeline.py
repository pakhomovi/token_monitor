from dataclasses import dataclass
from typing import Iterable

from .models import PoolSnapshot, Verdict
from .scoring import MarketResult, Params, classify_market, stress_model


@dataclass(frozen=True, slots=True)
class Scored:
    pool: PoolSnapshot
    market: MarketResult

    @property
    def breakeven_hours(self) -> float:
        # Единая шкала для сортировки FAST (темп последнего часа) и SLOW (темп суток)
        m = self.market.metrics
        if self.market.verdict is Verdict.FAST:
            return m["breakeven_hours_now"]
        # Дно диапазона маловероятно, но это лучшая метрика для сравнения пулов между собой
        return m.get("cover_100", m["breakeven_days"]) * 24


def screen(pools: Iterable[PoolSnapshot], cfg: Params = Params(),
           keep_skipped: bool = False) -> list[Scored]:
    """Этап 2: классификация без сетевых запросов. FAST выше SLOW, внутри — по скорости окупаемости."""
    model = stress_model(cfg)
    scored = [Scored(p, classify_market(p, cfg, model)) for p in pools]
    order = {Verdict.FAST: 0, Verdict.SLOW: 1, Verdict.SKIP: 2}
    if not keep_skipped:
        scored = [s for s in scored if s.market.verdict is not Verdict.SKIP]
    return sorted(scored, key=lambda s: (order[s.market.verdict],
                                         s.breakeven_hours if s.market.metrics else 0.0))
