"""TagPresence."""

from __future__ import annotations

import pytest

from aiochainway import TagPresence, TagRead
from aiochainway.protocol.errors import ValidationError


def tag(epc: str, rssi: float | None = -60.0, ant: int = 1) -> TagRead:
    return TagRead(epc=epc, pc=0x3000, antenna=ant, rssi_dbm=rssi)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_first_last_seen_and_counts() -> None:
    clock = Clock()
    p = TagPresence(clock=clock)
    p.feed(tag("AA", -70, 1))
    clock.now = 1005
    s = p.feed(tag("AA", -50, 2))
    assert (s.first_seen, s.last_seen, s.read_count) == (1000, 1005, 2)
    assert s.best_rssi == -50 and s.last_rssi == -50
    assert s.antennas == frozenset({1, 2}) and s.last_antenna == 2
    assert s.age(1010) == 5
    clock.now = 1006
    s = p.feed(tag("AA", -80, 2))
    assert s.best_rssi == -50 and s.last_rssi == -80


def test_rssi_none_keeps_previous() -> None:
    p = TagPresence(clock=Clock())
    p.feed(tag("AA", None))
    s = p.get("AA")
    assert s is not None and s.best_rssi is None
    p.feed(tag("AA", -40))
    p.feed(tag("AA", None))
    s = p.get("AA")
    assert s is not None and s.best_rssi == -40 and s.last_rssi == -40


def test_present_departed_and_prune() -> None:
    p = TagPresence(clock=Clock())
    p.feed(tag("AA"), now=100)
    p.feed(tag("BB"), now=125)
    assert [s.epc for s in p.present(now=130, ttl=10)] == ["BB"]
    assert [s.epc for s in p.departed(now=130, ttl=10)] == ["AA"]
    assert [s.epc for s in p.present(now=130, ttl=30)] == ["AA", "BB"]
    # Exactly ttl old is still present.
    assert [s.epc for s in p.present(now=135, ttl=10)] == ["BB"]
    gone = p.prune(now=130, ttl=10)
    assert [s.epc for s in gone] == ["AA"]
    assert "AA" not in p and "BB" in p and len(p) == 1


def test_out_of_order_now_keeps_latest_last_seen() -> None:
    p = TagPresence(clock=Clock())
    p.feed(tag("AA"), now=200)
    s = p.feed(tag("AA"), now=150)
    assert s.last_seen == 200 and s.first_seen == 200


def test_bounded_memory_evicts_least_recent() -> None:
    p = TagPresence(max_epcs=2, clock=Clock())
    p.feed(tag("AA"), now=1)
    p.feed(tag("BB"), now=2)
    p.feed(tag("AA"), now=3)  # A is now most recent
    p.feed(tag("CC"), now=4)
    assert set(p.snapshot()) == {"AA", "CC"}
    assert p.evicted == 1


def test_feed_many_forget_snapshot_copy() -> None:
    p = TagPresence(clock=Clock())
    p.feed_many([tag("AA"), tag("BB")], now=1)
    snap = p.snapshot()
    assert p.forget("AA") and not p.forget("AA")
    assert "AA" in snap and p.get("AA") is None


def test_default_clock_is_monotonic() -> None:
    p = TagPresence()
    s = p.feed(tag("AA"))
    assert p.present(ttl=60) == [s]


def test_validation() -> None:
    with pytest.raises(ValidationError):
        TagPresence(max_epcs=0)
    p = TagPresence()
    with pytest.raises(ValidationError):
        p.present(ttl=-1)
    with pytest.raises(ValidationError):
        p.departed(ttl=-1)
