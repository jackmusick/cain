"""
Stash container codec for the three PD2 / PlugY stash containers:

  - PD2 SHARED stash   magic 0x55BB55BB  (file ..\\Save\\pd2_shared.stash)
        header ... then a single flat JM item-list ('JM' + u16 count + items).
        Verified against C:\\d2re_scripts\\projectdiablo_all.c
        (FUN_102e9e80 writer / FUN_102ea120 reader): the item section begins at
        offset 0x12e with the 'JM' marker followed by the u16 item count, then
        the items in the standard JM codec.

  - PlugY personal     magic 'CSTM01'    (*.d2x)
        header ('CSTM01' + u32 fields) then N pages, each an 'ST' block:
            'ST' + flags(u32) + name(null-terminated ASCII) + 'JM' + u16 count
            + count items (standard JM codec).
        Verified against C:\\d2re_scripts\\plugy_all.c
        (FUN_1000b260 reader / FUN_1000b4d0 writer / FUN_1000daa0 ST writer).

  - PlugY shared       magic 'SSS\\0'/'SSS '  (*.sss legacy)
        same per-page 'ST' block layout as CSTM01, different fixed header.

ROUND-TRIP STRATEGY (byte-exact whole file):
  Everything that is NOT a JM item is preserved VERBATIM (the container header
  and every ST-block prologue: the 'ST' tag, flags, name string + its null
  terminator, the 'JM' marker and the u16 item count). Only the JM items
  themselves are decoded (via core.item_v2) and re-serialized structurally from
  their decoded fields. The non-item bytes are sliced straight from the original
  and re-concatenated, so the container framing is reproduced exactly while the
  items prove they decode + re-encode losslessly.

  This makes the codec robust to any header fields we have not fully reverse
  engineered: we never rewrite them, we copy them.

The oracle (cli verify-stash): parse -> serialize is BYTE-IDENTICAL to the whole
original file, with 0 item resyncs and every item decode_ok.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import item_v2

ITEM_MARKER = b"JM"
ST_MARKER = b"ST"

# File bytes are 55 BB 55 BB; as a little-endian u32 that reads 0xBB55BB55 (the
# value the game's reader/writer in projectdiablo_all.c stores via local_158[0]).
MAGIC_SHARED55BB = 0xBB55BB55
MAGIC_CSTM = b"CSTM01"
MAGIC_SSS = b"SSS"


@dataclass
class StashPage:
    """A PlugY 'ST' page: prologue (tag+flags+name+JM+count) kept verbatim, the
    items decoded. `prologue` is the exact bytes from the 'ST' tag through the
    u16 item count (inclusive) and is re-emitted unchanged."""
    name: str
    flags: int
    count: int
    items: list = field(default_factory=list)  # top-level item_v2.Item
    prologue: bytes = b""        # 'ST'..count bytes, verbatim
    items_start: int = 0         # byte offset of first item (after the u16 count)
    items_end: int = 0           # byte offset just past the last item


@dataclass
class Stash:
    kind: str                    # "shared55bb" | "cstm" | "sss"
    raw: bytes
    header: bytes = b""          # verbatim header (up to first item region)
    trailer: bytes = b""         # verbatim trailing bytes after last item region
    # shared55bb: a single flat list
    flat_list: object = None     # item_v2.ItemList or None
    flat_list_header: bytes = b""  # the 'JM' + u16 count bytes (verbatim)
    # cstm / sss: pages
    pages: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def detect_kind(data: bytes) -> str:
    if len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == MAGIC_SHARED55BB:
        return "shared55bb"
    if data[:6] == MAGIC_CSTM:
        return "cstm"
    if data[:3] == MAGIC_SSS:
        return "sss"
    raise ValueError(f"unknown stash magic {data[:8]!r}")


def is_stash_file(path: str, data: bytes | None = None) -> bool:
    pl = path.lower()
    if pl.endswith((".d2x", ".sss", ".stash")):
        return True
    if data is not None:
        try:
            detect_kind(data)
            return True
        except ValueError:
            return False
    return False


# ---------------------------------------------------------------------------
# Item-list span helpers (shared by all three container kinds)
# ---------------------------------------------------------------------------
def _flat_items(il) -> list:
    """Flatten a parsed item_v2.ItemList into top-level + socketed children."""
    out = []
    for it in il.items:
        out.append(it)
        out.extend(it.children)
    return out


def _list_end_byte(il, default_byte: int) -> int:
    items = _flat_items(il)
    if not items:
        return default_byte
    return max(it.end_bit for it in items) // 8


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------
def parse_stash(data: bytes, stat_table) -> Stash:
    kind = detect_kind(data)
    stash = Stash(kind=kind, raw=data)
    if kind == "shared55bb":
        _parse_shared55bb(data, stash, stat_table)
    else:
        _parse_pages(data, stash, stat_table)
    return stash


def _parse_shared55bb(data: bytes, stash: Stash, stat_table) -> None:
    """Header verbatim up to the 'JM' list marker, then one flat JM item-list."""
    jm = data.find(ITEM_MARKER)
    if jm < 0:
        # no items at all; entire file is header
        stash.header = data
        return
    stash.header = data[:jm]
    il = item_v2.parse_item_list(data, jm, stat_table)
    stash.flat_list = il
    stash.flat_list_header = data[jm:jm + 4]  # 'JM' + u16 count
    end = _list_end_byte(il, jm + 4)
    stash.trailer = data[end:]


def _parse_pages(data: bytes, stash: Stash, stat_table) -> None:
    """CSTM01 / SSS: header verbatim up to the first 'ST' page, then walk pages."""
    first = data.find(ST_MARKER)
    if first < 0:
        stash.header = data
        return
    stash.header = data[:first]
    pos = first
    n = len(data)
    while pos + 6 <= n and data[pos:pos + 2] == ST_MARKER:
        page, pos = _parse_one_page(data, pos, stat_table)
        stash.pages.append(page)
        # The PlugY page list is sequential: the next page begins immediately at
        # the byte after the previous page's last item. If the next two bytes are
        # not 'ST', we have reached the end of the page section.
    stash.trailer = data[pos:]


def _parse_one_page(data: bytes, pos: int, stat_table):
    """Parse one 'ST' block starting at byte `pos`. Returns (StashPage, next_pos)."""
    flags = struct.unpack_from("<I", data, pos + 2)[0]
    name_start = pos + 6
    nul = data.find(b"\x00", name_start)
    if nul < 0:
        raise ValueError(f"page name not terminated at 0x{name_start:x}")
    name = data[name_start:nul].decode("latin-1", "replace")
    jm = nul + 1
    if data[jm:jm + 2] != ITEM_MARKER:
        # PlugY allows the name to be followed directly by 'JM'; if not aligned,
        # search forward defensively (kept verbatim either way via prologue).
        found = data.find(ITEM_MARKER, jm)
        if found < 0:
            raise ValueError(f"no JM after page name at 0x{jm:x}")
        jm = found
    count = struct.unpack_from("<H", data, jm + 2)[0]
    items_start = jm + 4
    prologue = data[pos:items_start]

    page = StashPage(name=name, flags=flags, count=count,
                     prologue=prologue, items_start=items_start)

    il = item_v2.parse_item_list(data, jm, stat_table)
    page.items = il.items
    # propagate resync bookkeeping onto the page (for the verifier)
    page._il = il  # type: ignore[attr-defined]
    page.items_end = _list_end_byte(il, items_start)
    return page, page.items_end


# ---------------------------------------------------------------------------
# Serialize (whole file)
# ---------------------------------------------------------------------------
def serialize_stash(stash: Stash, stat_table) -> bytes:
    if stash.kind == "shared55bb":
        return _serialize_shared55bb(stash, stat_table)
    return _serialize_pages(stash, stat_table)


def _serialize_item_region(items: list, stat_table) -> bytes:
    """Serialize a sequence of top-level items (with socketed children) to bytes
    using the structural item_v2 writer (the true inverse of the reader)."""
    bw = item_v2.BitWriter()

    def emit(it):
        bw.bits.extend(item_v2.serialize_item_struct(it, stat_table))
        for ch in it.children:
            emit(ch)

    for it in items:
        emit(it)
    return bw.to_bytes()


def _serialize_shared55bb(stash: Stash, stat_table) -> bytes:
    out = bytearray()
    out += stash.header
    if stash.flat_list is not None:
        out += stash.flat_list_header
        out += _serialize_item_region(stash.flat_list.items, stat_table)
    out += stash.trailer
    return bytes(out)


def _serialize_pages(stash: Stash, stat_table) -> bytes:
    out = bytearray()
    out += stash.header
    for page in stash.pages:
        out += page.prologue
        out += _serialize_item_region(page.items, stat_table)
    out += stash.trailer
    return bytes(out)


# ---------------------------------------------------------------------------
# Verify (whole-file byte-exact oracle)
# ---------------------------------------------------------------------------
@dataclass
class VerifyResult:
    kind: str
    in_len: int
    out_len: int
    byte_exact: bool
    n_pages: int
    n_items: int
    decode_ok: int
    clean: int
    resyncs: int
    first_diff: int = -1


def verify_stash(data: bytes, stat_table) -> VerifyResult:
    stash = parse_stash(data, stat_table)
    out = serialize_stash(stash, stat_table)

    all_items: list = []
    resyncs = 0
    if stash.kind == "shared55bb":
        if stash.flat_list is not None:
            all_items = _flat_items(stash.flat_list)
            resyncs += stash.flat_list.resyncs
        n_pages = 0
    else:
        n_pages = len(stash.pages)
        for page in stash.pages:
            all_items.extend(_flat_items_page(page))
            il = getattr(page, "_il", None)
            if il is not None:
                resyncs += il.resyncs

    decode_ok = sum(1 for it in all_items if it.decode_ok)
    clean = sum(1 for it in all_items if it.clean)

    byte_exact = out == data
    first_diff = -1
    if not byte_exact:
        for i in range(min(len(out), len(data))):
            if out[i] != data[i]:
                first_diff = i
                break
        if first_diff == -1:
            first_diff = min(len(out), len(data))

    return VerifyResult(
        kind=stash.kind,
        in_len=len(data),
        out_len=len(out),
        byte_exact=byte_exact,
        n_pages=n_pages,
        n_items=len(all_items),
        decode_ok=decode_ok,
        clean=clean,
        resyncs=resyncs,
        first_diff=first_diff,
    )


def _flat_items_page(page: StashPage) -> list:
    out = []
    for it in page.items:
        out.append(it)
        out.extend(it.children)
    return out
