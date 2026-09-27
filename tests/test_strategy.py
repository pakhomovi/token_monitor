from math import isclose, sqrt

import pytest

from token_monitor.strategy import RangePosition, Shape


@pytest.fixture(params=[Shape.SPOT, Shape.BIDASK])
def pos(request):
    return RangePosition(1000, 0.7, request.param)


def test_above_range_is_all_quote(pos):
    s = pos.state(1.0)
    assert isclose(s.value, 1000) and s.token_share == 0 and s.avg_entry == 0
    assert isclose(pos.state(1.5).value, 1000)          # рост выше диапазона: остаёмся в quote


def test_below_range_is_all_token(pos):
    s = pos.state(0.2)
    assert isclose(s.token_share, 1)
    assert pos.bottom < s.avg_entry < 1


def test_single_bin_matches_closed_form():
    # Один бин [1−d, 1]: на нижней границе стоимость = √(1−d), средняя цена = √(pa·pb)
    s = RangePosition(1, 0.6, bins=1).state(0.4)
    assert isclose(s.value, sqrt(0.4))
    assert isclose(s.avg_entry, sqrt(0.4))


def test_path_independent_rebound(pos):
    # Возврат к цене входа возвращает весь капитал в quote — потери нет, остаются комиссии
    assert isclose(pos.state(1.0).value, 1000)
    assert pos.state(0.65).value > pos.state(0.3).value


def test_bidask_buys_lower_than_spot():
    spot, bidask = RangePosition(1000, 0.7, Shape.SPOT), RangePosition(1000, 0.7, Shape.BIDASK)
    assert bidask.state(0.3).avg_entry < spot.state(0.3).avg_entry
    # На небольшом падении bid-ask почти не задействован: меньше выкупа и меньше комиссий
    assert bidask.state(0.9).token_share < spot.state(0.9).token_share
    assert bidask.daily_fee_yield(0.9, 200, 1e5, 1e5) < spot.daily_fee_yield(0.9, 200, 1e5, 1e5)
    assert bidask.daily_fee_yield(0.35, 200, 1e5, 1e5) > spot.daily_fee_yield(0.35, 200, 1e5, 1e5)


def test_fees_only_in_range(pos):
    assert pos.daily_fee_yield(1.2, 200, 1e5, 1e5) == 0
    assert pos.daily_fee_yield(0.2, 200, 1e5, 1e5) == 0
    assert pos.daily_fee_yield(0.8, 200, 1e5, 1e5) > 0


def test_fee_share_shrinks_in_deeper_pool():
    p = RangePosition(1000, 0.7)
    assert p.daily_fee_yield(0.8, 200, 1e5, 1e6) < p.daily_fee_yield(0.8, 200, 1e5, 1e5)


@pytest.mark.parametrize("kw", [{"size": 0}, {"depth": 1}, {"depth": 0}, {"bins": 0}])
def test_validation(kw):
    with pytest.raises(ValueError):
        RangePosition(**{"size": 1000, "depth": 0.7, **kw})


def test_usd_volume_scales_with_price():
    # Одинаковая доля в пуле на разных ценах → комиссии пропорциональны цене
    p = RangePosition(1000, 0.7, bins=1)
    assert isclose(p.daily_fee_yield(0.5, 200, 1e5, 1e5) / p.daily_fee_yield(1.0, 200, 1e5, 1e5), 0.5)
