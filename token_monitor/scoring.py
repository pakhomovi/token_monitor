from dataclasses import dataclass, field
from math import inf, sqrt
from typing import Callable, Protocol

from .models import PoolSnapshot, SecurityInfo, Verdict
from .strategy import RangePosition, Shape


@dataclass(frozen=True, slots=True)
class Params:
    min_liquidity: float = 10_000    # позиции $300–1500: мелкий пул с большим объёмом рядом с крупным
    min_fee_bps: float = 100         # целевые пулы — 1% v3/v4, ниже комиссий не хватает
    max_tax: float = 0.05
    max_top10: float = 0.35
    stress_drop: float = 0.30        # стресс-сценарий: цена токена -30%
    slow_horizon_days: float = 7.0
    fast_horizon_hours: float = 12.0
    min_persistence: float = 0.6     # vol_4h*6 / vol_24h: активность не затухает...
    max_persistence: float = 3.0     # ...и это не разовый всплеск (памп, свежий пул)
    strict_unknown: bool = True      # отсутствующие security-данные считаются флагом
    # Wash/бот-объём: комиссии с него реальны, но это приманка и он не устойчив
    max_trade_share: float = 0.05    # средняя сделка / ликвидность пула
    min_unique_wallets: int = 50     # уникальных кошельков за 24ч
    max_fresh_wallets: float = 0.5   # доля свопов с кошельков моложе суток
    # Модель входа: spot/bidask — позиция ниже цены, fullrange — классический full-range LP
    strategy: str = "spot"
    strategy_depth: float = 0.7      # диапазон до -70%
    strategy_size: float = 1000      # размер позиции, $
    strategy_horizon_days: float = 2.0   # ¼ и ½ диапазона — мягкие сценарии, горизонт короче


@dataclass(frozen=True, slots=True)
class MarketResult:
    verdict: Verdict
    reasons: list[str]
    metrics: dict[str, float] = field(default_factory=dict)


def daily_yield(fee_bps: float, daily_volume: float, liquidity: float) -> float:
    return fee_bps / 1e4 * daily_volume / liquidity if liquidity > 0 else 0.0


def stress_loss(drop: float) -> float:
    # Full-range LP: стоимость позиции в quote-активе = sqrt(P1/P0) от входа
    return 1 - sqrt(1 - drop)


def persistence_ratio(p: PoolSnapshot) -> float:
    """Темп последних 4ч к среднему за сутки. Окно 1ч шумное: один тихий час отсекал стабильный пул.
    Без vol_4h (старые снапшоты) — откат на окно 1ч."""
    if p.vol_24h_usd <= 0:
        return 0.0
    recent = p.vol_4h_usd * 6 if p.vol_4h_usd is not None else p.vol_1h_usd * 24
    return recent / p.vol_24h_usd


def activity_flags(p: PoolSnapshot, cfg: Params = Params()) -> list[str]:
    """Признаки накрученного объёма. Отсутствующие данные флагом не считаются."""
    flags: list[str] = []
    if p.txns_24h and p.liquidity_usd > 0:
        share = p.vol_24h_usd / p.txns_24h / p.liquidity_usd
        if share > cfg.max_trade_share:
            flags.append(f"avg trade {share:.0%} of pool ({p.txns_24h} txns)")
    if p.unique_wallets_24h is not None and p.vol_24h_usd > 0 and p.unique_wallets_24h < cfg.min_unique_wallets:
        flags.append(f"only {p.unique_wallets_24h} wallets/24h")
    if p.fresh_wallet_share is not None and p.fresh_wallet_share > cfg.max_fresh_wallets:
        flags.append(f"{p.fresh_wallet_share:.0%} swaps from <1d wallets")
    return flags


class StressModel(Protocol):
    """Сколько дней комиссии должны капать, чтобы покрыть потерю позиции в стресс-сценарии."""

    label: str
    horizon_days: float

    def breakeven(self, p: PoolSnapshot, daily_volume: float) -> tuple[float, dict[str, float]]: ...


class FullRangeStress:
    def __init__(self, drop: float, horizon_days: float):
        self.loss = stress_loss(drop)
        self.label = f"full-range -{drop:.0%}"
        self.horizon_days = horizon_days

    def breakeven(self, p: PoolSnapshot, daily_volume: float) -> tuple[float, dict[str, float]]:
        y = daily_yield(p.fee_bps, daily_volume, p.liquidity_usd)
        return (self.loss / y if y > 0 else inf), {}


