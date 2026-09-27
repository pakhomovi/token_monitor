from dataclasses import dataclass, field
from math import inf, sqrt
from typing import Callable

from .models import PoolSnapshot, SecurityInfo, Verdict


@dataclass(frozen=True, slots=True)
class Params:
    min_liquidity: float = 50_000
    max_tax: float = 0.05
    max_top10: float = 0.35
    stress_drop: float = 0.30        # стресс-сценарий: цена токена -30%
    slow_horizon_days: float = 7.0
    fast_horizon_hours: float = 12.0
    min_persistence: float = 0.6     # vol_1h*24 / vol_24h: активность не затухает...
    max_persistence: float = 3.0     # ...и это не разовый всплеск (памп, свежий пул)
    strict_unknown: bool = True      # отсутствующие security-данные считаются флагом
    # Wash/бот-объём: комиссии с него реальны, но это приманка и он не устойчив
    max_trade_share: float = 0.05    # средняя сделка / ликвидность пула
    min_unique_wallets: int = 50     # уникальных кошельков за 24ч
    max_fresh_wallets: float = 0.5   # доля свопов с кошельков моложе суток


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


def classify_market(p: PoolSnapshot, cfg: Params = Params()) -> MarketResult:
    """Этап 2 воронки: только рыночная математика, без сетевых запросов.

    Метрики считаются всегда, даже для отсеянных пулов, чтобы при ручном анализе было видно,
    что именно отсекло пул.
    """
    loss = stress_loss(cfg.stress_drop)
    y_24h = daily_yield(p.fee_bps, p.vol_24h_usd, p.liquidity_usd)
    y_now = daily_yield(p.fee_bps, p.vol_1h_usd * 24, p.liquidity_usd)
    persistence = p.vol_1h_usd * 24 / p.vol_24h_usd if p.vol_24h_usd > 0 else 0.0

    # Сколько времени комиссии должны капать, чтобы покрыть стресс-падение
    be_days = loss / y_24h if y_24h > 0 else inf
    be_hours_now = loss / y_now * 24 if y_now > 0 else inf

    metrics = {
        "daily_yield_24h": y_24h,
        "daily_yield_now": y_now,
        "persistence": persistence,
        "breakeven_days": be_days,
        "breakeven_hours_now": be_hours_now,
    }

    stable = cfg.min_persistence <= persistence <= cfg.max_persistence
    if stable and be_days <= cfg.slow_horizon_days:
        verdict, reason = Verdict.SLOW, f"breakeven {be_days:.1f}d, persistence {persistence:.2f}"
    elif be_hours_now <= cfg.fast_horizon_hours:
        verdict, reason = Verdict.FAST, f"hot now: breakeven {be_hours_now:.1f}h"
    else:
        return MarketResult(Verdict.SKIP, [f"fees don't cover -{cfg.stress_drop:.0%} within horizon"], metrics)

    # Гейты применяются только к пулам, прошедшим математику: мёртвый пул — не wash
    if p.liquidity_usd < cfg.min_liquidity:
        return MarketResult(Verdict.SKIP, [f"liquidity < ${cfg.min_liquidity:,.0f}",
                                           f"else {verdict.value.upper()}: {reason}"], metrics)
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
