import pytest

from token_monitor.models import Network
from token_monitor.store import CandidateStore, Status


@pytest.fixture
def store(tmp_path):
    s = CandidateStore(str(tmp_path / "c.db"), cooldown_hours=1)
    yield s
    s.close()


def add(store, pool="0xPOOL", now=1000.0, network=Network.BSC):
    return store.add_pending(network, pool, "0xTOKEN", "slow", "card", now=now)


def test_add_pending_respects_cooldown(store):
    assert add(store, now=1000)
    assert not add(store, now=1000 + 1800)      # внутри часа
    assert add(store, now=1000 + 3601)          # cooldown прошёл


def test_addresses_are_case_insensitive(store):
    add(store, pool="0xABC")
    assert store.get(Network.BSC, "0xabc").status is Status.PENDING


def test_same_address_on_different_networks_is_distinct(store):
    assert add(store, network=Network.BSC)
    assert add(store, network=Network.BASE)


def test_happy_path_transitions(store):
    add(store)
    assert store.transition(Network.BSC, "0xpool", Status.APPROVED)
    assert store.transition(Network.BSC, "0xpool", Status.COLLECTED, report={"x": 1})
    assert store.transition(Network.BSC, "0xpool", Status.RESEARCHED)
    c = store.get(Network.BSC, "0xpool")
    assert c.status is Status.RESEARCHED
    assert c.report == {"x": 1}                 # отчёт не затирается переходом без report


def test_double_approve_is_rejected(store):
    add(store)
    assert store.transition(Network.BSC, "0xpool", Status.APPROVED)
    assert not store.transition(Network.BSC, "0xpool", Status.APPROVED)


def test_cannot_skip_states(store):
    add(store)
    assert not store.transition(Network.BSC, "0xpool", Status.RESEARCHED)
    assert not store.transition(Network.BSC, "0xpool", Status.PENDING)


def test_rejected_is_terminal(store):
    add(store)
    assert store.transition(Network.BSC, "0xpool", Status.REJECTED)
    assert not store.transition(Network.BSC, "0xpool", Status.APPROVED)


def test_by_status(store):
    add(store, pool="0x1")
    add(store, pool="0x2")
    store.transition(Network.BSC, "0x1", Status.APPROVED)
    assert [c.pool for c in store.by_status(Status.PENDING)] == ["0x2"]
    assert [c.pool for c in store.by_status(Status.APPROVED)] == ["0x1"]
