import json

import httpx
import pytest

from token_monitor.models import Network, Verdict
from token_monitor.pipeline import screen
from token_monitor.screener import CodexError, CodexScreener, ScreenerQuery, parse_pair


def row(**kw):
    base = {
        "pair": {"address": "0xPOOL", "networkId": 56, "token0": "0xTOKEN", "token1": "0xWBNB"},
        "token0": {"symbol": "CAKE"}, "token1": {"symbol": "WBNB"},
        "exchange": {"name": "PancakeSwap v3"},
        "quoteToken": "token1", "liquidity": "100000", "poolFeeBps": 100,
        "volumeUSD1": "10000", "volumeUSD24": "240000",
    }
    return {**base, **kw}


def ok(rows):
    return httpx.Response(200, json={"data": {"filterPairs": {"results": rows}}})


def make(handler, sleeps=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CodexScreener("key", client=client, sleep=(sleeps.append if sleeps is not None else lambda _: None))


Q = ScreenerQuery(networks=(Network.BSC, Network.ROBINHOOD), limit=10)


def test_parse_picks_non_quote_token_and_normalizes():
    p = parse_pair(row())
    assert (p.address, p.token, p.symbol, p.network) == ("0xpool", "0xtoken", "CAKE", Network.BSC)
    assert (p.fee_bps, p.liquidity_usd, p.vol_1h_usd, p.vol_24h_usd) == (100, 100_000, 10_000, 240_000)
    # Базовый актив по символу приоритетнее quoteToken
    assert parse_pair(row(quoteToken="token0")).token == "0xtoken"
    # Если базового нет — решает quoteToken
    memes = {"token0": {"symbol": "CAKE"}, "token1": {"symbol": "PEPE"}}
    assert parse_pair(row(quoteToken="token0", **memes)).token == "0xwbnb"


def test_major_pairs_are_dropped_unless_analyzed():
    majors = {"token0": {"symbol": "usdt"}, "token1": {"symbol": "WBNB"}}
    assert parse_pair(row(**majors)) is None
    assert parse_pair(row(**majors), target="0xWBNB").token == "0xwbnb"


def test_parse_vol_4h():
    assert parse_pair(row(volumeUSD4="40000")).vol_4h_usd == 40_000
    assert parse_pair(row()).vol_4h_usd is None


@pytest.mark.parametrize("override", [
    {"poolFeeBps": None},
    {"liquidity": None},
    {"pair": {"address": "0x1", "networkId": 1, "token0": "a", "token1": "b"}},  # не наша сеть
    {"pair": None},
])
def test_parse_drops_unusable_rows(override):
    assert parse_pair(row(**override)) is None


def test_missing_volume_is_zero():
    p = parse_pair(row(volumeUSD1=None, volumeUSD24="n/a"))
    assert p.vol_1h_usd == 0 and p.vol_24h_usd == 0


def test_query_variables():
    v = ScreenerQuery(networks=(Network.BASE,), min_liquidity=1e5, min_volume_24h=5e4,
                      min_fee_bps=10).variables()
    assert v["filters"] == {"network": [8453], "liquidity": {"gte": 1e5}, "volumeUSD24": {"gte": 5e4},
                            "poolFeeBps": {"gte": 10}, "potentialScam": False}
    assert set(ScreenerQuery(networks=(Network.BSC,)).variables()["filters"]) == {
        "network", "liquidity", "potentialScam"}
    assert v["rankings"] == [{"attribute": "volumeUSD24", "direction": "DESC"}]


@pytest.mark.parametrize("kw", [{"limit": 0}, {"limit": 201}, {"rank_by": "price; drop"}])
def test_query_validation(kw):
    with pytest.raises(ValueError):
        ScreenerQuery(networks=(Network.BSC,), **kw)


def test_fetch_single_request_with_auth_header():
    seen = []

    def handler(req):
        seen.append(req)
        return ok([row(), row(poolFeeBps=None), None])

    pools = make(handler).fetch(Q)
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == "key"
    assert json.loads(seen[0].content)["variables"]["filters"]["network"] == [56, 4663]
    assert [p.address for p in pools] == ["0xpool"]


def test_retries_on_over_capacity_with_server_hint():
    responses = iter([
        httpx.Response(200, json={"errors": [{"message": "busy", "extensions": {"retryAfterSeconds": 30}}]}),
        httpx.Response(429, headers={"Retry-After": "2"}),
        ok([row()]),
    ])
    sleeps = []
    assert len(make(lambda _: next(responses), sleeps).fetch(Q)) == 1
    assert sleeps == [30, 2]


def test_non_retryable_graphql_error():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"errors": [{"message": "bad field"}]})

    with pytest.raises(CodexError, match="bad field"):
        make(handler).fetch(Q)
    assert len(calls) == 1


def test_auth_error_is_not_retried():
    with pytest.raises(CodexError, match="401"):
        make(lambda _: httpx.Response(401)).fetch(Q)


def test_gives_up_after_max_retries():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503)

    with pytest.raises(CodexError, match="503"):
        make(handler).fetch(Q)
    assert len(calls) == 4


def test_empty_key_rejected():
    with pytest.raises(CodexError):
        CodexScreener("")


