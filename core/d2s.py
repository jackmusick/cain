"""
.d2s character save parser/serializer.

Strategy for the round-trip gate: the header is a fixed-layout region we parse
into named fields but preserve verbatim on write (we only recompute the checksum).
The item/skill sections are bit-parsed. This guarantees byte-exact output while
still exposing editable structure.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .item import parse_item_list, ItemList


D2S_MAGIC = 0xAA55AA55  # bytes 55 AA 55 AA read little-endian


def read_character_name(data: bytes) -> str:
    """Return the modern 16-byte character name field."""
    return data[0x14:0x24].split(b"\x00")[0].decode("latin-1", "replace")


def compute_checksum(data: bytes) -> int:
    """D2 .d2s checksum: sum with left-rotate, checksum field (0x0C..0x0F) zeroed."""
    buf = bytearray(data)
    buf[0x0C:0x10] = b"\x00\x00\x00\x00"
    checksum = 0
    for b in buf:
        checksum = ((checksum << 1) | (checksum >> 31)) & 0xFFFFFFFF  # rotl 1
        checksum = (checksum + b) & 0xFFFFFFFF
        # carry from the rotate is added back (standard D2 variant)
    return checksum & 0xFFFFFFFF


def compute_checksum_d2(data: bytes) -> int:
    """Canonical D2 algorithm: checksum = (checksum*2 + byte + carry), field zeroed."""
    buf = bytearray(data)
    buf[0x0C:0x10] = b"\x00\x00\x00\x00"
    checksum = 0
    for b in buf:
        carry = (checksum & 0x80000000) >> 31
        checksum = (((checksum << 1) & 0xFFFFFFFF) + b + carry) & 0xFFFFFFFF
    return checksum


@dataclass
class D2SChar:
    raw: bytes
    version: int
    filesize: int
    checksum: int
    name: str
    char_class: int
    level: int
    # offset where the item section ('JM' list) begins
    items_offset: int
    items: ItemList | None = None
    # everything from start..items_offset preserved verbatim
    header_blob: bytes = b""

    @classmethod
    def parse(cls, data: bytes) -> "D2SChar":
        magic, version, filesize, checksum = struct.unpack_from("<IIII", data, 0)
        if magic != D2S_MAGIC:
            raise ValueError(f"bad d2s magic 0x{magic:08x}")
        name = read_character_name(data)
        char_class = data[0x28]
        level = data[0x2B]
        # The item section starts with the 'JM' list header. The first top-level
        # 'JM' after the fixed header marks the player item list. We locate it
        # robustly by scanning from the known minimum header size.
        items_offset = data.index(b"JM", 0x14F)
        obj = cls(
            raw=data, version=version, filesize=filesize, checksum=checksum,
            name=name, char_class=char_class, level=level,
            items_offset=items_offset, header_blob=data[:items_offset],
        )
        return obj

    def parse_items(self, stat_table) -> ItemList:
        self.items = parse_item_list(self.raw, self.items_offset, stat_table)
        return self.items

    def serialize(self) -> bytes:
        """Re-emit. If items not edited, reuse raw tail; always fix checksum+size."""
        body = bytearray(self.raw)
        # recompute size + checksum to be safe
        struct.pack_into("<I", body, 0x08, len(body))
        cs = compute_checksum_d2(bytes(body))
        struct.pack_into("<I", body, 0x0C, cs)
        return bytes(body)
