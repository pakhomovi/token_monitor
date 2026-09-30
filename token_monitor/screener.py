"""Этап 1 воронки: один запрос к Codex `filterPairs` → top-N пулов."""
import logging
import re
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .models import Network, PoolSnapshot

log = logging.getLogger(__name__)

CODEX_URL = "https://graph.codex.io/graphql"

# https://docs.codex.io/networks.md
NETWORK_IDS: dict[Network, int] = {Network.BSC: 56, Network.BASE: 8453, Network.ROBINHOOD: 4663}
_NETWORK_BY_ID = {v: k for k, v in NETWORK_IDS.items()}

RANK_ATTRIBUTES = frozenset({"volumeUSD1", "volumeUSD4", "volumeUSD12", "volumeUSD24", "liquidity",
                             "trendingScore1", "trendingScore24", "poolFeeBps"})

_RESULT_FIELDS = """
      pair { address networkId token0 token1 fee protocol }
      token0 { symbol decimals }
      token1 { symbol decimals }
      exchange { name address exchangeVersion }
      quoteToken
      liquidity
      volumeUSD1
      volumeUSD4
      volumeUSD24
      poolFeeBps
      dynamicFee
      txnCount24
      uniqueTransactions24
      swapPct1dOldWallet
      riskCoverage
      riskVerdict
      riskScore
      riskReasons
"""

QUERY = """
query Screen($filters: PairFilters, $rankings: [PairRanking], $limit: Int) {
  filterPairs(filters: $filters, rankings: $rankings, limit: $limit) {
    results {""" + _RESULT_FIELDS + """    }
  }
}
"""

PAIRS_QUERY = """
query Pairs($pairs: [String], $limit: Int) {
  filterPairs(pairs: $pairs, limit: $limit) {
    results {""" + _RESULT_FIELDS + """    }
  }
}
"""


class CodexError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScreenerQuery:
    networks: tuple[Network, ...]
    limit: int = 50                  # filterPairs отдаёт максимум 200 за запрос
    min_liquidity: float = 10_000
    min_volume_24h: float = 0
    min_fee_bps: float = 0           # по poolFeeBps: без него top-N забивают 1bp мажор-пулы
    rank_by: str = "volumeUSD24"
    exclude_scam: bool = True        # бесплатный пре-фильтр Codex, полноценный security — этап 3
    tokens: tuple[str, ...] = ()     # анализ конкретных токенов вместо общего скрининга

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 200:
            raise ValueError("limit must be in [1, 200]")
        if self.rank_by not in RANK_ATTRIBUTES:
            raise ValueError(f"rank_by must be one of {sorted(RANK_ATTRIBUTES)}")

    def variables(self) -> dict[str, Any]:
        filters: dict[str, Any] = {
            "network": [NETWORK_IDS[n] for n in self.networks],
            "liquidity": {"gte": self.min_liquidity},
        }
        if self.min_volume_24h > 0:
            filters["volumeUSD24"] = {"gte": self.min_volume_24h}
        if self.min_fee_bps > 0:
            filters["poolFeeBps"] = {"gte": self.min_fee_bps}
        if self.exclude_scam:
            filters["potentialScam"] = False
        if self.tokens:
            filters["tokenAddress"] = list(self.tokens)
        return {
            "filters": filters,
            "rankings": [{"attribute": self.rank_by, "direction": "DESC"}],
            "limit": self.limit,
        }


def _num(v: Any) -> float | None:
    # Codex отдаёт денежные значения строками
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


# Базовые активы: пул мажор/стейбл не интересен для ресерча, а в паре с мемом целевой — мем.
# По символу, т.к. адреса на Robinhood не зафиксированы; подделка символа лишь исключит пул
BASE_SYMBOLS = frozenset(s.lower() for s in (
    "WETH", "ETH", "WBNB", "BNB", "WBTC", "BTCB", "cbBTC", "BTC", "cbETH", "wstETH", "weETH",
    "USDT", "USDC", "USDbC", "USDG", "USD1", "USDe", "DAI", "BUSD", "FDUSD", "TUSD", "PYUSD", "lisUSD",
))


STABLE_SYMBOLS = frozenset(s.lower() for s in (
    "USDT", "USDC", "USDbC", "USDG", "USD1", "USDe", "DAI", "BUSD", "FDUSD", "TUSD", "PYUSD", "lisUSD",
))


def is_stable(symbol: str | None) -> bool:
    return (symbol or "").lower() in STABLE_SYMBOLS


