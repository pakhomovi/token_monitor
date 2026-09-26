from dataclasses import dataclass
from enum import Enum


class Network(str, Enum):
    BSC = "bsc"
    BASE = "base"
    ROBINHOOD = "robinhood"


class Verdict(str, Enum):
    FAST = "fast"
    SLOW = "slow"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    """Рыночные данные пула от screener'а (Codex/Defined и т.п.)."""

    address: str
    token: str
    network: Network
    fee_bps: float          # 25 = 0.25%
    liquidity_usd: float    # v2: TVL; v3/v4: активная ликвидность около текущей цены
    vol_1h_usd: float
    vol_24h_usd: float
    symbol: str = ""
    exchange: str = ""


@dataclass(frozen=True, slots=True)
class SecurityInfo:
    """Данные security-проверки (GMGN). None = источник не вернул значение."""

    honeypot: bool | None = None
    buy_tax: float | None = None     # 0.05 = 5%
    sell_tax: float | None = None
    top10_share: float | None = None
    dev_rug_history: bool | None = None
