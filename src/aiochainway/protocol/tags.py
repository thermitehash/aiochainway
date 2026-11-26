"""Tag record parsing (payload of reply CMD ``0x83``).

Record layout, from the SDK's ``h.a(byte[], boolean)``::

    PC(2) | EPC(N) | [TID(12) | [USER(M)]] | RSSI(2) | ANT(1)

``N = (PC[0] >> 3) * 2``. Which optional banks are present is decided only by
the record length, as in the SDK:

* ``len == 2 + N + 3``: EPC only
* ``len == 2 + N + 12 + 3``: EPC + TID
* ``len >  2 + N + 12 + 3``: EPC + TID + USER (USER is everything in between)

The SDK silently accepts records whose length fits none of these (it ignores
trailing bytes, or pads a short EPC with zeros). With ``strict=True`` (the
default) such records raise :class:`FrameError` instead. ``strict=False``
follows the SDK's tolerance for trailing bytes after an EPC-only record.
"""

from __future__ import annotations

import time

from ..models import ANTENNA_MAX, ANTENNA_MIN, TagRead
from .errors import FrameError

PC_LEN = 2
TID_LEN = 12
RSSI_LEN = 2
ANT_LEN = 1
TAIL_LEN = RSSI_LEN + ANT_LEN
#: RSSI magnitudes at or above this (in dB) are treated as invalid, as in the SDK.
RSSI_MAX_MAGNITUDE = 200.0


def epc_length(pc: bytes) -> int:
    """Return the EPC length in bytes encoded in the PC word."""
    return (pc[0] >> 3) * 2


def rssi_to_dbm(raw: int) -> float | None:
    """Convert the reader's 16-bit RSSI to dBm, or ``None`` if implausible.

    ``dbm = -((0xFFFF - raw) / 10)``, valid only when the magnitude is strictly
    between 0 and 200 (SDK ``h.a``).
    """
    magnitude = (0xFFFF - raw) / 10
    if 0 < magnitude < RSSI_MAX_MAGNITUDE:
        return -round(magnitude, 1)
    return None


def parse_tag_record(
    data: bytes,
    *,
    strict: bool = True,
    source: str = "",
    received_at: float | None = None,
) -> TagRead:
    """Parse one ``0x83`` payload into a :class:`TagRead`.

    Raises:
        FrameError: the record is truncated, has an impossible length, or the
            antenna byte is not 1-4. Never raises ``IndexError``.

    """
    data = bytes(data)
    total = len(data)
    if total < PC_LEN + TAIL_LEN:
        raise FrameError(f"tag record too short: {total} bytes")
    pc = data[:PC_LEN]
    epc_end = PC_LEN + epc_length(pc)
    if total < epc_end + TAIL_LEN:
        raise FrameError(
            f"tag record truncated: PC 0x{pc.hex().upper()} needs "
            f"{epc_end + TAIL_LEN} bytes, got {total}"
        )
    tid: bytes | None = None
    user: bytes | None = None
    tail_start: int
    if total >= epc_end + TID_LEN + TAIL_LEN:
        tid_end = epc_end + TID_LEN
        tid = data[epc_end:tid_end]
        tail_start = total - TAIL_LEN
        if tail_start > tid_end:
            user = data[tid_end:tail_start]
    elif total == epc_end + TAIL_LEN or not strict:
        tail_start = epc_end
    else:
        raise FrameError(
            f"tag record length {total} fits neither EPC ({epc_end + TAIL_LEN}) "
            f"nor EPC+TID (>= {epc_end + TID_LEN + TAIL_LEN})"
        )
    rssi_raw = (data[tail_start] << 8) | data[tail_start + 1]
    antenna = data[tail_start + RSSI_LEN]
    if not ANTENNA_MIN <= antenna <= ANTENNA_MAX:
        raise FrameError(f"antenna byte {antenna} is not {ANTENNA_MIN}-{ANTENNA_MAX}")
    return TagRead(
        epc=data[PC_LEN:epc_end].hex().upper(),
        pc=(pc[0] << 8) | pc[1],
        antenna=antenna,
        rssi_dbm=rssi_to_dbm(rssi_raw),
        tid=tid.hex().upper() if tid is not None else None,
        user=user.hex().upper() if user is not None else None,
        received_at=time.time() if received_at is None else received_at,
        source=source,
    )
