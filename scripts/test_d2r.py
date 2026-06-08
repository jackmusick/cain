#!/usr/bin/env python3
"""D2R codec regression checks. Run: .venv/bin/python scripts/test_d2r.py

Validates the pieces proven so far against the real v105 save:
  - byte-exact round-trip (header + sections, verbatim preservation)
  - the Huffman item base-code decoder, against known starter items
"""
import struct
import sys

sys.path.insert(0, ".")

from core.bitreader import BitReader
from core.d2r import D2RChar, read_item_code

SAVE = "testdata/Ancksunamum.d2s"


def test_roundtrip():
    data = open(SAVE, "rb").read()
    out = D2RChar.parse(data).serialize()
    assert out == data, "round-trip not byte-exact"
    print("PASS round-trip byte-exact")


def test_header():
    c = D2RChar.parse(open(SAVE, "rb").read())
    assert c.version == 105, c.version
    assert c.name == "Ancksunamum", c.name
    assert set(c.sections) >= {"quests", "stats", "skills", "items"}
    print(f"PASS header: v{c.version} name={c.name!r} sections={list(c.sections)}")


def test_huffman_item_codes():
    # 5 uniform 10-byte simple items: 4x hp1, 1x tsc. Base code at bit 53.
    data = open(SAVE, "rb").read()
    jm = data.find(b"JM", 0x300)
    start = jm + 4
    expected = ["hp1", "hp1", "hp1", "hp1", "tsc"]
    got = []
    for i in range(len(expected)):
        br = BitReader(data, (start + i * 10) * 8 + 53)
        got.append(read_item_code(br))
    assert got == expected, f"{got} != {expected}"
    print(f"PASS huffman item codes: {got}")


if __name__ == "__main__":
    test_roundtrip()
    test_header()
    test_huffman_item_codes()
    print("\nall D2R checks passed")