def test_screen_orders_fast_then_slow_and_drops_skip():
    slow = parse_pair(row())                                                            # ~6.8 дня
    fast = parse_pair(row(volumeUSD1="200000", volumeUSD24="250000", pair={**row()["pair"], "address": "0xF"}))
    dead = parse_pair(row(volumeUSD1="0", volumeUSD24="0", pair={**row()["pair"], "address": "0xD"}))
    out = screen([slow, dead, fast])
    assert [s.market.verdict for s in out] == [Verdict.FAST, Verdict.SLOW]
    assert len(screen([slow, dead, fast], keep_skipped=True)) == 3


def test_query_token_filter():
    v = ScreenerQuery(networks=(Network.BSC,), tokens=("0xabc",)).variables()
    assert v["filters"]["tokenAddress"] == ["0xabc"]


def test_parse_target_overrides_quote_side():
    # Анализируем WBNB, хотя по quoteToken целевым был бы token0
    assert parse_pair(row(), target="0xWBNB").token == "0xwbnb"
    assert parse_pair(row(), target="0xOTHER").token == "0xtoken"   # чужой адрес → обычная логика


def test_analyze_cli(monkeypatch, capsys):
    from token_monitor import __main__ as cli

    def fake_fetch_rows(self, query):
        assert query.tokens == ("0xtoken",) and not query.exclude_scam
        return [{**row(), "riskCoverage": "ANALYZED", "riskVerdict": "CAUTION", "riskScore": 0.4, "riskReasons": ["TAX_SELL_HIGH"]}]

    monkeypatch.setenv("CODEX_API_KEY", "k")
    monkeypatch.setattr(CodexScreener, "fetch_rows", fake_fetch_rows)
    assert cli.main(["analyze", "0xTOKEN", "--network", "bsc"]) == 0
    out = capsys.readouterr().out
    assert "CAKE 0xtoken: 1 pools" in out
    assert "CAUTION" in out and "TAX_SELL_HIGH" in out
    assert "SLOW" in out


def test_parse_activity_fields():
    p = parse_pair(row(txnCount24=15, uniqueTransactions24="10", swapPct1dOldWallet="0.07"))
    assert (p.txns_24h, p.unique_wallets_24h, p.fresh_wallet_share) == (15, 10, 0.07)


def test_analyze_marks_unanalyzed_risk(monkeypatch, capsys):
    from token_monitor import __main__ as cli

    monkeypatch.setenv("CODEX_API_KEY", "k")
    monkeypatch.setattr(CodexScreener, "fetch_rows",
                        lambda self, q: [{**row(), "riskCoverage": "NOT_ANALYZED", "riskVerdict": "NEUTRAL"}])
    assert cli.main(["analyze", "0xTOKEN"]) == 0
    out = capsys.readouterr().out
    assert "not analyzed (NOT_ANALYZED)" in out and "NEUTRAL" not in out


@pytest.mark.parametrize("override,expected", [
    ({"poolFeeBps": 200, "pair": {**row()["pair"], "fee": 999}}, 200),           # poolFeeBps приоритетнее
    ({"poolFeeBps": None, "pair": {**row()["pair"], "fee": 20000}}, 200),        # v3/v4: миллионные доли
    ({"poolFeeBps": None, "pair": {**row()["pair"], "fee": 0}}, 0),              # v4 hook-пул
    ({"poolFeeBps": None, "pair": {**row()["pair"], "fee": 0x800000}}, None),    # динамическая комиссия
    ({"poolFeeBps": None, "dynamicFee": True, "pair": {**row()["pair"], "fee": 3000}}, None),
    ({"poolFeeBps": None, "exchange": {"address": "0xCA143CE32FE78F1F7019D7D551A6402FC5350C73"}}, 25),
    ({"poolFeeBps": None, "exchange": {"address": "0xunknownfork"}}, None),
])
def test_resolve_fee_bps(override, expected):
    from token_monitor.screener import resolve_fee_bps
    assert resolve_fee_bps(row(**override)) == expected


def test_cli_strategy_override(monkeypatch, capsys):
    from token_monitor import __main__ as cli

    monkeypatch.setenv("CODEX_API_KEY", "k")
    monkeypatch.setattr(CodexScreener, "fetch_rows", lambda self, q: [{**row(), "riskCoverage": "ANALYZED"}])
    assert cli.main(["analyze", "0xTOKEN", "--strategy", "bidask", "--depth", "0.6", "--scenarios", "1"]) == 0
    out = capsys.readouterr().out
    assert "cover ¼" in out and "bidask -60% $1,000" in out
    for scenario in ("¼ range", "½ range", "bottom", "below"):
        assert scenario in out


@pytest.mark.parametrize("pair_extra,exchange,expected", [
    ({"protocol": "UniswapV3"}, {}, 3),
    ({"protocol": "UniswapV4"}, {}, 4),
    ({"protocol": "PancakeSwapInfinityCL"}, {}, 4),
    ({"protocol": None}, {"exchangeVersion": "2"}, 2),
    ({"protocol": "PonsV2"}, {}, 2),
    ({}, {}, None),
])
def test_protocol_version(pair_extra, exchange, expected):
    from token_monitor.screener import protocol_version
    assert protocol_version(row(pair={**row()["pair"], **pair_extra}, exchange=exchange)) == expected