def is_base(symbol: str | None) -> bool:
    return (symbol or "").lower() in BASE_SYMBOLS


# v2-фабрики не отдают fee: комиссия зашита в контракт. Неизвестный форк не угадываем
V2_FEE_BPS: dict[str, float] = {
    "0xca143ce32fe78f1f7019d7d551a6402fc5350c73": 25,   # PancakeSwap v2 (BSC)
    "0x8909dc15e40173ff4699343b6eb8132c65e18ec6": 30,   # Uniswap v2 (BSC, Base)
}
_V4_DYNAMIC_FEE_FLAG = 0x800000


def resolve_fee_bps(row: dict[str, Any]) -> float | None:
    """poolFeeBps → pair.fee (v3/v4, миллионные доли) → известная v2-фабрика."""
    if (bps := _num(row.get("poolFeeBps"))) is not None:
        return bps
    exchange = row.get("exchange") or {}
    raw = _num((row.get("pair") or {}).get("fee"))
    if raw is not None:
        # Динамическая комиссия v4: в pair.fee лежит флаг, а не ставка
        if row.get("dynamicFee") or raw == _V4_DYNAMIC_FEE_FLAG:
            return None
        return raw / 100
    return V2_FEE_BPS.get(str(exchange.get("address")).lower())


def protocol_version(row: dict[str, Any]) -> int | None:
    """UniswapV3 → 3, UniswapV4 → 4; PancakeSwap Infinity — v4-архитектура. Иначе exchangeVersion."""
    protocol = str((row.get("pair") or {}).get("protocol") or "")
    if "infinity" in protocol.lower():
        return 4
    if m := re.search(r"V(\d)$", protocol):
        return int(m[1])
    return _int((row.get("exchange") or {}).get("exchangeVersion"))


def parse_pair(row: dict[str, Any], target: str | None = None) -> PoolSnapshot | None:
    """None для строк, по которым нельзя посчитать доходность (нет комиссии, чужая сеть и т.п.).

    target: адрес анализируемого токена — перекрывает выбор по quoteToken.
    """
    pair = row.get("pair") or {}
    network = _NETWORK_BY_ID.get(pair.get("networkId"))
    fee = resolve_fee_bps(row)
    liquidity = _num(row.get("liquidity"))
    if not (network and pair.get("address") and fee is not None and liquidity is not None):
        return None

    sym0, sym1 = ((row.get(t) or {}).get("symbol") for t in ("token0", "token1"))
    if target and target.lower() in (str(pair.get("token0")).lower(), str(pair.get("token1")).lower()):
        side = "token0" if str(pair["token0"]).lower() == target.lower() else "token1"
    elif is_base(sym0) and is_base(sym1):
        return None                      # мажор/стейбл пара: не кандидат
    elif is_base(sym0) != is_base(sym1):
        side = "token1" if is_base(sym0) else "token0"
    else:
        # Целевой токен — не quote-сторона пары
        side = "token0" if row.get("quoteToken") == "token1" else "token1"
    quote = "token1" if side == "token0" else "token0"
    return PoolSnapshot(
        address=pair["address"].lower(),
        token=pair[side].lower(),
        network=network,
        fee_bps=fee,
        liquidity_usd=liquidity,
        vol_1h_usd=_num(row.get("volumeUSD1")) or 0.0,
        vol_24h_usd=_num(row.get("volumeUSD24")) or 0.0,
        vol_4h_usd=_num(row.get("volumeUSD4")),
        version=protocol_version(row),
        symbol=(row.get(side) or {}).get("symbol") or "",
        exchange=(row.get("exchange") or {}).get("name") or "",
        txns_24h=_int(row.get("txnCount24")),
        unique_wallets_24h=_int(row.get("uniqueTransactions24")),
        fresh_wallet_share=_num(row.get("swapPct1dOldWallet")),
        target_is_token0=side == "token0",
        quote_symbol=(row.get(quote) or {}).get("symbol") or "",
        quote_decimals=_int((row.get(quote) or {}).get("decimals")),
    )


