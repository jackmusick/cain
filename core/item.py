"""
Item bitstream codec — the core of the editor.

Each item begins with the ASCII marker 'JM' (0x4A 0x4D), followed by a
bit-packed record. We parse the fixed flag header, the type code, and (for
"extended"/non-simple items) the property/stat list, which is driven entirely
by the live ItemStatCost schema (id -> save bits / save add / save param bits).
PD2 corruptions are just additional stats in this list with PD2-specific IDs —
because we read bit-widths from the live table, they parse without desync.

Round-trip philosophy: parse records, and re-serialize by re-emitting the exact
bits. For Phase 1 the gate is: parse the whole list, then re-pack to bytes that
are byte-identical to the original slice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .bitreader import BitReader, BitWriter


ITEM_MARKER = b"JM"
STAT_LIST_TERMINATOR = 0x1FF  # 511, 9-bit


@dataclass
class ItemStat:
    stat_id: int
    param: int          # param bits value (skill id, etc.), 0 if none
    value: int
    param_bits: int
    value_bits: int
    save_add: int
    value_bit_offset: int = -1  # absolute bit pos of the value field (for in-place edit)


@dataclass
class Item:
    start_bit: int
    end_bit: int
    # decoded fields (subset; full set grows as we cover features)
    identified: bool = False
    socketed: bool = False
    ethereal: bool = False
    simple: bool = False
    is_ear: bool = False
    personalized: bool = False
    runeword: bool = False
    is_new: bool = False
    starter_item: bool = False
    version: int = 0
    location_id: int = 0
    equipped_id: int = 0
    position_x: int = 0
    position_y: int = 0
    panel_id: int = 0
    ear_class: int = 0
    ear_level: int = 0
    ear_name: str = ""
    type_code: str = ""
    num_sockets: int = 0
    num_items_in_sockets: int = 0
    guid: int = 0
    quality: int = 0
    ilvl: int = 0
    graphic: int = -1
    autoprefix: int = -1
    prefix: int = -1
    suffix: int = -1
    set_unique_id: int = -1
    defense: int = -1
    max_durability: int = -1
    cur_durability: int = -1
    quantity: int = -1
    stats: list[ItemStat] = field(default_factory=list)
    extra_stat_lists: list = field(default_factory=list)
    socketed_children: list = field(default_factory=list)
    decode_ok: bool = False
    next_is_jm: bool = True
    clean: bool = False
    decode_error: str = ""
    raw_bits: list[int] = field(default_factory=list)  # verbatim bits for round-trip


@dataclass
class ItemList:
    raw: bytes
    items: list[Item] = field(default_factory=list)
    count: int = 0
    list_start_bit: int = 0
    resyncs: int = 0


def parse_item_list(data: bytes, offset: int, stat_table) -> ItemList:
    """Parse the player item list beginning at `offset` (points at 'JM' header).

    Layout: 'JM' (list marker) + uint16 count, then `count` items each starting
    with 'JM'. We parse each item to find its bit boundaries.
    """
    assert data[offset:offset + 2] == ITEM_MARKER, "expected JM list header"
    count = data[offset + 2] | (data[offset + 4 if False else offset + 3] << 8)
    # count is a little-endian uint16 right after the 'JM'
    count = int.from_bytes(data[offset + 2:offset + 4], "little")
    il = ItemList(raw=data, count=count, list_start_bit=(offset + 4) * 8)

    bit = (offset + 4) * 8
    for _ in range(count):
        # if we've desynced, the cursor won't be at a 'JM' — resync by scanning
        if data[bit // 8:bit // 8 + 2] != ITEM_MARKER:
            nb = data.find(ITEM_MARKER, bit // 8)
            if nb < 0:
                break
            bit = nb * 8
            il.resyncs += 1
        item, bit = parse_one_item(data, bit, stat_table)
        il.items.append(item)
        for _c in range(item.num_items_in_sockets):
            if data[bit // 8:bit // 8 + 2] != ITEM_MARKER:
                break
            child, bit = parse_one_item(data, bit, stat_table)
            item.socketed_children.append(child)
    return il


def parse_one_item(data: bytes, bit: int, stat_table):
    """Parse a single item starting at absolute bit `bit` (at its 'JM' marker).
    Returns (Item, next_bit). Full feature parse; falls back to scanning to the
    next 'JM' if the stat list can't be fully decoded (kept honest by round-trip).
    """
    br = BitReader(data, bit)
    # 'JM' marker (16 bits, byte-aligned)
    j = br.read(8)
    m = br.read(8)
    marker_ok = (j, m) == (0x4A, 0x4D)

    item = Item(start_bit=bit, end_bit=bit)
    # --- flag/header block, ported VERBATIM from d2itemreader d2item_parse_single ---
    br.read(4)
    item.identified = bool(br.read(1))
    br.read(6)
    item.socketed = bool(br.read(1))
    br.read(1)
    item.is_new = bool(br.read(1))
    br.read(2)
    item.is_ear = bool(br.read(1))
    item.starter_item = bool(br.read(1))
    br.read(3)
    item.simple = bool(br.read(1))
    item.ethereal = bool(br.read(1))
    br.read(1)
    item.personalized = bool(br.read(1))
    br.read(1)
    item.runeword = bool(br.read(1))
    br.read(5)
    item.version = br.read(8)
    br.read(2)
    item.location_id = br.read(3)
    item.equipped_id = br.read(4)
    item.position_x = br.read(4)
    item.position_y = br.read(4)
    item.panel_id = br.read(3)

    if not item.is_ear:
        # type code: 4 chars * 8 bits (space-padded), raw ASCII
        code = bytearray()
        for _ in range(4):
            c = br.read(8)
            code.append(0 if c == ord(" ") else c)
        item.type_code = bytes(code).rstrip(b"\x00").decode("latin-1", "replace")
        item.num_items_in_sockets = br.read(3)  # filled sockets (ALL items)
    else:
        item.ear_class = br.read(3)
        item.ear_level = br.read(7)
        name = []
        for _ in range(16):
            c = br.read(7)
            if c == 0:
                break
            name.append(c)
        item.ear_name = bytes(name).decode("latin-1", "replace")
        item.type_code = "ear"

    # Structured decode of extended items + stat list, SEQUENTIAL (cursor-based).
    # The parser's own end position is the boundary — no JM scanning (which gives
    # false hits on big saves). decode_ok = next byte starts with 'JM' (or EOF).
    try:
        if not item.simple and not item.is_ear:
            _decode_extended(br, item, stat_table)
        item.decode_ok = True
    except Exception as e:  # noqa: BLE001
        item.decode_error = repr(e)
        item.decode_ok = False

    # byte-align to the next item start
    end_bit = (br.bit_pos + 7) & ~7
    item.end_bit = end_bit
    br2 = BitReader(data, bit)
    nbits = end_bit - bit
    item.raw_bits = [br2.read_bit() for _ in range(nbits)]
    # validation: does the next position look like a valid record boundary?
    # Valid = next item ('JM'), next stash page ('ST'), or EOF/end-of-section.
    nb = end_bit // 8
    if nb + 2 <= len(data):
        nxt = data[nb:nb + 2]
        item.next_is_jm = nxt in (ITEM_MARKER, b"ST")
    else:
        item.next_is_jm = True  # EOF / end of section

    # Robust correctness: an item is only truly "clean" if it decoded without a
    # stat error AND ended at a real boundary. (next_is_jm alone gives false
    # positives — garbage that coincidentally lands on a 'JM' byte pair.)
    item.clean = item.decode_ok and item.next_is_jm
    return item, end_bit


# quality constants
Q_INFERIOR, Q_NORMAL, Q_SUPERIOR = 1, 2, 3
Q_MAGIC, Q_SET, Q_RARE, Q_UNIQUE, Q_CRAFTED = 4, 5, 6, 7, 8


def _decode_extended(br: BitReader, item: "Item", stat_table) -> None:
    """Decode the extended-item block following the type code, then the stat list.
    Ported from d2itemreader d2item_parse_single. num_items_in_sockets is already
    read (right after the type code) by parse_one_item, matching the canonical order.
    Mutates item. Raises on inconsistency."""
    item.guid = br.read(32)
    item.ilvl = br.read(7)
    item.quality = br.read(4)

    if br.read_bool():                        # multiplePictures
        item.graphic = br.read(3)
    if br.read_bool():                        # classSpecific
        item.autoprefix = br.read(11)

    q = item.quality
    if q in (Q_INFERIOR, Q_SUPERIOR):
        item.quality_data = br.read(3)
    elif q == Q_MAGIC:
        item.prefix = br.read(11)
        item.suffix = br.read(11)
    elif q in (Q_SET, Q_UNIQUE):
        item.set_unique_id = br.read(12)
    elif q in (Q_RARE, Q_CRAFTED):
        item.rare_name1 = br.read(8)
        item.rare_name2 = br.read(8)
        # up to 6 prefix/suffix slots, each prefixed by a present-bit
        item.rare_affixes = []
        for _ in range(6):
            if br.read_bool():
                item.rare_affixes.append(br.read(11))
    elif q == Q_NORMAL:
        pass

    # runeword data (16 bits total: 12 id + 4)
    if getattr(item, "runeword", False):
        item.runeword_id = br.read(12)
        item.runeword_pad = br.read(4)

    # personalization name (if flagged) — 7-bit chars, null-terminated
    if getattr(item, "personalized", False):
        name = []
        while True:
            ch = br.read(7)
            if ch == 0:
                break
            name.append(ch)
        item.personal_name = bytes(name).decode("latin-1", "replace")

    sch = getattr(stat_table, "schema", None)
    code = item.type_code

    # tome (ibk/tbk) extra 5 bits — AFTER personalization, BEFORE timestamp
    if code in ("ibk", "tbk"):
        item.tome_bits = br.read(5)

    # timestamp flag — 1 bit, EVERY extended item
    item.timestamp = br.read(1)

    is_armor = bool(sch) and code in sch.armor_codes
    is_weapon = bool(sch) and code in sch.weapon_codes
    # NOTE: there is NO extra post-timestamp bit. An earlier "pd2_bit" read was a
    # spurious 1-bit over-read that desynced rings/amulets/charms/jewels (the base
    # corpus is all armor/weapons, so it never exercised that branch and falsely
    # "validated" the read). Removing it fixes corrupted character items: corruption
    # is stored inline as ItemStatCost stats id360 'corrupted' + id361 'corruptor',
    # which _read_stat_list decodes for free via the live S12 schema. Verified:
    # berserk 36->132/134, blessed-hammer 31->93/95, bases unchanged 2585/2586,
    # round-trip byte-identical (workflow wf_4acacfeb-07f).

    # --- category-dependent fields, in d2itemreader spec order ---
    if is_armor:
        item.defense = br.read(11) - 10

    if is_armor or is_weapon:
        item.max_durability = br.read(8)
        if item.max_durability > 0:
            item.cur_durability = br.read(8)
            br.read(1)  # trailing random bit

    if sch and code in sch.stackable_codes:
        item.quantity = br.read(9)

    if item.socketed:
        item.num_sockets = br.read(4)

    # set items: 5-bit bitfield of which bonus property lists follow
    set_extra_lists = 0
    if q == Q_SET:
        set_extra_lists = br.read(5)

    # --- the main property/stat list ---
    item.stats = _read_stat_list(br, stat_table, item.version)
    # additional property lists (set bonuses) — each terminated by 0x1FF
    item.extra_stat_lists = []
    for _ in range(_popcount(set_extra_lists)):
        item.extra_stat_lists.append(_read_stat_list(br, stat_table, item.version))


def _popcount(x: int) -> int:
    return bin(x).count("1")


# nextInChain: stats melded with the following id (hardcoded in d2itemreader's
# d2gamedata_load_itemstats_common — no signifier exists in ItemStatCost.txt).
# 54->55->56 and 57->58->59 are two-step chains.
NEXT_IN_CHAIN = {17: 18, 48: 49, 50: 51, 52: 53, 54: 55, 55: 56, 57: 58, 58: 59}


def _read_stat_list(br: BitReader, stat_table, version: int = 101) -> list:
    """Read a property list: 9-bit stat ids until 0x1FF terminator.

    v103 (PD2 S13+) inserts ONE extra bit after each stat value (and after each
    nextInChain link value). Gated on version>=103 so v101 stays byte-identical.
    Verified (workflow wf_fc03ebea-692): LadyKiller 13/36 -> 26/42, v101 corpus
    unchanged, round-trip byte-exact.


    Faithful to d2itemreader's d2itemproplist_parse. The ItemStatCost "Encode"
    column selects the value layout:
      encode 0: [saveParamBits param] then [saveBits value]
      encode 1: same as 0 (param then value)            (rarely used)
      encode 2: hardcoded 6, 10, then saveBits           (e.g. by-time stats)
      encode 3: hardcoded 6, 10, 8, 8                     (e.g. param/charge stats)
    Plus the classic damage GROUPS where one id implies reading the next N
    consecutive stat ids' saveBits (min/max damage families).
    """
    stats = []
    while True:
        sid = br.read(9)
        if sid == STAT_LIST_TERMINATOR:
            break
        enc = stat_table.get(sid)
        if enc is None:
            raise ValueError(f"unknown stat id {sid} (not in ItemStatCost)")

        # v103 (PD2 S13) uses the VANILLA (non-S12) widths/add and reads ONE
        # LEADING bit before each value; v101 uses the S12 widths and no extra bit.
        # (Leading vs trailing land on the same boundary, but only leading yields
        # values matching UniqueItems.txt — verified workflow wf_12f20496-97e.)
        v103 = version >= 103
        if v103 and enc.save_bits_base:
            sbits, sadd, spar = enc.save_bits_base, enc.save_add_base, enc.save_param_bits_base
        else:
            sbits, sadd, spar = enc.save_bits, enc.save_add, enc.save_param_bits

        if sbits == 0:
            raise ValueError(f"stat {sid} has save_bits=0")

        if v103:
            br.read(1)  # v103 LEADING bit before the value field

        params = []
        val_off = -1  # bit offset of the editable value field (simple stats only)
        if enc.encode == 2:
            params.append(br.read(6) - sadd)
            params.append(br.read(10) - sadd)
            params.append(br.read(sbits) - sadd)
        elif enc.encode == 3:
            params.append(br.read(6) - sadd)
            params.append(br.read(10) - sadd)
            params.append(br.read(8) - sadd)
            params.append(br.read(8) - sadd)
        elif spar > 0:
            params.append(br.read(spar) - sadd)
            val_off = br.bit_pos
            params.append(br.read(sbits) - sadd)
        else:
            val_off = br.bit_pos
            params.append(br.read(sbits) - sadd)

        # follow nextInChain: each chained stat contributes one more saveBits value
        chain = enc
        while chain.stat_id in NEXT_IN_CHAIN:
            nxt = stat_table.get(NEXT_IN_CHAIN[chain.stat_id])
            if nxt is None:
                break
            if v103 and nxt.save_bits_base:
                nb, na, np_ = nxt.save_bits_base, nxt.save_add_base, nxt.save_param_bits_base
            else:
                nb, na, np_ = nxt.save_bits, nxt.save_add, nxt.save_param_bits
            if np_ != 0:
                break
            if v103:
                br.read(1)  # v103 leading bit before each chained value too
            params.append(br.read(nb) - na)
            chain = nxt
            val_off = -1  # chained: don't expose single-field edit

        stats.append(ItemStat(stat_id=sid, param=params[0] if len(params) > 1 else 0,
                              value=params[-1], param_bits=spar,
                              value_bits=sbits, save_add=sadd,
                              value_bit_offset=val_off))
    return stats


def _find_next_item_boundary(data: bytes, bit: int) -> int:
    """Find the next item's starting bit by scanning for a byte-aligned 'JM'
    after the current one. Items are byte-aligned in the list."""
    start_byte = (bit // 8) + 2  # skip current JM
    idx = data.find(ITEM_MARKER, start_byte)
    if idx == -1:
        return len(data) * 8
    return idx * 8


def serialize_item_list(il: ItemList, data: bytes) -> bytes:
    """Re-emit the item-list region from parsed items (verbatim bits)."""
    # list header: 'JM' + count(uint16) — reuse from raw up to first item
    if not il.items:
        return b""
    first = il.items[0].start_bit // 8
    header = data[(il.list_start_bit // 8) - 4:first]
    out = bytearray(header)
    for it in il.items:
        bw = BitWriter()
        bw.write_bits_list(it.raw_bits)
        out += bw.to_bytes()
    return bytes(out)
