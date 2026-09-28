"""DexScreener: бесплатные метрики пулов (объём 1ч/6ч/24ч, сделки, ликвидность) без ключа."""
from dataclasses import dataclass
from typing import Iterable

import httpx

from .models import Network

URL = "https://api.dexscreener.com/latest/dex/pairs"
CHAIN_IDS = {Network.BSC: "bsc", Network.BASE: "base", Network.ROBINHOOD: "robinhood"}
MAX_PER_REQUEST = 30


@dataclass(frozen=True, slots=True)
class PairStats:
    vol_1h: float
    vol_6h: float
    vol_24h: float
    liquidity_usd: float
    price_usd: float | None
    buys_24h: int
    sells_24h: int

    @property
    def persistence(self) -> float:
        """Темп последних 6ч к среднему за сутки (у DexScreener нет окна 4ч)."""
        return self.vol_6h * 4 / self.vol_24h if self.vol_24h > 0 else 0.0


def parse(pair: dict) -> PairStats:
    vol, txns = pair.get("volume") or {}, (pair.get("txns") or {}).get("h24") or {}
    price = pair.get("priceUsd")
    return PairStats(float(vol.get("h1") or 0), float(vol.get("h6") or 0), float(vol.get("h24") or 0),
                     float((pair.get("liquidity") or {}).get("usd") or 0), float(price) if price else None,
                     int(txns.get("buys") or 0), int(txns.get("sells") or 0))


def pair_stats(pools: Iterable[tuple[str, Network]], client: httpx.Client | None = None) -> dict[str, PairStats]:
    """Адрес пула (v3) или poolId (v4) в нижнем регистре → метрики. До 30 пулов на запрос в пределах сети."""
    by_chain: dict[Network, list[str]] = {}
    for address, network in pools:
        by_chain.setdefault(network, []).append(address.lower())
    own = client is None
    client = client or httpx.Client(timeout=20)
    out: dict[str, PairStats] = {}
    try:
        for network, addresses in by_chain.items():
            for i in range(0, len(addresses), MAX_PER_REQUEST):
                chunk = addresses[i:i + MAX_PER_REQUEST]
                resp = client.get(f"{URL}/{CHAIN_IDS[network]}/{','.join(chunk)}")
                resp.raise_for_status()
                for pair in resp.json().get("pairs") or []:
                    out[pair["pairAddress"].lower()] = parse(pair)
    finally:
        if own:
            client.close()
    return out
