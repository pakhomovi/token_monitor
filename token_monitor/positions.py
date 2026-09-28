"""Разбор LP-позиции в терминах стратегии: цена целевого токена в quote, диапазон, состав, комиссии."""
from dataclasses import dataclass

from .models import Network
from .onchain import Position, amounts, sqrt_price_at_tick
from .screener import is_base, is_stable

# Цена ETH/BNB одинакова во всех сетях: одна эталонная пара на всех (нативный ETH адреса не имеет)
_QUOTE_REFERENCE = {
    "eth": ("0x4200000000000000000000000000000000000006", Network.BASE),
    "weth": ("0x4200000000000000000000000000000000000006", Network.BASE),
    "bnb": ("0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", Network.BSC),
    "wbnb": ("0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", Network.BSC),
}


def quote_reference(symbol: str) -> tuple[str, Network] | None:
    """Откуда брать $-цену quote-актива; None — стейбл (= $1) или неизвестный актив."""
    return None if is_stable(symbol) else _QUOTE_REFERENCE.get(symbol.lower())


@dataclass(frozen=True, slots=True)
class PositionView:
    pos: Position

    @property
    def target_is_token0(self) -> bool:
        # Целевой — не базовый актив; если оба не базовые, считаем quote token1 (как в интерфейсе Uniswap)
        return not is_base(self.pos.token0.symbol) or is_base(self.pos.token1.symbol)

    @property
    def target(self):
        return self.pos.token0 if self.target_is_token0 else self.pos.token1

    @property
    def quote(self):
        return self.pos.token1 if self.target_is_token0 else self.pos.token0

    def _target_price(self, raw_price: float) -> float:
        return raw_price if self.target_is_token0 else 1 / raw_price

    @property
    def price(self) -> float:
        """Цена целевого токена в quote."""
        return self._target_price(self.pos.price)

    @property
    def range(self) -> tuple[float, float]:
        a, b = (self._target_price(self.pos.price_at_tick(t)) for t in (self.pos.tick_lower, self.pos.tick_upper))
        return min(a, b), max(a, b)

    @property
    def depth(self) -> float:
        lo, hi = self.range
        return 1 - lo / hi

    @property
    def from_top(self) -> float:
        """Где цена относительно верхней границы: −0.17 = на 17% ниже, >0 — выше диапазона."""
        return self.price / self.range[1] - 1

    def _split(self, a0: float, a1: float) -> tuple[float, float]:
        return (a0, a1) if self.target_is_token0 else (a1, a0)

    @property
    def holdings(self) -> tuple[float, float]:
        """(целевой токен, quote) в позиции."""
        return self._split(*self.pos.amounts)

    @property
    def fees(self) -> tuple[float, float]:
        return self._split(*self.pos.fees)

    def split_raw(self, amount0: int, amount1: int) -> tuple[float, float]:
        """Сырые суммы token0/token1 → (целевой токен, quote) с учётом decimals."""
        return self._split(amount0 / 10 ** self.pos.token0.decimals, amount1 / 10 ** self.pos.token1.decimals)

    @property
    def value(self) -> float:
        """Стоимость позиции в quote без комиссий."""
        t, q = self.holdings
        return t * self.price + q

    @property
    def fees_value(self) -> float:
        t, q = self.fees
        return t * self.price + q

    @property
    def token_share(self) -> float:
        return self.holdings[0] * self.price / self.value if self.value else 0.0

    @property
    def value_at_top(self) -> float:
        """Стоимость на верхней границе (вся позиция в quote) — капитал входа для диапазона ниже цены."""
        p = self.pos
        top_tick = p.tick_upper if self.target_is_token0 else p.tick_lower
        a0, a1 = amounts(p.liquidity, sqrt_price_at_tick(top_tick), p.tick_lower, p.tick_upper)
        _, q = self._split(a0 / 10 ** p.token0.decimals, a1 / 10 ** p.token1.decimals)
        return q

    @property
    def pnl_from_top(self) -> float:
        """PnL без комиссий, если вход был на верхней границе (типично для позиции ниже цены)."""
        return self.value / self.value_at_top - 1 if self.value_at_top else 0.0
