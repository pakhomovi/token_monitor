from dataclasses import replace
from math import isclose, sqrt

import pytest

from token_monitor.models import Network, PoolSnapshot, SecurityInfo, Verdict
from token_monitor.scoring import (FullRangeStress, Params, RangeStress, activity_flags, classify_market,
                                   daily_yield, persistence_ratio, security_flags, stress_loss,
                                   stress_model)
from token_monitor.strategy import Shape


def pool(**kw) -> PoolSnapshot:
    base = dict(
        address="0xpool", token="0xtoken", network=Network.BSC,
        fee_bps=100, liquidity_usd=100_000, vol_1h_usd=0, vol_24h_usd=0,
    )
    return PoolSnapshot(**{**base, **kw})


FR = Params(strategy="fullrange", min_liquidity=50_000, min_fee_bps=0)   # классическая full-range модель


CLEAN = SecurityInfo(honeypot=False, buy_tax=0.0, sell_tax=0.01, top10_share=0.2,
                     dev_rug_history=False)


def test_stress_loss_matches_full_range_formula():
    assert isclose(stress_loss(0.3), 1 - sqrt(0.7))
    assert isclose(stress_loss(0.5), 0.2929, abs_tol=1e-4)


def test_daily_yield_handles_zero_liquidity():
    assert daily_yield(30, 1_000_000, 0) == 0.0
    assert isclose(daily_yield(100, 1_000_000, 100_000), 0.1)


def test_low_liquidity_is_skipped():
    r = classify_market(pool(liquidity_usd=10_000, vol_1h_usd=1e6, vol_24h_usd=1e7), FR)
    assert r.verdict is Verdict.SKIP
    assert "liquidity" in r.reasons[0]


def test_stable_volume_is_slow():
    # 1% fee, 240k/24h на 100k ликвидности → 2.4%/день; loss(-30%) ≈ 16.3% → ~6.8 дня
    r = classify_market(pool(vol_1h_usd=10_000, vol_24h_usd=240_000), FR)
    assert r.verdict is Verdict.SLOW
    assert r.metrics["breakeven_days"] < 7


def test_spike_is_fast_not_slow():
    # Последний час в ~19 раз выше среднего за сутки: всплеск, а не стабильный поток комиссий
    r = classify_market(pool(vol_1h_usd=200_000, vol_24h_usd=250_000), FR)
    assert r.metrics["persistence"] > Params().max_persistence
    assert r.verdict is Verdict.FAST


def test_fading_volume_is_not_slow():
    # 24ч объём высокий, но активность затухла (последний час почти ноль)
    r = classify_market(pool(vol_1h_usd=100, vol_24h_usd=2_000_000), FR)
    assert r.verdict is Verdict.SKIP


def test_dead_pool_is_skipped():
    r = classify_market(pool(vol_1h_usd=0, vol_24h_usd=0), FR)
    assert r.verdict is Verdict.SKIP


def test_clean_security_has_no_flags():
    assert security_flags(CLEAN) == []


@pytest.mark.parametrize("field,value,expected", [
    ("honeypot", True, "honeypot"),
    ("sell_tax", 0.10, "sell tax 10%"),
    ("buy_tax", 0.06, "buy tax 6%"),
    ("top10_share", 0.5, "top10 50%"),
    ("dev_rug_history", True, "dev rug history"),
])
def test_security_red_flags(field, value, expected):
    assert security_flags(replace(CLEAN, **{field: value})) == [expected]


def test_unknown_security_fields():
    assert "honeypot unknown" in security_flags(SecurityInfo())
    assert security_flags(SecurityInfo(), Params(strict_unknown=False)) == []


# SLOW-пул из test_stable_volume_is_slow с органической активностью
ORGANIC = dict(vol_1h_usd=10_000, vol_24h_usd=240_000, txns_24h=2_000, unique_wallets_24h=500,
               fresh_wallet_share=0.05)


def test_organic_activity_keeps_verdict():
    assert classify_market(pool(**ORGANIC), FR).verdict is Verdict.SLOW


@pytest.mark.parametrize("override,reason", [
    # Реальный кейс: $32M объёма за 15 сделок в пуле на $1.1M
    ({"txns_24h": 15}, "avg trade 16% of pool"),
    ({"unique_wallets_24h": 10}, "only 10 wallets/24h"),
    ({"fresh_wallet_share": 0.99}, "99% swaps from <1d wallets"),
])
def test_wash_volume_is_skipped(override, reason):
    r = classify_market(pool(**{**ORGANIC, **override}), FR)
    assert r.verdict is Verdict.SKIP
    assert r.reasons[0].startswith("wash:") and reason in r.reasons[0]
    assert r.metrics["breakeven_days"] < 7          # математика посчитана, отсекло именно качество объёма


def test_missing_activity_data_is_not_flagged():
    assert activity_flags(pool(vol_24h_usd=240_000)) == []


