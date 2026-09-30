"""Проверка дева по блокчейну: кто реально запустил токен, сколько взял на старте, что сделал с долей,
сколько ещё токенов запустил и что с ними стало.

Работает через RPC, без GMGN/эксплореров: Codex часто показывает «деплоером» контракт лаунчпада,
а реальный запускатор — отправитель транзакции, в которой токен сминтился.
"""
from dataclasses import dataclass, field
from typing import Sequence

from .onchain import TRANSFER_TOPIC, ChainReader, address, calldata, word, words

ZERO_TOPIC = "0x" + word(0)
T_TOPIC = "0x" + TRANSFER_TOPIC.hex()


@dataclass(frozen=True, slots=True)
class CreationTx:
    tx_hash: str
    block: int
    launcher: str                 # tx.from — реальный запускатор
    factory: str | None           # tx.to: лаунчпад; None — токен задеплоен напрямую
    dev_received: int             # сколько токенов получил запускатор в транзакции создания (сырые)
    # Событие лаунчпада, где индексированы и запускатор, и токен: по нему ищутся другие запуски
    launch_event: tuple[str, str, int, int] | None = None   # (адрес, topic0, позиция дева, позиция токена)


@dataclass(frozen=True, slots=True)
class Outflow:
    to: str
    amount: int
    is_contract: bool


@dataclass(frozen=True, slots=True)
class LaunchedToken:
    address: str
    symbol: str = ""
    market_cap: float = 0.0
    holders: int = 0
    migrated: bool | None = None

    @property
    def alive(self) -> bool:
        return self.market_cap >= 50_000


@dataclass(frozen=True, slots=True)
class DevReport:
    token: str
    total_supply: int
    creation: CreationTx
    balance_now: int
    outflows: tuple[Outflow, ...]
    launches: tuple[LaunchedToken, ...] = field(default=())

    @property
    def bought_share(self) -> float:
        return self.creation.dev_received / self.total_supply if self.total_supply else 0.0

    @property
    def holds_share(self) -> float:
        return self.balance_now / self.total_supply if self.total_supply else 0.0

    @property
    def to_wallets_share(self) -> float:
        """Доля предложения, переведённая дево на обычные кошельки: способ спрятать долю."""
        return sum(o.amount for o in self.outflows if not o.is_contract) / self.total_supply if self.total_supply else 0.0

    def flags(self) -> list[str]:
        out = []
        if self.bought_share >= 0.05:
            out.append(f"dev bought {self.bought_share:.1%} at launch")
        if self.holds_share >= 0.05:
            out.append(f"dev still holds {self.holds_share:.1%}")
        if self.to_wallets_share >= 0.01:
            out.append(f"dev moved {self.to_wallets_share:.1%} to other wallets")
        if len(self.launches) >= 5:
            alive = sum(t.alive for t in self.launches)
            out.append(f"serial launcher: {len(self.launches)} launches, {alive} alive")
        return out


def parse_creation(receipt: dict, tx: dict, token: str) -> CreationTx:
    """Разбор транзакции, в которой токен сминтился (Transfer из 0x0)."""
    token = token.lower()
    launcher = tx["from"].lower()
    launcher_topic, token_topic = "0x" + word(launcher), "0x" + word(token)
    received = sum(int(lg["data"], 16) for lg in receipt["logs"]
                   if lg["address"].lower() == token and lg["topics"][0] == T_TOPIC
                   and len(lg["topics"]) == 3 and lg["topics"][2] == launcher_topic)
    event = None
    for lg in receipt["logs"]:
        topics = [t.lower() for t in lg["topics"]]
        if lg["address"].lower() != token and launcher_topic in topics[1:] and token_topic in topics[1:]:
            event = (lg["address"].lower(), topics[0], topics.index(launcher_topic), topics.index(token_topic))
            break
    return CreationTx(tx["hash"], int(receipt["blockNumber"], 16), launcher,
                      tx["to"].lower() if tx.get("to") else None, received, event)


def block_at(reader: ChainReader, ts: int) -> int:
    """Первый блок с timestamp ≥ ts (бинарный поиск по заголовкам)."""
    lo, hi = 0, int(reader._rpc("eth_blockNumber", []), 16)
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (mid + 1, hi) if reader.block_timestamp(mid) < ts else (lo, mid)
    return lo


def find_creation(reader: ChainReader, token: str, created_ts: int, window: int = 5_000) -> CreationTx:
    around = block_at(reader, created_ts)
    logs = reader.logs(token, [T_TOPIC, ZERO_TOPIC], max(0, around - window), around + window)
    if not logs:
        raise RuntimeError("mint transfer not found near creation time")
    first = min(logs, key=lambda lg: (int(lg["blockNumber"], 16), int(lg.get("logIndex", "0x0"), 16)))
    tx_hash = first["transactionHash"]
    return parse_creation(reader._rpc("eth_getTransactionReceipt", [tx_hash]),
                          reader._rpc("eth_getTransactionByHash", [tx_hash]), token)


def dev_report(reader: ChainReader, token: str, created_ts: int, launches_lookback_blocks: int = 0,
               outflow_blocks: int = 1_000_000) -> DevReport:
    token = token.lower()
    c = find_creation(reader, token, created_ts)
    supply_raw, bal_raw = reader.batch([(token, calldata("totalSupply()")),
                                        (token, calldata("balanceOf(address)", c.launcher))])
    head = int(reader._rpc("eth_blockNumber", []), 16)
    outs = reader.logs(token, [T_TOPIC, "0x" + word(c.launcher)], c.block, min(head, c.block + outflow_blocks))
    per_dest: dict[str, int] = {}
    for lg in outs:
        to = address(int(lg["topics"][2], 16)).lower()
        per_dest[to] = per_dest.get(to, 0) + int(lg["data"], 16)
    outflows = tuple(Outflow(to, amt, len(str(reader._rpc("eth_getCode", [to, "latest"]))) > 2)
                     for to, amt in sorted(per_dest.items(), key=lambda kv: -kv[1]))

    launched: tuple[LaunchedToken, ...] = ()
    if c.launch_event and launches_lookback_blocks:
        emitter, topic0, dev_pos, token_pos = c.launch_event
        topics: list = [topic0] + [None] * max(dev_pos, token_pos)
        topics[dev_pos] = "0x" + word(c.launcher)
        logs = reader.logs(emitter, topics, max(0, head - launches_lookback_blocks), head)
        launched = tuple(dict.fromkeys(LaunchedToken(address(int(lg["topics"][token_pos], 16)).lower())
                                       for lg in logs if len(lg["topics"]) > token_pos))
    return DevReport(token, words(supply_raw)[0], c, words(bal_raw)[0], outflows, launched)


def enrich_launches(report: DevReport, stats: Sequence[dict]) -> DevReport:
    """Подставляет в запуски метрики из Codex filterTokens (address, symbol, marketCap, holders, migrated)."""
    by = {s["address"].lower(): s for s in stats}
    launches = tuple(LaunchedToken(t.address, by.get(t.address, {}).get("symbol", ""),
                                   float(by.get(t.address, {}).get("marketCap") or 0),
                                   int(by.get(t.address, {}).get("holders") or 0),
                                   by.get(t.address, {}).get("migrated")) for t in report.launches)
    return DevReport(report.token, report.total_supply, report.creation, report.balance_now, report.outflows, launches)
