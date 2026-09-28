import httpx

from token_monitor.dexscreener import PairStats, pair_stats, parse
from token_monitor.models import Network

RAW = {"pairAddress": "0xABC", "priceUsd": "0.0159", "volume": {"h1": 5669.69, "h6": 32232.43, "h24": 214332.65},
       "liquidity": {"usd": 188792.12}, "txns": {"h24": {"buys": 815, "sells": 556}}}


def test_parse_and_persistence():
    s = parse(RAW)
    assert s == PairStats(5669.69, 32232.43, 214332.65, 188792.12, 0.0159, 815, 556)
    assert abs(s.persistence - 32232.43 * 4 / 214332.65) < 1e-12
    assert parse({"pairAddress": "0x1"}).persistence == 0


def test_groups_by_chain_and_chunks_by_30():
    urls = []

    def handler(req):
        urls.append(str(req.url))
        addrs = str(req.url).rsplit("/", 1)[1].split(",")
        return httpx.Response(200, json={"pairs": [{**RAW, "pairAddress": a} for a in addrs]})

    pools = [(f"0x{i:02X}", Network.ROBINHOOD) for i in range(31)] + [("0xBASE", Network.BASE)]
    out = pair_stats(pools, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(urls) == 3 and sum("/robinhood/" in u for u in urls) == 2
    assert "0x1e" in out and "0xbase" in out                  # ключи в нижнем регистре