def test_dead_pool_is_not_flagged_for_few_wallets():
    assert activity_flags(pool(unique_wallets_24h=0)) == []


def test_low_liquidity_still_reports_metrics_and_would_be_verdict():
    # Кейс HADES: $21k ликвидности, 6% fee — математика проходит, отсекает только порог
    r = classify_market(pool(liquidity_usd=21_314, fee_bps=600, vol_1h_usd=1_817, vol_24h_usd=48_463), FR)
    assert r.verdict is Verdict.SKIP
    assert r.reasons[0].startswith("liquidity <") and r.reasons[1].startswith("else SLOW")
    assert isclose(r.metrics["daily_yield_24h"], 0.06 * 48_463 / 21_314)


def test_dead_pool_with_few_wallets_is_not_wash():
    r = classify_market(pool(vol_24h_usd=5, unique_wallets_24h=7), FR)
    assert not r.reasons[0].startswith("wash")


def test_zero_fee_hook_pool_is_skipped():
    r = classify_market(pool(fee_bps=0, **ORGANIC), FR)
    assert r.verdict is Verdict.SKIP and r.reasons == ["no LP fee (hook pool)"]


# --- Модель входа ниже цены: решают ¼ и ½ диапазона, дно — только метрика ---

SPOT = Params(strategy="spot", strategy_depth=0.7, strategy_size=1000, min_fee_bps=0)


def test_stress_model_factory():
    assert isinstance(stress_model(FR), FullRangeStress)
    m = stress_model(Params(strategy="bidask", strategy_depth=0.6, strategy_size=500))
    assert isinstance(m, RangeStress) and m.pos.shape is Shape.BIDASK and m.label == "bidask -60%"


def test_range_breakeven_is_worst_of_quarter_and_half():
    r = classify_market(pool(**ORGANIC), SPOT)
    m = r.metrics
    assert m["breakeven_days"] == max(m["cover_25"], m["cover_50"])
    assert m["cover_25"] < m["cover_50"] < m["cover_100"]       # чем глубже, тем дольше покрывать


def test_bottom_scenario_does_not_decide_verdict():
    # Подбираем объём так, чтобы ½ диапазона укладывалась в горизонт, а дно — нет
    model = stress_model(SPOT)
    p = next(pool(vol_1h_usd=v / 24, vol_24h_usd=v, txns_24h=2_000, unique_wallets_24h=500,
                  fresh_wallet_share=0.05)
             for v in range(1_000, 500_000, 1_000)
             if model.breakeven(pool(vol_24h_usd=v), v)[0] <= SPOT.strategy_horizon_days)
    r = classify_market(p, SPOT)
    assert r.metrics["cover_100"] > SPOT.strategy_horizon_days
    assert r.verdict is Verdict.SLOW


def test_skip_reason_names_strategy():
    r = classify_market(pool(vol_1h_usd=10, vol_24h_usd=240), SPOT)
    assert r.verdict is Verdict.SKIP and "spot -70%" in r.reasons[0]


def test_unstable_volume_reason_is_explicit():
    # Комиссий хватает по 24ч, но последний час затих: отсекает persistence, а не доходность
    r = classify_market(pool(**{**ORGANIC, "vol_1h_usd": 2_000}), SPOT)
    assert r.verdict is Verdict.SKIP and r.reasons[0].startswith("unstable volume: persistence 0.20")


def test_range_uses_its_own_horizon():
    assert stress_model(SPOT).horizon_days == SPOT.strategy_horizon_days == 2.0
    assert stress_model(FR).horizon_days == FR.slow_horizon_days


def test_persistence_uses_4h_window():
    # Кейс NOSH: последний час тихий (1h-метрика 0.55), но 4ч идут в среднесуточном темпе
    p = pool(vol_1h_usd=5_504, vol_4h_usd=40_258, vol_24h_usd=241_656)
    assert isclose(persistence_ratio(p), 40_258 * 6 / 241_656)
    assert persistence_ratio(replace(p, vol_4h_usd=None)) == 5_504 * 24 / 241_656   # fallback на 1ч
    assert persistence_ratio(pool(vol_24h_usd=0)) == 0


def test_fee_below_one_percent_is_skipped_with_would_be_verdict():
    # Объём ×10, чтобы 0.3% проходил математику и отсекал именно порог комиссии
    r = classify_market(pool(**{**ORGANIC, "fee_bps": 30, "vol_1h_usd": 100_000, "vol_24h_usd": 2_400_000}), Params())
    assert r.verdict is Verdict.SKIP and r.reasons[0] == "fee 0.3% < 1%" and r.reasons[1].startswith("else")


def test_defaults_target_small_one_percent_pools():
    p = Params()
    assert (p.min_liquidity, p.min_fee_bps) == (10_000, 100)
    assert classify_market(pool(**{**ORGANIC, "liquidity_usd": 12_000}), p).verdict is not Verdict.SKIP
