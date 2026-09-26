from dataclasses import replace
from math import isclose, sqrt

import pytest

from token_monitor.models import Network, PoolSnapshot, SecurityInfo, Verdict
from token_monitor.scoring import Params, classify_market, daily_yield, security_flags, stress_loss


def pool(**kw) -> PoolSnapshot:
    base = dict(
        address="0xpool", token="0xtoken", network=Network.BSC,
        fee_bps=100, liquidity_usd=100_000, vol_1h_usd=0, vol_24h_usd=0,
    )
    return PoolSnapshot(**{**base, **kw})


CLEAN = SecurityInfo(honeypot=False, buy_tax=0.0, sell_tax=0.01, top10_share=0.2,
                     dev_rug_history=False)


def test_stress_loss_matches_full_range_formula():
    assert isclose(stress_loss(0.3), 1 - sqrt(0.7))
    assert isclose(stress_loss(0.5), 0.2929, abs_tol=1e-4)


def test_daily_yield_handles_zero_liquidity():
    assert daily_yield(30, 1_000_000, 0) == 0.0
    assert isclose(daily_yield(100, 1_000_000, 100_000), 0.1)


def test_low_liquidity_is_skipped():
    r = classify_market(pool(liquidity_usd=10_000, vol_1h_usd=1e6, vol_24h_usd=1e7))
    assert r.verdict is Verdict.SKIP
    assert "liquidity" in r.reasons[0]


def test_stable_volume_is_slow():
    # 1% fee, 240k/24h на 100k ликвидности → 2.4%/день; loss(-30%) ≈ 16.3% → ~6.8 дня
    r = classify_market(pool(vol_1h_usd=10_000, vol_24h_usd=240_000))
    assert r.verdict is Verdict.SLOW
    assert r.metrics["breakeven_days"] < 7


def test_spike_is_fast_not_slow():
    # Последний час в ~19 раз выше среднего за сутки: всплеск, а не стабильный поток комиссий
    r = classify_market(pool(vol_1h_usd=200_000, vol_24h_usd=250_000))
    assert r.metrics["persistence"] > Params().max_persistence
    assert r.verdict is Verdict.FAST


def test_fading_volume_is_not_slow():
    # 24ч объём высокий, но активность затухла (последний час почти ноль)
    r = classify_market(pool(vol_1h_usd=100, vol_24h_usd=2_000_000))
    assert r.verdict is Verdict.SKIP


def test_dead_pool_is_skipped():
    r = classify_market(pool(vol_1h_usd=0, vol_24h_usd=0))
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