class RangeStress:
    """Позиция ниже цены. Решают вероятные сценарии (падение на ¼ и ½ диапазона),
    дно диапазона — маловероятно, считается только как метрика для сравнения пулов."""

    DECISIVE = (0.25, 0.5)
    REFERENCE = 1.0

    def __init__(self, position: RangePosition, horizon_days: float):
        self.pos = position
        self.label = f"{position.shape.value} -{position.depth:.0%}"
        self.horizon_days = horizon_days

    def cover_days(self, p: PoolSnapshot, daily_volume: float, fraction: float) -> float:
        price = 1 - self.pos.depth * fraction
        loss = 1 - self.pos.state(price).value / self.pos.size
        y = self.pos.daily_fee_yield(price, p.fee_bps, daily_volume, p.liquidity_usd)
        return loss / y if y > 0 else inf

    def breakeven(self, p: PoolSnapshot, daily_volume: float) -> tuple[float, dict[str, float]]:
        covers = {f: self.cover_days(p, daily_volume, f) for f in (*self.DECISIVE, self.REFERENCE)}
        extra = {f"cover_{int(f * 100)}": d for f, d in covers.items()}
        return max(covers[f] for f in self.DECISIVE), extra


def stress_model(cfg: Params) -> StressModel:
    if cfg.strategy == "fullrange":
        return FullRangeStress(cfg.stress_drop, cfg.slow_horizon_days)
    return RangeStress(RangePosition(cfg.strategy_size, cfg.strategy_depth, Shape(cfg.strategy)),
                       cfg.strategy_horizon_days)


def classify_market(p: PoolSnapshot, cfg: Params = Params(),
                    model: StressModel | None = None) -> MarketResult:
    """Этап 2 воронки: только рыночная математика, без сетевых запросов.

    Метрики считаются всегда, даже для отсеянных пулов, чтобы при ручном анализе было видно,
    что именно отсекло пул. model передаётся снаружи, чтобы не пересобирать позицию на каждый пул.
    """
    if p.fee_bps <= 0:
        # v4-пулы лаунчпадов: комиссию забирает хук, LP не получает ничего
        return MarketResult(Verdict.SKIP, ["no LP fee (hook pool)"])

    model = model or stress_model(cfg)
    persistence = persistence_ratio(p)
    be_days, extra = model.breakeven(p, p.vol_24h_usd)
    be_days_now, _ = model.breakeven(p, p.vol_1h_usd * 24)
    be_hours_now = be_days_now * 24

    metrics = {
        "daily_yield_24h": daily_yield(p.fee_bps, p.vol_24h_usd, p.liquidity_usd),
        "daily_yield_now": daily_yield(p.fee_bps, p.vol_1h_usd * 24, p.liquidity_usd),
        "persistence": persistence,
        "breakeven_days": be_days,
        "breakeven_hours_now": be_hours_now,
        **extra,
    }

    stable = cfg.min_persistence <= persistence <= cfg.max_persistence
    if stable and be_days <= model.horizon_days:
        verdict, reason = Verdict.SLOW, f"breakeven {be_days:.1f}d, persistence {persistence:.2f}"
    elif be_hours_now <= cfg.fast_horizon_hours:
        verdict, reason = Verdict.FAST, f"hot now: breakeven {be_hours_now:.1f}h"
    elif be_days <= model.horizon_days:
        # Комиссий хватает, но объём нестабилен: на него нельзя опираться весь горизонт
        return MarketResult(Verdict.SKIP, [f"unstable volume: persistence {persistence:.2f}, "
                                           f"breakeven {be_days:.1f}d"], metrics)
    else:
        return MarketResult(Verdict.SKIP, [f"fees don't cover {model.label} within horizon"], metrics)

    # Гейты применяются только к пулам, прошедшим математику: мёртвый пул — не wash
    gate = (f"fee {p.fee_bps / 100:g}% < {cfg.min_fee_bps / 100:g}%" if p.fee_bps < cfg.min_fee_bps else
            f"liquidity < ${cfg.min_liquidity:,.0f}" if p.liquidity_usd < cfg.min_liquidity else None)
    if gate:
        return MarketResult(Verdict.SKIP, [gate, f"else {verdict.value.upper()}: {reason}"], metrics)
    if wash := activity_flags(p, cfg):
        return MarketResult(Verdict.SKIP, [f"wash: {'; '.join(wash)}"], metrics)
    return MarketResult(verdict, [reason], metrics)


def security_flags(s: SecurityInfo, cfg: Params = Params()) -> list[str]:
    """Этап 3 воронки: пустой список = проверка пройдена."""
    checks: list[tuple[str, object, Callable[[object], bool], Callable[[object], str]]] = [
        ("honeypot", s.honeypot, bool, lambda _: "honeypot"),
        ("buy tax", s.buy_tax, lambda v: v > cfg.max_tax, lambda v: f"buy tax {v:.0%}"),
        ("sell tax", s.sell_tax, lambda v: v > cfg.max_tax, lambda v: f"sell tax {v:.0%}"),
        ("top10", s.top10_share, lambda v: v > cfg.max_top10, lambda v: f"top10 {v:.0%}"),
        ("dev history", s.dev_rug_history, bool, lambda _: "dev rug history"),
    ]
    flags: list[str] = []
    for name, value, is_bad, describe in checks:
        if value is None:
            if cfg.strict_unknown:
                flags.append(f"{name} unknown")
        elif is_bad(value):
            flags.append(describe(value))
    return flags
