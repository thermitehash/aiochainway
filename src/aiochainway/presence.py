"""Tag presence tracking (pure, no I/O).

Feed :class:`TagRead` objects in; ask which EPCs are present (seen within a
TTL) or have departed. Memory is bounded: past ``max_epcs`` the least recently
seen EPC is evicted.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
import time

from .models import TagRead
from .protocol.errors import ValidationError

Clock = Callable[[], float]
DEFAULT_MAX_EPCS = 10_000


@dataclass(frozen=True, slots=True)
class TagSighting:
    """What is known about one EPC."""

    epc: str
    first_seen: float
    last_seen: float
    read_count: int
    last_rssi: float | None
    best_rssi: float | None
    antennas: frozenset[int]
    last_antenna: int

    def age(self, now: float) -> float:
        """Seconds since this EPC was last read."""
        return now - self.last_seen


class TagPresence:
    """Track first/last seen, RSSI and antennas per EPC.

    Times come from ``clock`` (``time.monotonic`` by default) at :meth:`feed`
    time, or from the explicit ``now`` argument, so tests can inject time.
    """

    def __init__(
        self, *, max_epcs: int = DEFAULT_MAX_EPCS, clock: Clock = time.monotonic
    ) -> None:
        """Create an empty tracker."""
        if max_epcs < 1:
            raise ValidationError("max_epcs must be at least 1")
        self.max_epcs = max_epcs
        self._clock = clock
        self._tags: OrderedDict[str, TagSighting] = OrderedDict()
        #: EPCs evicted because ``max_epcs`` was reached.
        self.evicted = 0

    def __len__(self) -> int:
        """Return the number of EPCs tracked."""
        return len(self._tags)

    def __contains__(self, epc: object) -> bool:
        """Whether ``epc`` is tracked (present or not)."""
        return epc in self._tags

    def feed(self, tag: TagRead, now: float | None = None) -> TagSighting:
        """Record one read and return the updated sighting."""
        at = self._clock() if now is None else now
        prev = self._tags.get(tag.epc)
        if prev is None:
            sighting = TagSighting(
                epc=tag.epc,
                first_seen=at,
                last_seen=at,
                read_count=1,
                last_rssi=tag.rssi_dbm,
                best_rssi=tag.rssi_dbm,
                antennas=frozenset((tag.antenna,)),
                last_antenna=tag.antenna,
            )
        else:
            best = prev.best_rssi
            if tag.rssi_dbm is not None and (best is None or tag.rssi_dbm > best):
                best = tag.rssi_dbm
            sighting = replace(
                prev,
                last_seen=max(prev.last_seen, at),
                read_count=prev.read_count + 1,
                last_rssi=tag.rssi_dbm if tag.rssi_dbm is not None else prev.last_rssi,
                best_rssi=best,
                antennas=prev.antennas | {tag.antenna},
                last_antenna=tag.antenna,
            )
        self._tags[tag.epc] = sighting
        self._tags.move_to_end(tag.epc)
        while len(self._tags) > self.max_epcs:
            self._tags.popitem(last=False)
            self.evicted += 1
        return sighting

    def feed_many(self, tags: Iterable[TagRead], now: float | None = None) -> None:
        """Record several reads."""
        for tag in tags:
            self.feed(tag, now)

    def get(self, epc: str) -> TagSighting | None:
        """Return the sighting for ``epc``, if tracked."""
        return self._tags.get(epc)

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    @staticmethod
    def _check_ttl(ttl: float) -> None:
        if ttl < 0:
            raise ValidationError("ttl must not be negative")

    def present(self, now: float | None = None, ttl: float = 30.0) -> list[TagSighting]:
        """EPCs seen within the last ``ttl`` seconds (most recent last)."""
        self._check_ttl(ttl)
        at = self._now(now)
        return [s for s in self._tags.values() if at - s.last_seen <= ttl]

    def departed(
        self, now: float | None = None, ttl: float = 30.0
    ) -> list[TagSighting]:
        """EPCs tracked but not seen for more than ``ttl`` seconds."""
        self._check_ttl(ttl)
        at = self._now(now)
        return [s for s in self._tags.values() if at - s.last_seen > ttl]

    def forget(self, epc: str) -> bool:
        """Stop tracking ``epc``; return whether it was tracked."""
        return self._tags.pop(epc, None) is not None

    def prune(self, now: float | None = None, ttl: float = 30.0) -> list[TagSighting]:
        """Remove and return departed EPCs."""
        gone = self.departed(now, ttl)
        for sighting in gone:
            del self._tags[sighting.epc]
        return gone

    def snapshot(self) -> dict[str, TagSighting]:
        """Return a copy of every tracked sighting, keyed by EPC."""
        return dict(self._tags)
