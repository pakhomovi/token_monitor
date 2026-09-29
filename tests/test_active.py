from dataclasses import replace

from token_monitor import active
from token_monitor.models import Network, PoolSnapshot
from token_monitor.scoring import Params, classify_market

POOL = PoolSnapshot("0xpool", "0xtoken", Network.ROBINHOOD, 200, 100_000, 10_000, 240_000,
                    symbol="CAT", vol_4h_usd=40_000, exchange="Uniswap", version=4, txns_24h=2_000,
                    unique_wallets_24h=500, fresh_wallet_share=0.05,
                    target_is_token0=True, quote_symbol="USDG", quote_decimals=6)


class Reader:
    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def test_effective_tvl_is_filled_for_readable_pools(monkeypatch):
    monkeypatch.setattr(active, "ChainReader", Reader)
    monkeypatch.setattr(active, "pool_states", lambda r, pools: [(1.0, 5 * 10**10)] * len(pools))
    v2 = replace(POOL, address="0xv2", version=2)
    out = active.with_active_liquidity([POOL, v2], codex=None)
    assert out[0].effective_tvl_usd == 2 * 5 * 10**10 / 1e6       # стейбл-quote: $1 без запроса
    assert out[1].effective_tvl_usd is None                        # v2 — full-range, TVL и так точен


def test_rpc_failure_falls_back_to_tvl(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(active, "ChainReader", Reader)
    monkeypatch.setattr(active, "pool_states", boom)
    assert active.with_active_liquidity([POOL], codex=None)[0].effective_tvl_usd is None
    assert "оценка по TVL" in capsys.readouterr().err


def test_denser_active_liquidity_slows_breakeven():
    thin, dense = replace(POOL, effective_tvl_usd=50_000), replace(POOL, effective_tvl_usd=500_000)
    assert (classify_market(dense, Params()).metrics["cover_50"]
            > classify_market(thin, Params()).metrics["cover_50"])