class CodexScreener:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None, url: str = CODEX_URL,
                 max_retries: int = 3, max_wait: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise CodexError("CODEX_API_KEY is not set")
        self._client = client or httpx.Client(timeout=30)
        self._headers = {"Authorization": api_key}   # без "Bearer": так требует Codex
        self._url = url
        self._max_retries = max_retries
        self._max_wait = max_wait
        self._sleep = sleep

    def __enter__(self) -> "CodexScreener":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch_rows(self, query: ScreenerQuery) -> list[dict[str, Any]]:
        data = self._request({"query": QUERY, "variables": query.variables()})
        return [r for r in (data.get("filterPairs") or {}).get("results") or [] if r]

    def fetch(self, query: ScreenerQuery) -> list[PoolSnapshot]:
        rows = self.fetch_rows(query)
        pools = [p for row in rows if (p := parse_pair(row))]
        if len(pools) < len(rows):
            log.info("screener: dropped %d of %d rows without fee/liquidity", len(rows) - len(pools), len(rows))
        return pools

    def prices(self, inputs: list[tuple[str, Network, int | None]]) -> list[float | None]:
        """USD-цены одним запросом, в порядке inputs; timestamp у каждого свой (None — текущая)."""
        if not inputs:
            return []
        payload = [{"address": a, "networkId": NETWORK_IDS[n], **({"timestamp": ts} if ts else {})}
                   for a, n, ts in inputs]
        data = self._request({"query": "query($i:[GetPriceInput]){getTokenPrices(inputs:$i){priceUsd}}",
                              "variables": {"i": payload}})
        out = data.get("getTokenPrices") or []
        return [(p or {}).get("priceUsd") for p in out] + [None] * (len(inputs) - len(out))

    def pairs(self, pools: list[tuple[str, Network]]) -> list[dict[str, Any]]:
        """Метрики конкретных пулов (v3 — адрес, v4 — poolId)."""
        data = self._request({"query": PAIRS_QUERY,
                              "variables": {"pairs": [f"{a}:{NETWORK_IDS[n]}" for a, n in pools], "limit": len(pools)}})
        return [r for r in (data.get("filterPairs") or {}).get("results") or [] if r]

    def token_stats(self, tokens: list[tuple[str, Network]]) -> list[dict[str, Any]]:
        """Метрики токенов одним запросом: адрес, сеть, символ, создание, капитализация, холдеры, лаунчпад."""
        if not tokens:
            return []
        q = """query($t:[String], $n:Int){filterTokens(tokens:$t, limit:$n){results{
            createdAt marketCap holders token{ address symbol networkId launchpad{ migrated } } }}}"""
        out: list[dict[str, Any]] = []
        for i in range(0, len(tokens), 200):
            chunk = tokens[i:i + 200]
            data = self._request({"query": q, "variables": {"t": [f"{a}:{NETWORK_IDS[n]}" for a, n in chunk],
                                                            "n": len(chunk)}})
            for r in (data.get("filterTokens") or {}).get("results") or []:
                t = r.get("token") or {}
                out.append({"address": t.get("address", "").lower(), "symbol": t.get("symbol") or "",
                            "network": _NETWORK_BY_ID.get(t.get("networkId")), "created_at": r.get("createdAt"),
                            "marketCap": r.get("marketCap"), "holders": r.get("holders"),
                            "migrated": (t.get("launchpad") or {}).get("migrated")})
        return out

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            last = attempt == self._max_retries
            try:
                resp = self._client.post(self._url, json=payload, headers=self._headers)
            except httpx.TransportError as e:
                if last:
                    raise CodexError(f"transport error: {e}") from e
                self._backoff(attempt, None)
                continue

            if resp.status_code in (401, 403):
                raise CodexError(f"HTTP {resp.status_code}: check CODEX_API_KEY / plan")
            if resp.status_code == 429 or resp.status_code >= 500:
                if last:
                    raise CodexError(f"HTTP {resp.status_code} after {attempt + 1} attempts")
                self._backoff(attempt, _num(resp.headers.get("Retry-After")))
                continue
            resp.raise_for_status()

            body = resp.json()
            errors = body.get("errors") or []
            if not errors:
                return body.get("data") or {}
            # GraphQL-ошибки приходят с HTTP 200; ретраим только те, где Codex указал паузу
            hints = (_num((e.get("extensions") or {}).get("retryAfterSeconds")) for e in errors)
            retry_after = next((w for w in hints if w is not None), None)
            if retry_after is None or last:
                raise CodexError("; ".join(e.get("message", "unknown error") for e in errors))
            self._backoff(attempt, retry_after)
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        # Серверная подсказка приоритетнее; иначе экспонента с full jitter
        delay = retry_after if retry_after is not None else random.uniform(0, 2 ** attempt)
        self._sleep(min(delay, self._max_wait))
