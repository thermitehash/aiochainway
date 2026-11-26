"""Every complete frame quoted in docs/ must be a valid frame."""

from __future__ import annotations

import pathlib
import re

import pytest

from aiochainway.protocol.frame import decode

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
FRAME = re.compile(r"A5 5A(?: [0-9A-F]{2})+ 0D 0A")


def _frames() -> list[tuple[str, str]]:
    found = []
    for path in sorted(DOCS.glob("*.md")):
        for match in FRAME.finditer(path.read_text()):
            found.append((path.name, match.group(0)))
    return found


FRAMES = _frames()


def test_docs_quote_frames() -> None:
    assert len(FRAMES) >= 20


@pytest.mark.parametrize(("doc", "frame"), FRAMES)
def test_doc_frame_is_valid(doc: str, frame: str) -> None:
    decode(bytes.fromhex(frame.replace(" ", "")))
