"""Diablo II Resurrected (.d2s version >= 97) save reader.

Built via the implement->round-trip-test loop against real D2R saves, with the
decompiled D2R.exe as tiebreaker for any bitfield the byte-exact gate can't
disambiguate. Mirrors the verbatim-preservation strategy of core/d2s.py and
core/stash.py: parse the fields we understand, copy everything else byte-for-
byte, recompute only filesize + checksum on write.

Confirmed deterministically against testdata/Ancksunamum.d2s (version 105):
  - magic/version/filesize/checksum/activeweapon at legacy offsets 0x00..0x13
  - checksum algorithm is unchanged from LoD (compute_checksum_d2)
  - the fixed header expanded; the character name moved from 0x14 to 0x12b
  - section markers are unchanged from LoD: Woo!/WS/w4/gf/if/JM/jf/kf/lf
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .bitreader import BitReader
from .d2r_tables import FOLLOWSTATS, ITEM_BASES, SAVEBITS, SAVEPARAMBITS
from .d2s import compute_checksum_d2

# D2R item base codes are Huffman-coded (LSB-first walk). Tree transcribed from
# d07riv's d2r.html reference; verified against real save data. Walk node[bit]
# from the root until a leaf (a single character).
_HUFFMAN_TREE = [[[[["w", "u"], [["8", ["y", ["5", ["j", []]]]], "h"]],
                   ["s", [["2", "n"], "x"]]],
                  [[["c", ["k", "f"]], "b"], [["t", "m"], ["9", "7"]]]],
                 [" ", [[[["e", "d"], "p"],
                         ["g", [[["z", "q"], "3"], ["v", "6"]]]],
                        [["r", "l"], ["a", [["1", ["4", "0"]], ["i", "o"]]]]]]]


def huffman_char(br: BitReader) -> str:
    """Decode one Huffman-coded character from the bitstream (LSB-first)."""
    node = _HUFFMAN_TREE
    while isinstance(node, list):
        if not node:  # dead branch ([] leaf) — malformed stream
            raise ValueError("huffman: walked into empty node")
        node = node[br.read_bit()]
    return node


def read_item_code(br: BitReader) -> str:
    """Read a 4-char D2R item base code; the 4th char must be a space.
    Returns the trimmed 3-char code (e.g. 'cap')."""
    chars = [huffman_char(br) for _ in range(4)]
    if chars[3] != " ":
        raise ValueError(f"bad item code, 4th char not space: {chars!r}")
    return "".join(chars[:3]).rstrip()


def _parse_stats(br: BitReader) -> None:
    """Walk a stat list to its 0x1ff terminator (d07riv parseStats)."""
    while True:
        sid = br.read(9)
        if sid == 511:
            break
        if sid >= len(SAVEBITS) or SAVEBITS[sid] == 0 and SAVEPARAMBITS[sid] == 0:
            # tolerate zero-width only if it's a real stat; else it's a desync
            if sid >= len(SAVEBITS):
                raise ValueError(f"unknown stat id {sid}")
        br.read(SAVEPARAMBITS[sid] if sid < len(SAVEPARAMBITS) else 0)
        br.read(SAVEBITS[sid] if sid < len(SAVEBITS) else 0)
        follow = FOLLOWSTATS[sid] if sid < len(FOLLOWSTATS) else 0
        while follow > 0:
            sid += 1
            br.read(SAVEPARAMBITS[sid] if sid < len(SAVEPARAMBITS) else 0)
            br.read(SAVEBITS[sid] if sid < len(SAVEBITS) else 0)
            follow -= 1


def walk_item(br: BitReader) -> dict:
    """Parse one D2R item starting at br.pos (no per-item JM marker). Advances
    br to just past the item (byte-aligned). Returns decoded summary fields.
    Faithful port of d07riv's parseItem for D2R."""
    start_bit = br.pos
    br.read(4)
    br.read(1)
    br.read(6)
    socketed = br.read(1)
    br.read(4)
    ear = br.read(1)
    br.read(4)
    simple = br.read(1)
    br.read(2)
    personalized = br.read(1)
    br.read(1)
    runeword = br.read(1)
    br.read(8)           # D2R: 8-bit field after runeword flag
    br.read(3)           # location
    br.read(4)           # body location
    br.read(4)           # inv column
    br.read(3)           # inv row
    br.read(1)
    br.read(3)           # storage page

    if ear:
        br.read(10)
        while br.read(7):
            pass
        br.align_byte()
        return {"start_bit": start_bit, "end_bit": br.pos, "ear": True}

    code = read_item_code(br)
    base = ITEM_BASES.get(code, 0)
    sockets_in = 0
    quality = 0

    if not simple:
        sockets_in = br.read(3)
        br.read(32)      # guid
        br.read(7)       # ilvl
        quality = br.read(4)
        if br.read(1):   # has graphic
            br.read(3)
        if br.read(1):   # has class info / autoprefix
            br.read(11)
        if quality in (1, 3):      # inferior / superior
            br.read(3)
        elif quality == 4:         # magic: 11-bit prefix + 11-bit suffix
            br.read(11)
            br.read(11)
        elif quality in (5, 7):    # set / unique id
            br.read(12)
        elif quality in (6, 8):    # rare / crafted: 2 name ids + up to 6 affixes
            br.read(8)
            br.read(8)
            for _ in range(6):
                if br.read(1):
                    br.read(11)
        # quality 2 (normal): nothing extra
        if runeword:
            br.read(16)
        if personalized:
            while br.read(7):
                pass
        if base & 8:     # tome
            br.read(5)
        if br.read(1):   # realm data
            br.read(96)
        if base & 4:     # armor: defense
            br.read(11)
        if base & 6:     # armor or weapon: durability
            if br.read(8):
                br.read(9)
        if base & 1:     # stackable: quantity
            br.read(9)
        if socketed:
            br.read(4)
        setflags = br.read(5) if quality == 5 else 0
        _parse_stats(br)
        for bit in range(5):
            if setflags & (1 << bit):
                _parse_stats(br)
        if runeword:
            _parse_stats(br)
    else:
        br.read(1)

    br.align_byte()
    item = {"start_bit": start_bit, "end_bit": br.pos, "code": code,
            "quality": quality, "simple": bool(simple), "socketed": bool(socketed),
            "ear": False, "children": []}
    for _ in range(sockets_in):
        item["children"].append(walk_item(br))
    return item


