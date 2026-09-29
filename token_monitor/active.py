"""Активная ликвидность пулов с блокчейна вместо TVL индексатора.

TVL из Codex/DexScreener — вся ликвидность пула, включая далёкую от цены. Комиссии делятся по активной
ликвидности у текущей цены, а концентрированные LP делают её в разы плотнее: на реальных позициях оценка по
TVL завышала комиссии в 3–12 раз. Здесь для каждого пула считается эффективный TVL (см. onchain.effective_tvl).
"""
import logging
import sys
from dataclasses import replace
from typing import Sequence

from .models import Network, PoolSnapshot
from .onchain import CHAINS, ChainReader, effective_tvl, pool_states
from .positions import quote_reference
from .screener import CodexError, CodexScreener, is_stable

log = logging.getLogger(__name__)


def _readable(p: PoolSnapshot) -> bool:
    # v2 — full-range по определению (TVL и так точен); v4 читаем только через StateView Uniswap
    return (p.network in CHAINS and p.fee_bps > 0 and p.quote_decimals is not None and p.target_is_token0 is not None
            and (p.version == 3 or (p.version == 4 and p.exchange.lower().startswith("uniswap"))))


def quote_prices(pools: Sequence[PoolSnapshot], codex: CodexScreener | None) -> dict[str, float]:
    """$-цена quote-актива по символу: стейблы = 1, ETH/BNB — один запрос к Codex на всех."""
    out = {p.quote_symbol: 1.0 for p in pools if is_stable(p.quote_symbol)}
    refs = list(dict.fromkeys(r for p in pools if (r := quote_reference(p.quote_symbol))))
    if refs and codex:
        try:
            ref_usd = dict(zip(refs, codex.prices([(a, n, None) for a, n in refs])))
            out |= {p.quote_symbol: u for p in pools
                    if (r := quote_reference(p.quote_symbol)) and (u := ref_usd.get(r))}
        except CodexError as e:
            log.warning("quote prices: %s", e)
    return out


def with_active_liquidity(pools: Sequence[PoolSnapshot], codex: CodexScreener | None) -> list[PoolSnapshot]:
    """Проставляет effective_tvl_usd тем пулам, что удалось прочитать; остальные остаются на TVL."""
    q_usd = quote_prices([p for p in pools if _readable(p)], codex)
    todo = [p for p in pools if _readable(p) and p.quote_symbol in q_usd]
    updated: dict[tuple[Network, str], PoolSnapshot] = {}
    for network in dict.fromkeys(p.network for p in todo):
        batch = [p for p in todo if p.network is network]
        try:
            with ChainReader(network) as reader:
                states = pool_states(reader, [(p.version, p.address) for p in batch])
        except Exception as e:                      # RPC недоступен — работаем на TVL, но громко
            print(f"{network.value}: активная ликвидность недоступна ({e}), оценка по TVL", file=sys.stderr)
            continue
        for p, (sqrt_price, liquidity) in zip(batch, states):
            if liquidity and sqrt_price:
                eff = effective_tvl(sqrt_price, liquidity, p.target_is_token0, p.quote_decimals,
                                    q_usd[p.quote_symbol])
                updated[(p.network, p.address)] = replace(p, effective_tvl_usd=eff)
    return [updated.get((p.network, p.address), p) for p in pools]
