from token_monitor.dev import (CreationTx, DevReport, LaunchedToken, Outflow, dev_report, enrich_launches,
                               parse_creation)
from token_monitor.onchain import TRANSFER_TOPIC, word

TOKEN, DEV, FACTORY, POOL = "0x" + "a" * 40, "0x" + "d" * 40, "0x" + "f" * 40, "0x" + "e" * 40
T = "0x" + TRANSFER_TOPIC.hex()
LAUNCH_TOPIC = "0x" + "11" * 32
SUPPLY = 10**27


def t(addr):
    return "0x" + word(addr)


RECEIPT = {"blockNumber": hex(100), "logs": [
    {"address": FACTORY, "topics": [LAUNCH_TOPIC, t(DEV), t(TOKEN)], "data": "0x"},
    {"address": TOKEN, "topics": [T, t(0), t(POOL)], "data": hex(SUPPLY)},
    {"address": TOKEN, "topics": [T, t(POOL), t(DEV)], "data": hex(SUPPLY // 4)},     # покупка дева на старте
]}
TX = {"hash": "0xabc", "from": DEV.upper().replace("0X", "0x"), "to": FACTORY}


def test_parse_creation_finds_launcher_buy_and_launch_event():
    c = parse_creation(RECEIPT, TX, TOKEN)
    assert (c.launcher, c.factory, c.block) == (DEV, FACTORY, 100)
    assert c.dev_received == SUPPLY // 4
    assert c.launch_event == (FACTORY, LAUNCH_TOPIC, 1, 2)


def test_direct_deploy_has_no_factory():
    c = parse_creation({"blockNumber": "0x1", "logs": []}, {"hash": "0x1", "from": DEV, "to": None}, TOKEN)
    assert c.factory is None and c.launch_event is None and c.dev_received == 0


def report(**kw):
    base = dict(token=TOKEN, total_supply=SUPPLY, creation=parse_creation(RECEIPT, TX, TOKEN),
                balance_now=0, outflows=(Outflow(POOL, SUPPLY // 4, True),))
    return DevReport(**{**base, **kw})


def test_flags():
    assert report().flags() == ["dev bought 25.0% at launch"]
    hidden = report(outflows=(Outflow("0x" + "1" * 40, SUPPLY // 50, False),), balance_now=SUPPLY // 10)
    assert "dev still holds 10.0%" in hidden.flags() and "dev moved 2.0% to other wallets" in hidden.flags()
    serial = report(launches=tuple(LaunchedToken(f"0x{i:040x}", market_cap=3_000) for i in range(6)))
    assert "serial launcher: 6 launches, 0 alive" in serial.flags()


def test_enrich_launches_from_codex():
    r = enrich_launches(report(launches=(LaunchedToken(TOKEN),)),
                        [{"address": TOKEN.upper().replace("0X", "0x"), "symbol": "VRAX", "marketCap": "7000000",
                          "holders": 7000, "migrated": True}])
    assert r.launches[0].symbol == "VRAX" and r.launches[0].alive


class FakeReader:
    """Минимальный RPC: время блока = номер блока, логи и вызовы из заготовок."""
    def __init__(self):
        self.calls = []

    def _rpc(self, method, params):
        if method == "eth_blockNumber":
            return hex(1_000)
        if method == "eth_getTransactionReceipt":
            return RECEIPT
        if method == "eth_getTransactionByHash":
            return TX
        if method == "eth_getCode":
            return "0x6000" if params[0] == POOL else "0x"
        raise AssertionError(method)

    def block_timestamp(self, block):
        return block

    def logs(self, address, topics, frm, to):
        self.calls.append((address, topics))
        if address == TOKEN and topics[1] == t(0):
            return [{"blockNumber": hex(100), "logIndex": "0x1", "transactionHash": "0xabc"}]
        if address == TOKEN:                                   # выводы дева
            return [{"topics": [T, t(DEV), t(POOL)], "data": hex(SUPPLY // 4)}]
        if address == FACTORY:                                 # другие запуски
            return [{"topics": [LAUNCH_TOPIC, t(DEV), t(TOKEN)]},
                    {"topics": [LAUNCH_TOPIC, t(DEV), t("0x" + "b" * 40)]}]
        return []

    def batch(self, calls):
        return ["0x" + word(SUPPLY), "0x" + word(0)]


def test_dev_report_end_to_end():
    reader = FakeReader()
    r = dev_report(reader, TOKEN, created_ts=100, launches_lookback_blocks=500)
    assert r.creation.launcher == DEV and r.bought_share == 0.25 and r.holds_share == 0
    assert r.outflows == (Outflow(POOL, SUPPLY // 4, True),) and r.to_wallets_share == 0
    assert [x.address for x in r.launches] == [TOKEN, "0x" + "b" * 40]
    # Поиск запусков — по событию лаунчпада с адресом дева на нужной позиции
    assert (FACTORY, [LAUNCH_TOPIC, t(DEV), None]) in reader.calls