def parse_items(data: bytes, section_bit: int) -> tuple[int, list[dict]]:
    """Parse the JM item list at section_bit. Returns (count, [items])."""
    br = BitReader(data, section_bit)
    if br.read(8) != ord("J") or br.read(8) != ord("M"):
        raise ValueError("missing JM item-list header")
    count = br.read(16)
    items = [walk_item(br) for _ in range(count)]
    return count, items

D2S_MAGIC = 0xAA55AA55
D2R_MIN_VERSION = 97  # 0x61; LoD is 96 (0x60)

# Offsets that survived from LoD into the D2R header.
OFF_MAGIC = 0x00
OFF_VERSION = 0x04
OFF_FILESIZE = 0x08
OFF_CHECKSUM = 0x0C
OFF_ACTIVE_WEAPON = 0x10

# Section markers (unchanged from LoD), in file order.
SECTION_MARKERS = [
    (b"Woo!", "quests"),
    (b"WS", "waypoints"),
    (b"w4", "npc_intro"),
    (b"gf", "stats"),
    (b"if", "skills"),
    (b"JM", "items"),
]


@dataclass
class D2RChar:
    raw: bytes
    version: int
    filesize: int
    checksum: int
    name: str
    name_offset: int
    sections: dict[str, int] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: bytes) -> "D2RChar":
        magic, version, filesize, checksum = struct.unpack_from("<IIII", data, 0)
        if magic != D2S_MAGIC:
            raise ValueError(f"bad d2s magic 0x{magic:08x}")
        if version < D2R_MIN_VERSION:
            raise ValueError(
                f"version {version} is not D2R (>= {D2R_MIN_VERSION}); use core.d2s")

        name_offset, name = cls._find_name(data)
        sections: dict[str, int] = {}
        cursor = OFF_ACTIVE_WEAPON
        for marker, label in SECTION_MARKERS:
            idx = data.find(marker, cursor)
            if idx >= 0:
                sections[label] = idx
                cursor = idx + len(marker)
        return cls(raw=data, version=version, filesize=filesize, checksum=checksum,
                   name=name, name_offset=name_offset, sections=sections)

    @staticmethod
    def _find_name(data: bytes) -> tuple[int, str]:
        """The name is a NUL-terminated ASCII field in the expanded header,
        before the first section. Locate it as the first printable run >= 2
        chars after the fixed prologue."""
        first_section = min(
            (data.find(m, OFF_ACTIVE_WEAPON) for m, _ in SECTION_MARKERS
             if data.find(m, OFF_ACTIVE_WEAPON) >= 0),
            default=len(data))
        i = 0x20
        while i < first_section:
            b = data[i]
            if 0x41 <= b <= 0x7A:  # A-z start
                j = i
                while j < first_section and 0x20 <= data[j] < 0x7F:
                    j += 1
                if j - i >= 2 and (j < len(data) and data[j] == 0):
                    return i, data[i:j].decode("latin-1")
            i += 1
        return -1, ""

    def serialize(self) -> bytes:
        """Re-emit. Verbatim body for now; only filesize + checksum recomputed."""
        body = bytearray(self.raw)
        struct.pack_into("<I", body, OFF_FILESIZE, len(body))
        struct.pack_into("<I", body, OFF_CHECKSUM, 0)
        struct.pack_into("<I", body, OFF_CHECKSUM, compute_checksum_d2(bytes(body)))
        return bytes(body)
