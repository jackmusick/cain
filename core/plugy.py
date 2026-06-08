"""
PlugY personal (.d2x = CSTM01) and shared (.sss = SSS) stash parser.

Format:
  Header:  "CSTM01" (.d2x) or "SSS\0"+version (.sss), then global fields, then pages.
  Page:    "ST" + flags(4 bytes) + name(null-terminated ASCII) + "JM" + count(u16)
           + `count` item records (same JM item codec as .d2s).

For RE we mainly need clean item boundaries + the page NAME (which labels the
known contents — invaluable for verification).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .item import parse_one_item, ITEM_MARKER


@dataclass
class StashPage:
    name: str
    count: int
    items: list = field(default_factory=list)
    start_byte: int = 0


@dataclass
class Stash:
    kind: str  # "personal" | "shared"
    pages: list = field(default_factory=list)


def parse_stash(data: bytes, stat_table) -> Stash:
    if data[:6] == b"CSTM01":
        kind = "personal"
    elif data[:3] == b"SSS":
        kind = "shared"
    else:
        raise ValueError(f"unknown stash magic {data[:6]!r}")

    stash = Stash(kind=kind)
    # Walk pages by locating 'ST' markers; each page header is
    # 'ST' + 4 flag bytes + null-terminated name + 'JM' + u16 count.
    i = data.find(b"ST")
    while i != -1 and i < len(data):
        # name starts after ST + 4 flag bytes
        name_start = i + 6
        nul = data.find(b"\x00", name_start)
        name = data[name_start:nul].decode("latin-1", "replace")
        jm = data.find(ITEM_MARKER, nul)
        if jm < 0:
            break
        count = struct.unpack_from("<H", data, jm + 2)[0]
        page = StashPage(name=name, count=count, start_byte=i)
        bit = (jm + 4) * 8
        for _ in range(count):
            if data[bit // 8:bit // 8 + 2] != ITEM_MARKER:
                nb = data.find(ITEM_MARKER, bit // 8)
                if nb < 0:
                    break
                bit = nb * 8
            item, bit = parse_one_item(data, bit, stat_table)
            page.items.append(item)
            for _c in range(item.num_items_in_sockets):
                if data[bit // 8:bit // 8 + 2] != ITEM_MARKER:
                    break
                child, bit = parse_one_item(data, bit, stat_table)
                item.socketed_children.append(child)
        stash.pages.append(page)
        # next page
        i = data.find(b"ST", (bit // 8) if page.items else (jm + 4))
    return stash


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from core.tables import GameTables
    gt = GameTables(sys.argv[2] if len(sys.argv) > 2 else
                    r"C:\Users\JackMusick\Downloads\Diablo II\ProjectD2\pd2data.mpq")
    st = gt.stat_table()
    data = open(sys.argv[1], "rb").read()
    stash = parse_stash(data, st)
    print(f"{stash.kind} stash: {len(stash.pages)} pages")
    for pg in stash.pages[:12]:
        clean = sum(1 for it in pg.items if it.next_is_jm)
        print(f"  '{pg.name}': {len(pg.items)}/{pg.count} items, {clean} clean")
