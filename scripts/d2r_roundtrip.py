#!/usr/bin/env python3
"""D2R save round-trip oracle.

The iteration loop for reverse-engineering the D2R (.d2s v97+) format:

  decode(file) -> structured -> encode(structured) -> bytes
  assert bytes == original   (byte-exact gate)

Run against a real D2R save; the first offset that differs tells us exactly
which field the reader/writer gets wrong. Mirrors the discipline used for the
PD2 stash/item codecs (core/stash.py, core/item_v2.py).

  .venv/bin/python scripts/d2r_roundtrip.py testdata/Ancksunamum.d2s
"""
from __future__ import annotations

import struct
import sys

sys.path.insert(0, ".")


def hexdump_diff(a: bytes, b: bytes, around: int = 16) -> str:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            lo = max(0, i - around)
            hi = min(n, i + around)
            return (f"first diff at 0x{i:x} ({i})\n"
                    f"  orig: {a[lo:hi].hex(' ')}\n"
                    f"  ours: {b[lo:hi].hex(' ')}\n"
                    f"        {'   ' * (i - lo)}^^")
    if len(a) != len(b):
        return f"identical for {n} bytes but lengths differ: orig={len(a)} ours={len(b)}"
    return "BYTE-EXACT"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "testdata/Ancksunamum.d2s"
    data = open(path, "rb").read()
    magic, version, filesize, checksum = struct.unpack_from("<IIII", data, 0)
    print(f"{path}: version={version} (0x{version:x}) size={len(data)} "
          f"filesize_field={filesize} checksum=0x{checksum:08x}")

    try:
        from core.d2r import D2RChar  # noqa: PLC0415 — optional until implemented
    except ImportError:
        print("\ncore/d2r.py not implemented yet — header probe only.")
        _probe(data)
        return 0

    char = D2RChar.parse(data)
    out = char.serialize()
    print("\nround-trip:", hexdump_diff(data, out))
    return 0 if out == data else 1


def _probe(data: bytes):
    """Locate section markers and the name to seed the reader."""
    markers = {b"Woo!": "quests", b"w4": "npc-intro", b"gf": "stats",
               b"if": "skills", b"JM": "items", b"jf": "merc",
               b"kf": "merc-end", b"lf": "iron-golem"}
    print("\nsection markers:")
    for tag, label in markers.items():
        i = data.find(tag, 0x10)
        print(f"  {label:11s} {tag!r:8} @ {hex(i) if i >= 0 else '-'}")


if __name__ == "__main__":
    raise SystemExit(main())
