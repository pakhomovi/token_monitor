"""Этап 1 воронки: один запрос к Codex `filterPairs` → top-N пулов."""
import logging
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

QUERY = """
query Screen($filters: PairFilters, $rankings: [PairRanking], $limit: Int) {
  filterPairs(filters: $filters, rankings: $rankings, limit: $limit) {
    results {
      pair { address networkId token0 token1 }
      token0 { symbol }
      token1 { symbol }
      exchange { name }
      quoteToken
      liquidity
      volumeUSD1
      volumeUSD24
      poolFeeBps
    }
  }
}
"""


class CodexError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScreenerQuery:
    networks: tuple[Network, ...]
    limit: int = 50                  # filterPairs отдаёт максимум 200 за запрос
    min_liquidity: float = 50_000
    min_volume_24h: float = 0
    min_fee_bps: float = 0           # отсекает 1bp стейбл/мажор-пулы, забивающие top-N
    rank_by: str = "volumeUSD24"
    exclude_scam: bool = True        # бесплатный пре-фильтр Codex, полноценный security — этап 3

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


def parse_pair(row: dict[str, Any]) -> PoolSnapshot | None:
    """None для строк, по которым нельзя посчитать доходность (нет комиссии, чужая сеть и т.п.)."""
    pair = row.get("pair") or {}
    network = _NETWORK_BY_ID.get(pair.get("networkId"))
    fee = _num(row.get("poolFeeBps"))
    liquidity = _num(row.get("liquidity"))
    if not (network and pair.get("address") and fee is not None and liquidity is not None):
        return None

    # Целевой токен — не quote-сторона пары (WBNB/USDT/WETH)
    side = "token0" if row.get("quoteToken") == "token1" else "token1"
    return PoolSnapshot(
        address=pair["address"].lower(),
        token=pair[side].lower(),
        network=network,
        fee_bps=fee,
        liquidity_usd=liquidity,
        vol_1h_usd=_num(row.get("volumeUSD1")) or 0.0,
        vol_24h_usd=_num(row.get("volumeUSD24")) or 0.0,
        symbol=(row.get(side) or {}).get("symbol") or "",
        exchange=(row.get("exchange") or {}).get("name") or "",
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

    def fetch(self, query: ScreenerQuery) -> list[PoolSnapshot]:
        data = self._request({"query": QUERY, "variables": query.variables()})
        rows = (data.get("filterPairs") or {}).get("results") or []
        pools = [p for row in rows if row and (p := parse_pair(row))]
        if len(pools) < len(rows):
            log.info("screener: dropped %d of %d rows without fee/liquidity", len(rows) - len(pools), len(rows))
        return pools

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
