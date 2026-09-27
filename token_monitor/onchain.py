"""Чтение LP-позиций Uniswap v3/v4 напрямую с блокчейна: JSON-RPC eth_call без web3.

Все вызовы одной позиции уходят батчами (JSON-RPC batch), поэтому позиция читается за 3–4 запроса.
Адреса контрактов — из @uniswap/sdk-core (sdks/sdk-core/src/addresses.ts).
"""
import os
import random
import re
import time
from dataclasses import dataclass
from functools import cache
from typing import Callable, Sequence

import httpx
from Crypto.Hash import keccak

from .models import Network

Q128 = 1 << 128
Q96 = 1 << 96
U256 = 1 << 256
NATIVE = "0x" + "0" * 40
V4_DYNAMIC_FEE = 0x800000


@dataclass(frozen=True, slots=True)
class Chain:
    rpc: str
    v3_factory: str
    v3_npm: str
    v4_posm: str
    v4_state_view: str


CHAINS: dict[Network, Chain] = {
    Network.BASE: Chain("https://mainnet.base.org",
                        "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
                        "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
                        "0x7c5f5a4bbd8fd63184577525326123b519429bdc",
                        "0xa3c0c9b65bad0b08107aa264b0f3db444b867a71"),
    Network.ROBINHOOD: Chain("https://rpc.mainnet.chain.robinhood.com",
                             "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
                             "0x73991a25c818bf1f1128deaab1492d45638de0d3",
                             "0x58daec3116aae6d93017baaea7749052e8a04fa7",
                             "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"),
    Network.BSC: Chain("https://bsc-dataseed.bnbchain.org",
                       "0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7",
                       "0x7b8A01B39D58278b5DE7e48c8449c9f4F5170613",
                       "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b",
                       "0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4"),
}

_URL_NETWORKS = {"base": Network.BASE, "robinhood": Network.ROBINHOOD, "bnb": Network.BSC, "bsc": Network.BSC}


# --- ABI: только статические типы + строка symbol() ---

