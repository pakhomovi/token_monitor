import json
from math import isclose

import httpx
import pytest

from token_monitor.models import Network
from token_monitor.onchain import (ChainReader, Position, RateLimited, Token, amounts, calldata, decode_string,
                                   parse_position_url, selector, signed, unclaimed, v3_fee_growth_inside,
                                   v4_pool_id, v4_unpack_info, word, words)
from token_monitor.positions import PositionView


def test_selectors_match_known_abi():
    assert selector("positions(uint256)") == "99fbab88"
    assert selector("slot0()") == "3850c7bd"
    assert selector("getSlot0(bytes32)") == "c815641c"


def test_encoding_handles_negative_ticks_and_addresses():
    assert word(-1) == "f" * 64
    assert signed(int(word(-887220), 16), 24) == -887220
    assert word("0xABC") == "0" * 61 + "abc"
    assert calldata("ticks(int24)", -60).endswith("f" * 62 + "c4")


def test_decode_string_abi_and_bytes32():
    abi = "0x" + word(32) + word(3) + b"CAT".hex().ljust(64, "0")
    assert decode_string(abi) == "CAT"
    assert decode_string("0x" + b"MKR".hex().ljust(64, "0")) == "MKR"


def test_amounts_edges():
    sp = 1.0001 ** 50                            # √цены при tick=100
    a0, a1 = amounts(10**18, sp, 0, 200)
    assert a0 > 0 and a1 > 0
    assert amounts(10**18, 1.0001 ** 150, 0, 200)[0] == 0          # выше диапазона — только token1
    assert amounts(10**18, 1.0, 100, 200)[1] == 0                   # ниже — только token0


def test_fee_growth_wraps_modulo_2_256():
    assert unclaimed(2**128, 5, 2**256 - 5) == 10


def test_v3_fee_growth_inside_in_range():
    # В диапазоне: inside = global − outside_lower − outside_upper
    assert v3_fee_growth_inside(tick=0, lower=-10, upper=10, glob=100, out_lower=30, out_upper=20) == 50


def test_v4_position_info_unpack():
    lower, upper = -60, 120
    info = (0xABC << 56) | ((upper & 0xFFFFFF) << 32) | ((lower & 0xFFFFFF) << 8)
    assert v4_unpack_info(info) == (lower, upper)


def test_v4_pool_id_is_keccak_of_pool_key():
    pid = v4_pool_id("0x" + "0" * 40, "0x" + "1" * 40, 3000, 60, "0x" + "0" * 40)
    assert len(pid) == 32 and pid != v4_pool_id("0x" + "0" * 40, "0x" + "1" * 40, 500, 60, "0x" + "0" * 40)


@pytest.mark.parametrize("url,expected", [
    ("https://app.uniswap.org/positions/v3/base/6093665", (Network.BASE, 3, 6093665)),
    ("https://app.uniswap.org/positions/v4/robinhood/3318457", (Network.ROBINHOOD, 4, 3318457)),
])
def test_parse_position_url(url, expected):
    assert parse_position_url(url) == expected


def test_parse_position_url_rejects_unknown():
    with pytest.raises(ValueError):
        parse_position_url("https://app.uniswap.org/positions/v3/solana/1")


def test_batch_retries_rate_limit():
    calls, sleeps = [], []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(200, json=[{"id": 0, "error": {"code": -32016, "message": "over rate limit"}}])
        return httpx.Response(200, json=[{"id": i, "result": "0x" + word(i)} for i in range(len(json.loads(req.content)))])

    r = ChainReader(Network.BASE, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append)
    assert [words(x)[0] for x in r.batch([("0x1", "0x"), ("0x2", "0x")])] == [0, 1]
    assert sleeps == [0.5]


def test_batch_chunks_requests():
    sizes = []

    def handler(req):
        body = json.loads(req.content)
        sizes.append(len(body))
        return httpx.Response(200, json=[{"id": c["id"], "result": "0x"} for c in body])

    r = ChainReader(Network.BASE, client=httpx.Client(transport=httpx.MockTransport(handler)), max_batch=4)
    r.batch([("0x1", "0x")] * 9)
    assert sizes == [4, 4, 1]


def test_gives_up_after_retries():
    r = ChainReader(Network.BASE, client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429))),
                    retries=2, sleep=lambda _: None)
    with pytest.raises(RateLimited):
        r.batch([("0x1", "0x")])


# Позиция #6093665 (Base, WETH/Basecat 1%) — реальные значения с блокчейна
WETH = Token("0x4200000000000000000000000000000000000006", "WETH", 18)
CAT = Token("0xb2000000000000000000004c27f6523082f41d01", "Basecat", 18)
REAL = Position(Network.BASE, 3, 6093665, "0xf794", WETH, CAT, 100.0, 118400, 127000, 120228,
                1.0001 ** (120228 / 2), 0, 1, 0, 0)


def test_view_orients_to_target_token():
    v = PositionView(REAL)
    assert v.target.symbol == "Basecat" and v.quote.symbol == "WETH"
    lo, hi = v.range
    assert isclose(v.depth, 1 - lo / hi) and isclose(v.depth, 1 - 1.0001 ** -8600, rel_tol=1e-9)
    assert -0.2 < v.from_top < -0.15                  # цена на ~17% ниже верхней границы


def test_view_pnl_from_top_matches_range_math():
    from dataclasses import replace
    v = PositionView(replace(REAL, liquidity=10**20))
    assert v.value_at_top > v.value                   # цена в диапазоне ниже входа → просадка
    assert -0.05 < v.pnl_from_top < 0
    assert isclose(v.value, v.holdings[0] * v.price + v.holdings[1])
