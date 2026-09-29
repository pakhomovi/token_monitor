"""LP-стратегии «выкуп падения»: ликвидность в quote (ETH/BNB/USD) в диапазоне [P0·(1−depth), P0].

Цена входа нормирована к 1, стоимость считается в quote. Позиция v3/v4 не зависит от пути цены:
стоимость определяется только текущей ценой, поэтому откат — это движение вверх по той же кривой,
а путь влияет лишь на накопленные комиссии.
"""
from dataclasses import dataclass
from enum import Enum
from math import sqrt


class Shape(str, Enum):
    SPOT = "spot"        # равномерная ликвидность, как одна позиция Uniswap
    BIDASK = "bidask"    # капитал линейно растёт к нижней границе


@dataclass(frozen=True, slots=True)
class Bin:
    pa: float
    pb: float
    liquidity: float


@dataclass(frozen=True, slots=True)
class State:
    value: float          # стоимость позиции в quote без комиссий
    token_share: float    # доля стоимости в токене
    avg_entry: float      # средняя цена выкупленного токена (доля от P0), 0 если не выкупали


class RangePosition:
    def __init__(self, size: float, depth: float, shape: Shape = Shape.SPOT, bins: int = 50):
        if size <= 0 or not 0 < depth < 1 or bins < 1:
            raise ValueError("size > 0, 0 < depth < 1, bins >= 1")
        self.size, self.depth, self.shape = size, depth, shape
        self.bottom = 1 - depth
        ratio = self.bottom ** (1 / bins)
        # Бин 0 — сразу под ценой входа, последний — у нижней границы
        edges = [(ratio ** (i + 1), ratio ** i) for i in range(bins)]
        # spot = одна позиция Uniswap: ликвидность L одинакова по диапазону, капитал бина ∝ √pb − √pa.
        # bidask: капитал линейно растёт к нижней границе
        weights = ([sqrt(pb) - sqrt(pa) for pa, pb in edges] if shape is Shape.SPOT
                   else [float(i + 1) for i in range(bins)])
        total = sum(weights)
        self.bins: list[Bin] = [Bin(pa, pb, size * w / total / (sqrt(pb) - sqrt(pa)))
                                for (pa, pb), w in zip(edges, weights)]

    def state(self, p: float) -> State:
        token = quote = 0.0
        for b in self.bins:
            sp = min(max(sqrt(p), sqrt(b.pa)), sqrt(b.pb))
            token += b.liquidity * (1 / sp - 1 / sqrt(b.pb))
            quote += b.liquidity * (sp - sqrt(b.pa))
        value = token * p + quote
        spent = self.size - quote
        return State(value, token * p / value if value else 0.0, spent / token if token else 0.0)

    def active_liquidity(self, p: float) -> float:
        return next((b.liquidity for b in self.bins if b.pa < p <= b.pb), 0.0)

    def daily_fee_yield(self, p: float, fee_bps: float, vol_24h: float, pool_tvl: float) -> float:
        """Комиссии за сутки / размер позиции, пока цена стоит на уровне p.

        pool_tvl — full-range эквивалент ликвидности пула (L = TVL / 2√P0). С эффективным TVL с блокчейна
        оценка сходится с фактом; с TVL из индексатора завышает в 3–12 раз (концентрированные LP у цены).
        Объём в токенах считается постоянным, т.е. в долларах он падает вместе с ценой (консервативно).
        """
        mine = self.active_liquidity(p)
        if not mine:
            return 0.0
        share = mine / (pool_tvl / 2 + mine)
        return fee_bps / 1e4 * vol_24h * p * share / self.size