def keccak256(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


@cache
def selector(signature: str) -> str:
    return keccak256(signature.encode()).hex()[:8]


def word(value: int | str | bytes) -> str:
    if isinstance(value, str):                     # address
        return value.lower().removeprefix("0x").rjust(64, "0")
    if isinstance(value, bytes):                   # bytes32
        return value.hex().ljust(64, "0")
    return f"{value % U256:064x}"                  # int/uint, отрицательные — дополнительный код


def calldata(signature: str, *args: int | str | bytes) -> str:
    return "0x" + selector(signature) + "".join(word(a) for a in args)


def words(data: str) -> list[int]:
    raw = data.removeprefix("0x")
    return [int(raw[i:i + 64], 16) for i in range(0, len(raw), 64)]


def signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def address(value: int) -> str:
    return "0x" + f"{value:040x}"[-40:]


def decode_string(data: str) -> str:
    raw = bytes.fromhex(data.removeprefix("0x"))
    if len(raw) == 32:                             # старые токены: bytes32
        return raw.rstrip(b"\0").decode(errors="replace")
    if len(raw) < 64:
        return ""
    length = int.from_bytes(raw[32:64], "big")
    return raw[64:64 + length].decode(errors="replace")


# --- Математика позиции ---

def sqrt_price_at_tick(tick: int) -> float:
    return 1.0001 ** (tick / 2)


def amounts(liquidity: int, sqrt_p: float, tick_lower: int, tick_upper: int) -> tuple[float, float]:
    """Сырые количества token0/token1 в позиции при текущей √цене."""
    sa, sb = sqrt_price_at_tick(tick_lower), sqrt_price_at_tick(tick_upper)
    sp = min(max(sqrt_p, sa), sb)
    return liquidity * (sb - sp) / (sp * sb), liquidity * (sp - sa)


def unclaimed(liquidity: int, inside_now: int, inside_last: int) -> int:
    # feeGrowth — счётчики по модулю 2^256, переполнение штатное
    return liquidity * ((inside_now - inside_last) % U256) // Q128


def v3_fee_growth_inside(tick: int, lower: int, upper: int, glob: int, out_lower: int, out_upper: int) -> int:
    below = out_lower if tick >= lower else glob - out_lower
    above = out_upper if tick < upper else glob - out_upper
    return (glob - below - above) % U256


def v4_unpack_info(info: int) -> tuple[int, int]:
    """PositionInfo: | poolId 200b | tickUpper 24b | tickLower 24b | hasSubscriber 8b |."""
    return signed(info >> 8, 24), signed(info >> 32, 24)


TRANSFER_TOPIC = keccak256(b"Transfer(address,address,uint256)")


def v4_pool_id(currency0: str, currency1: str, fee: int, tick_spacing: int, hooks: str) -> bytes:
    return keccak256(bytes.fromhex("".join(word(v) for v in (currency0, currency1, fee, tick_spacing, hooks))))


def parse_position_url(url: str) -> tuple[Network, int, int]:
    """https://app.uniswap.org/positions/v4/robinhood/3318457 → (сеть, версия, tokenId)."""
    m = re.search(r"/positions/v([34])/([a-z]+)/(\d+)", url)
    if not m or m[2] not in _URL_NETWORKS:
        raise ValueError(f"unsupported position url: {url}")
    return _URL_NETWORKS[m[2]], int(m[1]), int(m[3])


class RateLimited(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Token:
    address: str
    symbol: str
    decimals: int


@dataclass(frozen=True, slots=True)
class Position:
    network: Network
    version: int
    token_id: int
    pool: str                  # v3: адрес пула, v4: poolId
    token0: Token
    token1: Token
    fee_bps: float
    tick_lower: int
    tick_upper: int
    tick: int
    sqrt_price: float          # √(token1/token0) в сырых единицах
    liquidity: int
    pool_liquidity: int        # активная ликвидность пула у текущей цены
    fees0: int                 # несобранные комиссии, сырые единицы
    fees1: int

    @property
    def in_range(self) -> bool:
        return self.tick_lower <= self.tick < self.tick_upper

    @property
    def amounts(self) -> tuple[float, float]:
        a0, a1 = amounts(self.liquidity, self.sqrt_price, self.tick_lower, self.tick_upper)
        return a0 / 10 ** self.token0.decimals, a1 / 10 ** self.token1.decimals

    @property
    def fees(self) -> tuple[float, float]:
        return self.fees0 / 10 ** self.token0.decimals, self.fees1 / 10 ** self.token1.decimals

    @property
    def fee_share(self) -> float:
        """Доля позиции в активной ликвидности: столько объёма пула она забирает, пока цена в диапазоне."""
        return self.liquidity / self.pool_liquidity if self.in_range and self.pool_liquidity else 0.0

    def price_at_tick(self, tick: int | None = None) -> float:
        """Цена token0 в token1 с учётом decimals."""
        t = self.tick if tick is None else tick
        return 1.0001 ** t * 10 ** (self.token0.decimals - self.token1.decimals)

    @property
    def price(self) -> float:
        return self.sqrt_price ** 2 * 10 ** (self.token0.decimals - self.token1.decimals)


class ChainReader:
    def __init__(self, network: Network, *, client: httpx.Client | None = None, url: str | None = None,
                 max_batch: int = 4, retries: int = 7, sleep: Callable[[float], None] = time.sleep):
        self.network = network
        self.chain = CHAINS[network]
        self.url = url or os.environ.get(f"RPC_{network.name}") or self.chain.rpc
        self._client = client or httpx.Client(timeout=30)
        # Публичные RPC считают каждый вызов батча отдельно: мелкие батчи + backoff на rate limit
        self._max_batch, self._retries, self._sleep = max_batch, retries, sleep

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ChainReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def batch(self, calls: Sequence[tuple[str, str]]) -> list[str]:
        out: list[str] = []
        for i in range(0, len(calls), self._max_batch):
            out += self._with_retry(calls[i:i + self._max_batch])
        return out

    def _with_retry(self, calls: Sequence[tuple[str, str]]) -> list[str]:
        for attempt in range(self._retries + 1):
            try:
                return self._batch(calls)
            except RateLimited:
                if attempt == self._retries:
                    raise
                self._sleep(min(0.5 * 2 ** attempt, 20) * random.uniform(0.5, 1.5))
        raise AssertionError("unreachable")

    def _batch(self, calls: Sequence[tuple[str, str]]) -> list[str]:
        payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}
                   for i, (to, data) in enumerate(calls)]
        resp = self._client.post(self.url, json=payload)
        if resp.status_code == 429:
            raise RateLimited()
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict):                 # ошибка на уровне всего батча
            raise RuntimeError(f"rpc: {body.get('error')}")
        by_id = {r["id"]: r for r in body}
        out = []
        for i, (to, data) in enumerate(calls):
            r = by_id.get(i) or {}
            if "rate limit" in str(r.get("error", "")).lower():
                raise RateLimited()
            if "error" in r or "result" not in r:
                raise RuntimeError(f"rpc {to} {data[:10]}: {r.get('error')}")
            out.append(r["result"])
        return out

    def tokens(self, addresses: Sequence[str]) -> list[Token]:
        erc20 = [a for a in addresses if a != NATIVE]
        results = self.batch([(a, calldata(s)) for a in erc20 for s in ("symbol()", "decimals()")])
        meta = {a: Token(a, decode_string(results[2 * i]), words(results[2 * i + 1])[0]) for i, a in enumerate(erc20)}
        return [meta.get(a) or Token(NATIVE, "ETH", 18) for a in addresses]

    def _rpc(self, method: str, params: list) -> object:
        for attempt in range(self._retries + 1):
            resp = self._client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            body = resp.json() if resp.status_code != 429 else {"error": {"message": "rate limit"}}
            err = str(body.get("error") or "")
            if "rate limit" in err.lower() and attempt < self._retries:
                self._sleep(min(0.5 * 2 ** attempt, 20) * random.uniform(0.5, 1.5))
                continue
            if "error" in body:
                raise RuntimeError(f"rpc {method}: {body['error']}")
            return body["result"]
        raise AssertionError("unreachable")

    def block_timestamp(self, block: int | str = "latest") -> int:
        tag = hex(block) if isinstance(block, int) else block
        return int(self._rpc("eth_getBlockByNumber", [tag, False])["timestamp"], 16)

    def minted_at(self, version: int, token_id: int) -> tuple[int, int]:
        """(блок, unix-время) минта NFT позиции — фактически момент входа.

        Сначала событие Transfer(0x0 → owner) через eth_getLogs; если RPC ограничивает диапазон логов,
        бинарный поиск по ownerOf на исторических блоках (нужен архивный узел).
        """
        nft = self.chain.v3_npm if version == 3 else self.chain.v4_posm
        head = int(self._rpc("eth_blockNumber", []), 16)
        topics = ["0x" + TRANSFER_TOPIC.hex(), "0x" + word(0), None, "0x" + word(token_id)]
        if found := self._mint_block_from_logs(nft, topics, head):
            return found, self.block_timestamp(found)

        def exists(block: int) -> bool:
            try:
                return len(str(self._rpc("eth_call", [{"to": nft, "data": calldata("ownerOf(uint256)", token_id)},
                                                     hex(block)]))) > 2
            except RuntimeError as e:
                if "not available" in str(e) or "missing trie" in str(e):
                    raise RuntimeError("rpc has no archive state and limits eth_getLogs") from e
                return False          # revert: токена ещё нет (или уже сожжён)

        lo, hi = 0, head
        while lo < hi:
            mid = (lo + hi) // 2
            lo, hi = (lo, mid) if exists(mid) else (mid + 1, hi)
        return lo, self.block_timestamp(lo)

    def _mint_block_from_logs(self, nft: str, topics: list, head: int,
                              window: int = 2_000_000, min_window: int = 100_000) -> int | None:
        """Идём окнами назад от head; при ошибке (лимит диапазона/ответа) окно уменьшается вдвое."""
        to = head
        while to > 0:
            frm = max(0, to - window + 1)
            try:
                logs = self._rpc("eth_getLogs", [{"address": nft, "fromBlock": hex(frm), "toBlock": hex(to),
                                                  "topics": topics}])
            except RuntimeError:
                if window <= min_window:
                    return None          # RPC не тянет логи — пусть работает бинарный поиск
                window //= 2
                continue
            if logs:
                return int(logs[0]["blockNumber"], 16)
            to = frm - 1
        return None

    def position(self, version: int, token_id: int) -> Position:
        return self._v3(token_id) if version == 3 else self._v4(token_id)

    def _v3(self, token_id: int) -> Position:
        c = self.chain
        (pos,) = self.batch([(c.v3_npm, calldata("positions(uint256)", token_id))])
        w = words(pos)
        t0, t1, fee = address(w[2]), address(w[3]), w[4]
        lower, upper, liq = signed(w[5], 24), signed(w[6], 24), w[7]
        (pool_raw,) = self.batch([(c.v3_factory, calldata("getPool(address,address,uint24)", t0, t1, fee))])
        pool = address(words(pool_raw)[0])
        slot0, pool_liq, g0, g1, tl, tu = self.batch([
            (pool, calldata("slot0()")), (pool, calldata("liquidity()")),
            (pool, calldata("feeGrowthGlobal0X128()")), (pool, calldata("feeGrowthGlobal1X128()")),
            (pool, calldata("ticks(int24)", lower)), (pool, calldata("ticks(int24)", upper)),
        ])
        s = words(slot0)
        tick = signed(s[1], 24)
        wl, wu = words(tl), words(tu)
        in0 = v3_fee_growth_inside(tick, lower, upper, words(g0)[0], wl[2], wu[2])
        in1 = v3_fee_growth_inside(tick, lower, upper, words(g1)[0], wl[3], wu[3])
        tok0, tok1 = self.tokens([t0, t1])
        return Position(self.network, 3, token_id, pool, tok0, tok1, fee / 100, lower, upper, tick,
                        s[0] / Q96, liq, words(pool_liq)[0],
                        w[10] + unclaimed(liq, in0, w[8]), w[11] + unclaimed(liq, in1, w[9]))

    def _v4(self, token_id: int) -> Position:
        c = self.chain
        info_raw, liq_raw = self.batch([
            (c.v4_posm, calldata("getPoolAndPositionInfo(uint256)", token_id)),
            (c.v4_posm, calldata("getPositionLiquidity(uint256)", token_id)),
        ])
        w = words(info_raw)
        c0, c1, fee, spacing, hooks = address(w[0]), address(w[1]), w[2], signed(w[3], 24), address(w[4])
        lower, upper = v4_unpack_info(w[5])
        liq = words(liq_raw)[0]
        pid = v4_pool_id(c0, c1, fee, spacing, hooks)
        slot0, pool_liq, inside, last = self.batch([
            (c.v4_state_view, calldata("getSlot0(bytes32)", pid)),
            (c.v4_state_view, calldata("getLiquidity(bytes32)", pid)),
            (c.v4_state_view, calldata("getFeeGrowthInside(bytes32,int24,int24)", pid, lower, upper)),
            (c.v4_state_view, calldata("getPositionInfo(bytes32,address,int24,int24,bytes32)",
                                       pid, c.v4_posm, lower, upper, token_id)),
        ])
        s, g, l = words(slot0), words(inside), words(last)
        lp_fee = s[3] if fee == V4_DYNAMIC_FEE else fee    # динамическая комиссия — текущая из slot0
        tok0, tok1 = self.tokens([c0, c1])
        return Position(self.network, 4, token_id, "0x" + pid.hex(), tok0, tok1, lp_fee / 100, lower, upper,
                        signed(s[1], 24), s[0] / Q96, liq, words(pool_liq)[0],
                        unclaimed(liq, g[0], l[1]), unclaimed(liq, g[1], l[2]))
