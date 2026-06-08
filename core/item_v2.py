"""
Faithful PD2 item codec — transcribed 1:1 from D2Common.dll FUN_6fd7a600
(the game's own item reader) and its writer FUN_6fd77180. Decompiled via Ghidra
(see C:\\d2re_scripts\\reader_6fd7a600.c and d2common_all.c).

This REPLACES the d2itemreader-derived approximations in item.py. The crucial
correctness facts (verified against the decompiled C, NOT guessed):

  - bit reader primitive  Ordinal_10130(nbits)  -> BitReader.read   (LSB first)
  - bit writer primitive  Ordinal_10128(v,nbits) -> BitWriter.write
  - per-stat width = ItemStatCost row[0x19 + uVar11] / row[0x1c + uVar11*4],
    uVar11 = (version<0x5d). BOTH v101 (0x65) and v103 (0x67) are >= 0x5d, so
    uVar11 == 0 -> the struct's primary save-bits/save-add fields. PD2 populates
    those struct fields from DIFFERENT .txt columns by season:
      v101 (0x65, Season 12) -> 'Save Bits S12' / 'Save Add S12' (col 52)
                                = enc.save_bits / save_add
      v103 (0x67, Season 13+) -> vanilla 'Save Bits' / 'Save Add' (col 21/22)
                                = enc.save_bits_base / save_add_base
    Verified empirically: v101 stat 22 maxdamage is 10 bits (S12), v103 uniques
    decode to exact UniqueItems.txt values with the vanilla columns.
  - The generic stat reader FUN_6fd79d90 reads row[0x24] (save_param_bits)
    leading bits IF > 0, THEN row[0x19] (save_bits) value, minus row[0x1c].
    For v103 PD2 sets the in-memory param-leading to 1 for every stat, so v103
    reads exactly ONE leading bit before each stat value (incl. grouped values).
    This is the verified v103 stat-list delta (LadyKiller 13->38 clean with it).
  - There is ALSO an extra-32-bit block at reader lines 406-417 (writer
    36199-36212): a 1-bit flag when the item is extended and version > 0x56;
    if set, reads two 32-bit values, plus a THIRD when version > 0x5d.

Round-trip philosophy: parse EVERY field into the Item struct, then the writer
re-emits each field from the struct (NOT verbatim bits). To stay perfectly
faithful to the game's exact bit layout (incl. quality-block sub-fields and any
trailing alignment), every primitive read is journaled into `Item.ops`, and the
writer replays that journal. This guarantees byte-exact round-trip while still
exposing fully-decoded structure for editing (stat values can be patched and
re-emitted via the typed path).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .bitreader import BitReader, BitWriter

ITEM_MARKER = b"JM"
TERM = 0x1FF  # 9-bit stat-list terminator

# Quality codes (D2Common quality enum, value read into item.quality)
Q_INFERIOR, Q_NORMAL, Q_SUPERIOR = 1, 2, 3
Q_MAGIC, Q_SET, Q_RARE, Q_UNIQUE, Q_CRAFTED = 4, 5, 6, 7, 8

# Threshold constants from the reader (pvStack_18 = item version comparisons).
V_0x4A = 0x4A
V_0x51 = 0x51
V_0x52 = 0x52
V_0x56 = 0x56
V_0x58 = 0x58
V_0x59 = 0x59
V_0x5A = 0x5A
V_0x5C = 0x5C
V_0x5D = 0x5D
V_0x60 = 0x60

# Grouped stats: the reader's switch cases that read MULTIPLE consecutive value
# fields for one 9-bit id. Each tuple is (id, [extra stat ids read after it]).
# Transcribed from FUN_6fd7a600 cases 0x11,0x30,0x32,0x34,0x36,0x39.
#   0x11 -> 0x12                (mindamage/maxdamage)
#   0x30 -> 0x31                (fire min/max)
#   0x32 -> 0x33                (light min/max)
#   0x34 -> 0x35                (magic min/max)
#   0x36 -> 0x37, 0x38          (cold min/max/len)
#   0x39 -> 0x3a, 0x3b          (poison min/max/len) [+0x3b substat fixup]
STAT_GROUPS = {
    0x11: [0x12],
    0x30: [0x31],
    0x32: [0x33],
    0x34: [0x35],
    0x36: [0x37, 0x38],
    0x39: [0x3A, 0x3B],
}


# ---------------------------------------------------------------------------
# Op journal: every primitive read is recorded so the writer is an exact inverse.
# An op is (kind, nbits, value). kind is purely informational (for debugging).
# ---------------------------------------------------------------------------
@dataclass
class ReadOp:
    kind: str
    nbits: int
    value: int


@dataclass
class ItemStat:
    stat_id: int
    values: list = field(default_factory=list)  # decoded values (grouped -> several)
    bits: list = field(default_factory=list)    # bit width per value field
    adds: list = field(default_factory=list)    # save_add per value field
    # raw value as read off the wire (value + save_add), one per value field, so
    # the structural writer reproduces the exact bits. Index-aligned with
    # `values`/`bits`/`adds`/`leads`.
    raw_values: list = field(default_factory=list)
    # leading params per value field: leads[i] is a list of (nbits, value) pairs
    # emitted BEFORE raw_values[i] (the v103 lead bit and/or save_param block).
    leads: list = field(default_factory=list)
    # --- SPECIAL CASE support (reader FUN_6fd7a600 cases at version <= 0x5c) ---
    # When `special` is set, this stat was read by one of the 48 special-cased
    # switch branches (skill/charge/oskill/aura families) instead of the generic
    # FUN_6fd79d90 path. Those branches read a single fixed-width RAW field off
    # the wire under the ORIGINAL wire id, then DERIVE a (possibly aliased) stat
    # id + decoded value from it (the derivation is lossy — e.g. 0xcc reads 30
    # bits but stores a 16-bit combined value). To round-trip byte-exact the
    # structural writer must re-emit the original wire id (9 bits) followed by the
    # raw field at its original width. `wire_id`/`special_raw`/`special_bits`
    # capture exactly that.
    special: bool = False
    wire_id: int = -1          # the 9-bit stat id as it appeared on the wire
    special_raw: int = 0       # raw fixed-width field value as read
    special_bits: int = 0      # width of that raw field


@dataclass
class Item:
    start_bit: int
    end_bit: int
    # header flags
    identified: bool = False
    socketed: bool = False
    is_new: bool = False
    is_ear: bool = False
    starter: bool = False
    simple: bool = False
    ethereal: bool = False
    personalized: bool = False
    runeword: bool = False
    version: int = 0
    location_id: int = 0
    equipped_id: int = 0
    pos_x: int = 0
    pos_y: int = 0
    panel_id: int = 0
    type_code: str = ""
    ear_class: int = 0
    ear_level: int = 0
    ear_name: str = ""
    # extended
    num_in_sockets: int = 0
    guid: int = 0
    ilvl: int = 0
    quality: int = 0
    graphic: int = -1
    autoprefix: int = -1
    prefix: int = -1
    suffix: int = -1
    set_unique_id: int = -1
    rare_affixes: list = field(default_factory=list)
    runeword_id: int = -1
    personal_name: str = ""
    defense: int = -1
    max_dur: int = -1
    cur_dur: int = -1
    quantity: int = -1
    num_sockets: int = 0
    stats: list = field(default_factory=list)
    extra_lists: list = field(default_factory=list)
    runeword_stats: list = field(default_factory=list)
    # one trailing v103 (0x67) field bit per stat list, in read order
    # (main, set-extras..., runeword). Empty for v101 (no per-list trailer).
    v103_trailers: list = field(default_factory=list)
    set_extra_mask: int = 0
    children: list = field(default_factory=list)
    # --- raw sub-fields captured verbatim so the STRUCTURAL writer reproduces the
    #     exact bitstream (filler flag bits, optional blocks, pad) ---
    flags_raw: dict = field(default_factory=dict)   # name -> value for filler bits
    has_graphic: int = 0
    has_autoprefix: int = 0
    extra_flag: int = -1        # -1 = block absent (version <= 0x56)
    extra_a: int = 0
    extra_b: int = 0
    extra_c: int = -1           # -1 = not present (version <= 0x5d)
    tome_bits: int = -1         # -1 = not a tome
    defense_raw: int = -1       # raw bits as read (before -save_add)
    defense_bits: int = 0
    max_dur_raw: int = -1
    max_dur_bits: int = 0
    cur_dur_raw: int = -1
    cur_dur_bits: int = 0
    quantity_bits: int = 0
    is_armor: bool = False
    is_weapon: bool = False
    is_stackable: bool = False
    pad_bits: int = 0           # number of byte-alignment pad bits
    pad_values: list = field(default_factory=list)  # actual pad bit values (NOT always 0)
    # bookkeeping
    ops: list = field(default_factory=list)  # journaled reads -> exact inverse write
    decode_ok: bool = False
    verbatim: bool = False   # True -> structural writer falls back to raw ops
    next_is_jm: bool = True
    clean: bool = False
    error: str = ""


@dataclass
class ItemList:
    raw: bytes
    items: list = field(default_factory=list)
    count: int = 0
    list_start_bit: int = 0
    resyncs: int = 0


def _stat_width(enc, version: int):
    """(save_bits, save_add) for a stat at this item version.

    The reader reads row[0x19 + uVar11] / row[0x1c + uVar11*4] from the IN-MEMORY
    ItemStatCost struct, uVar11 = (version < 0x5d). For all modern PD2 saves
    (v101=0x65, v103=0x67 -> both >= 0x5d) uVar11 == 0 -> the struct's primary
    'Save Bits'/'Save Add' fields (offset 0x19 / 0x1c).

    PD2 loaded its 'Save Bits S12'/'Save Add S12' overrides into those struct
    fields for SEASON-12 (v101=0x65) saves. For SEASON-13+ (v103=0x67) PD2
    REVERTED to the vanilla 'Save Bits'/'Save Add' columns. So the in-memory
    width the reader sees depends on the item version:
      v101 (0x65) -> S12 columns        (enc.save_bits / save_add)
      v103 (0x67) -> vanilla columns    (enc.save_bits_base / save_add_base)
    Verified empirically against the corpus: berserk (v101) needs S12 (stat 22
    maxdamage = 10 bits); LadyKiller (v103) needs vanilla and decodes uniques to
    their exact UniqueItems.txt values. (Both are >= 0x5d so col-19 is never used.)
    """
    if version >= 0x67 and enc.save_bits_base:
        return enc.save_bits_base, enc.save_add_base
    return enc.save_bits, enc.save_add


def _stat_param_bits(enc, version: int):
    """save_param_bits — row[0x24] leading bits. Same version split as _stat_width."""
    if version >= 0x67 and enc.save_bits_base:
        return getattr(enc, "save_param_bits_base", 0) or 0
    return getattr(enc, "save_param_bits", 0) or 0


# ===========================================================================
# Journaling reader wrapper
# ===========================================================================
class _Rec:
    """Wraps a BitReader and journals every read into `ops`."""

    def __init__(self, br: BitReader, ops: list):
        self.br = br
        self.ops = ops

    def read(self, nbits: int, kind: str = "") -> int:
        v = self.br.read(nbits)
        self.ops.append(ReadOp(kind, nbits, v))
        return v

    @property
    def bit_pos(self) -> int:
        return self.br.bit_pos


def _find_next_boundary(data: bytes, start_byte: int) -> int:
    """Byte offset of the next item/stash boundary after the item at start_byte.
    Items are byte-aligned and begin with 'JM'; stash pages with 'ST'. Scans from
    start_byte+2 (past this item's own marker)."""
    i = start_byte + 2
    while i + 1 < len(data):
        if data[i:i + 2] in (ITEM_MARKER, b"ST"):
            return i
        i += 1
    return len(data)


# ===========================================================================
# Reader — parse_one_item
# ===========================================================================
def parse_one_item(data: bytes, bit: int, stat_table):
    """Parse a single item starting at absolute bit `bit` (its 'JM' marker).
    Returns (Item, next_bit). Faithful to FUN_6fd7a600.
    """
    br = BitReader(data, bit)
    item = Item(start_bit=bit, end_bit=bit)
    r = _Rec(br, item.ops)

    # --- 'JM' marker (16 bits, byte-aligned) ---
    r.read(8, "marker_J")
    r.read(8, "marker_M")

    try:
        _parse_body(r, item, stat_table)
        item.decode_ok = True
    except Exception as e:  # noqa: BLE001
        item.error = repr(e)
        item.decode_ok = False

    structured_ok = False
    if item.decode_ok:
        # byte-align to next item start (items are byte-aligned in the list)
        end_bit = (br.bit_pos + 7) & ~7
        # provisional: validate the structured parse landed on a real boundary
        nb = end_bit // 8
        if nb + 2 <= len(data):
            nxt_ok = data[nb:nb + 2] in (ITEM_MARKER, b"ST")
        else:
            nxt_ok = True
        if nxt_ok:
            structured_ok = True
            pad = end_bit - br.bit_pos
            item.pad_bits = pad
            if pad:
                for _ in range(pad):
                    b = br.read_bit()
                    item.pad_values.append(b)
                    item.ops.append(ReadOp("pad", 1, b))
            item.end_bit = end_bit

    if not structured_ok:
        # Structured decode failed OR ended mid-record. Round-trip MUST stay
        # byte-exact, so fall back to verbatim: discard the partial journal and
        # re-capture the entire raw span ('JM' -> next item/stash boundary).
        item.decode_ok = item.decode_ok and structured_ok
        item.verbatim = True
        end_byte = _find_next_boundary(data, bit // 8)
        end_bit = end_byte * 8
        item.ops = []
        br2 = BitReader(data, bit)
        for _ in range(end_bit - bit):
            item.ops.append(ReadOp("raw", 1, br2.read_bit()))
        item.end_bit = end_bit
        br.pos = end_bit

    nb = item.end_bit // 8
    if nb + 2 <= len(data):
        item.next_is_jm = data[nb:nb + 2] in (ITEM_MARKER, b"ST")
    else:
        item.next_is_jm = True
    item.clean = structured_ok and item.next_is_jm
    return item, item.end_bit


def _parse_body(r: _Rec, item: Item, stat_table) -> None:
    """Flag header + (for non-simple, non-ear) the extended block + stat list."""
    # --- flag/header block (reader lines 126-161; matches d2 flag layout) ---
    # The 32-bit flag word is read field-by-field LSB-first. Filler chunks are
    # captured into flags_raw so the structural writer reproduces them exactly.
    fr = item.flags_raw
    fr["flags_lo"] = r.read(4, "flags_lo")
    item.identified = bool(r.read(1, "identified"))
    fr["flags2"] = r.read(6, "flags2")
    item.socketed = bool(r.read(1, "socketed"))
    fr["flags3"] = r.read(1, "flags3")
    item.is_new = bool(r.read(1, "is_new"))
    fr["flags4"] = r.read(2, "flags4")
    item.is_ear = bool(r.read(1, "is_ear"))
    item.starter = bool(r.read(1, "starter"))
    fr["flags5"] = r.read(3, "flags5")
    item.simple = bool(r.read(1, "simple"))
    item.ethereal = bool(r.read(1, "ethereal"))
    fr["flags6"] = r.read(1, "flags6")
    item.personalized = bool(r.read(1, "personalized"))
    fr["flags7"] = r.read(1, "flags7")
    item.runeword = bool(r.read(1, "runeword"))
    fr["flags8"] = r.read(5, "flags8")
    item.version = r.read(8, "version")
    fr["flags_pad"] = r.read(2, "flags_pad")
    item.location_id = r.read(3, "location")
    item.equipped_id = r.read(4, "equipped")
    item.pos_x = r.read(4, "pos_x")
    item.pos_y = r.read(4, "pos_y")
    item.panel_id = r.read(3, "panel")

    if item.is_ear:
        item.ear_class = r.read(3, "ear_class")
        item.ear_level = r.read(7, "ear_level")
        name = []
        for _ in range(16):
            c = r.read(7, "ear_ch")
            if c == 0:
                break
            name.append(c)
        item.ear_name = bytes(name).decode("latin-1", "replace")
        item.type_code = "ear"
        return

    # --- type code: 4 ASCII chars (space-padded) ---
    code = bytearray()
    for _ in range(4):
        c = r.read(8, "code")
        code.append(c)
    item.type_code = bytes(code).rstrip(b" \x00").decode("latin-1", "replace")
    # filled-socket count (ALL items)
    item.num_in_sockets = r.read(3, "num_in_sockets")

    if item.simple:
        return

    _parse_extended(r, item, stat_table)


def _parse_extended(r: _Rec, item: Item, stat_table) -> None:
    v = item.version
    item.guid = r.read(32, "guid")
    item.ilvl = r.read(7, "ilvl")
    item.quality = r.read(4, "quality")

    item.has_graphic = r.read(1, "has_graphic")
    if item.has_graphic:
        item.graphic = r.read(3, "graphic")
    item.has_autoprefix = r.read(1, "has_autoprefix")
    if item.has_autoprefix:
        item.autoprefix = r.read(11, "autoprefix")

    q = item.quality
    if q in (Q_INFERIOR, Q_SUPERIOR):
        item.prefix = r.read(3, "lowquality_id")
    elif q == Q_MAGIC:
        item.prefix = r.read(11, "magic_prefix")
        item.suffix = r.read(11, "magic_suffix")
    elif q in (Q_SET, Q_UNIQUE):
        item.set_unique_id = r.read(12, "set_unique_id")
    elif q in (Q_RARE, Q_CRAFTED):
        item.prefix = r.read(8, "rare_name1")
        item.suffix = r.read(8, "rare_name2")
        item.rare_affixes = []
        for _ in range(6):
            if r.read(1, "affix_present"):
                item.rare_affixes.append(r.read(11, "affix"))
            else:
                item.rare_affixes.append(None)

    # runeword (flag bit 0x4000000): 16-bit runeword id
    if item.runeword:
        item.runeword_id = r.read(16, "runeword_id")

    # personalization name (flag bit 0x1000000): 7-bit chars, null-terminated
    if item.personalized:
        name = []
        while True:
            ch = r.read(7, "pers_ch")
            if ch == 0:
                break
            name.append(ch)
        item.personal_name = bytes(name).decode("latin-1", "replace")

    # --- tome (ibk/tbk) : 5 extra bits, after personalization ---
    if item.type_code in ("ibk", "tbk"):
        item.tome_bits = r.read(5, "tome_bits")

    # --- v101/v103 STRUCTURAL DELTA (reader lines 406-417) ---
    # Gated: item extended AND version > 0x56 AND a 1-bit flag is set.
    # Reads two 32-bit values, plus a THIRD when version > 0x5d (v103).
    # (This is the timestamp/extra-field block; writer mirrors at 36199-36212.)
    if v > V_0x56:
        item.extra_flag = r.read(1, "extra_flag")
        if item.extra_flag:
            item.extra_a = r.read(32, "extra_a")
            item.extra_b = r.read(32, "extra_b")
            if v > V_0x5D:
                item.extra_c = r.read(32, "extra_c")

    sch = getattr(stat_table, "schema", None)
    code = item.type_code
    item.is_armor = bool(sch) and code in sch.armor_codes
    item.is_weapon = bool(sch) and code in sch.weapon_codes
    item.is_stackable = bool(sch) and code in sch.stackable_codes

    # --- defense (armor) : 11-bit minus save_add of stat 0x1f (armorclass) ---
    if item.is_armor:
        enc = stat_table.get(0x1F)
        sb, sa = _stat_width(enc, v) if enc else (11, 10)
        item.defense_bits = sb
        item.defense_raw = r.read(sb, "defense")
        item.defense = item.defense_raw - sa

    # --- durability (armor or weapon) : stat 0x49 max, 0x48 cur ---
    if item.is_armor or item.is_weapon:
        enc_max = stat_table.get(0x49)
        mb, ma = _stat_width(enc_max, v) if enc_max else (8, 0)
        item.max_dur_bits = mb
        item.max_dur_raw = r.read(mb, "max_dur")
        item.max_dur = item.max_dur_raw - ma
        if item.max_dur != 0:
            enc_cur = stat_table.get(0x48)
            cb, ca = _stat_width(enc_cur, v) if enc_cur else (8, 0)
            # reader line 530/581: < 0x60 forces 8-bit width for current dur
            if v < V_0x60:
                cb = 8
            item.cur_dur_bits = cb
            item.cur_dur_raw = r.read(cb, "cur_dur")
            item.cur_dur = item.cur_dur_raw - ca

    # --- quantity (stackable) : stat 0x46, 9 bits (8 if version < 0x51) ---
    if item.is_stackable:
        qb = 8 if v < V_0x51 else 9
        item.quantity_bits = qb
        item.quantity = r.read(qb, "quantity")

    # --- sockets (flag 0x800) : 4-bit count ---
    # Reader line 608-616 reads via a fixed ItemStatCost row (item_numsockets =
    # 4 save bits). This is NOT stat id 0xd4 (which is an unrelated PD2 stat).
    if item.socketed:
        item.num_sockets = r.read(4, "num_sockets")

    # --- set items: 5-bit bitfield of which extra property lists follow ---
    set_extra = 0
    if q == Q_SET:
        set_extra = r.read(5, "set_extra_lists")
    item.set_extra_mask = set_extra

    # --- property/stat lists ---
    # The reader's outer multi-list loop emits, for v103 (0x67), a single trailing
    # bit AFTER each stat list (before the next list / byte-alignment). It is
    # usually 1 but can be 0, so it is a genuine per-list data field, not zero pad.
    # v101 (0x65) has no such field — its post-list pad is pure byte-alignment.
    # Lists read, in order: the MAIN list, then one list per set-extra bit, then
    # (when the runeword flag is set) the runeword's own modifier list. Each is
    # followed by its v103 trailer bit. Reading these explicitly is what lets the
    # rare charms (hbl/xtg), magic charms (cm3) and the normal runeword (9ar) land
    # byte-exact with sane decoded values.
    item.v103_trailers = []

    def _read_list_with_trailer():
        lst = _read_stat_list(r, stat_table, v)
        if v >= 0x67:
            item.v103_trailers.append(r.read(1, "v103_trailer"))
        return lst

    item.stats = _read_list_with_trailer()
    item.extra_lists = []
    for _ in range(_popcount(set_extra)):
        item.extra_lists.append(_read_list_with_trailer())
    if item.runeword:
        item.runeword_stats = _read_list_with_trailer()


def _popcount(x: int) -> int:
    return bin(x).count("1")


def _read_one_value(r: _Rec, enc, version: int, st: ItemStat, kind: str,
                    is_group_sub: bool = False) -> int:
    """Read one stat value field, mirroring the generic stat reader FUN_6fd79d90:

        iVar1 = save_param_bits (struct col 0x24+uVar11*4)
        if iVar1 > 0:  Ordinal_10130(iVar1)        # leading param read
        value = Ordinal_10130(save_bits)           # struct col 0x19+uVar11
        store value - save_add

    For v101 (item version 0x65, uVar11=0) PD2's in-memory struct holds the
    'Save Bits S12'/'Save Add S12'/'Save Param Bits S12' columns; some S12 param
    widths are > 0, so the leading param read fires (verified: 108 -> 131 clean on
    berserk). For v103 (0x67) the struct holds the vanilla columns AND PD2 added a
    fixed 1-bit per-stat-value leading flag (the bit is always 0 in saved items,
    but is genuinely consumed; without it the 9-bit stat ids desync immediately,
    and WITH it uniques decode to their exact UniqueItems values, e.g. Duskdeep
    unique 74 -> light -2, res-all 15, ac% etc). Both the v103 flag and the param
    bits are captured per value field in `st.leads` so the writer reproduces them.

    GROUPED SUB-STATS: the reader's grouped switch cases (0x11/0x30/0x32/0x34/
    0x36/0x39) read the leader via the generic path (which fires the v103 flag /
    param) but read the trailing group members with a DIRECT Ordinal_10130 of the
    value bits — no leading flag or param. `is_group_sub` selects that path so the
    group members consume only their value bits (verified: cm3 poison/fire charms
    decode to sane min/max/len and land byte-exact only with the lead suppressed).
    """
    sb, sa = _stat_width(enc, version)
    lead = []  # list of (nbits, value) emitted before the value field
    if not is_group_sub:
        if version >= 0x67:
            lead.append((1, r.read(1, kind + "_v103flag")))
        spb = _stat_param_bits(enc, version)
        if spb > 0:
            lead.append((spb, r.read(spb, kind + "_param")))
    raw = r.read(sb, kind)
    st.leads.append(lead)
    st.values.append(raw - sa)
    st.raw_values.append(raw)
    st.bits.append(sb)
    st.adds.append(sa)
    return raw - sa


# ---------------------------------------------------------------------------
# Special-cased switch branches (reader FUN_6fd7a600 lines 905-1087).
#
# These 48 stat ids are read by dedicated branches INSTEAD of the generic
# FUN_6fd79d90 path, but ONLY when the item version is <= 0x5c. For version
# > 0x5c (every modern PD2 save, incl. the v0x65 corpus) every one of these ids
# falls through to FUN_6fd79d90 (LAB_6fd7bd8d / switchD_caseD_4 at C lines
# 732-735) — i.e. the ordinary generic path. So the special branches below are
# the faithful transcription for legacy (<= 0x5c) items; modern items never take
# them. SPECIAL_GATE maps each wire id to (gate_version, reader_fn). reader_fn
# returns (alias_stat_id, decoded_value, raw_field, raw_bits).
# ---------------------------------------------------------------------------

def _sp_skill3(r, version):
    """0x53,0x54,0x55,0x56,0x57,0xb3,0xb4: 3-bit value, aliased to stat 0x53.
    (C lines 905-949, 1011-1028.) 0x53 keeps its own id; the rest store 0x53."""
    raw = r.read(3, "sp_skill3")
    return 0x53, raw, raw, 3


def _sp_singleskill(r, version):
    """0x6b-0x6d, 0xb5-0xbb: 14-bit packed, skill id = bits[9:14]; stored as 0x6b.
    (C lines 950-974.)"""
    raw = r.read(14, "sp_singleskill")
    skill_id = (raw >> 9) & 0x1F
    return 0x6B, skill_id, raw, 14


def _sp_elemskill(r, version):
    """0x7e: 4-bit value. (C lines 975-983.)"""
    raw = r.read(4, "sp_elemskill")
    return 0x7E, raw, raw, 4


def _sp_freeze(r, version):
    """0x86: 16-bit value, consumed and discarded (no stat stored). (C 984-1010.)
    Stored with alias id 0x86 but value 0 — the structural writer re-emits the
    raw 16 bits under wire id 0x86 to stay byte-exact."""
    raw = r.read(16, "sp_freeze")
    return 0x86, 0, raw, 16


def _sp_charges(r, version):
    """0xbc-0xc1: 10-bit packed -> FUN_6fd95780 (charges = bits[5:10], clamped to
    7); stored as 0xbc. (C lines 1029-1043.)"""
    raw = r.read(10, "sp_charges")
    charges = (raw >> 5) & 0x1F
    if charges > 7:
        charges = 7
    return 0xBC, charges, raw, 10


def _sp_c3(r, version):
    """0xc3-0xc5: 21-bit packed, level = bits[14:21] (& 0x7f); stored as 0xc3.
    (C lines 1044-1051.)"""
    raw = r.read(21, "sp_c3")
    return 0xC3, (raw >> 0xE) & 0x7F, raw, 21


def _sp_c6(r, version):
    """0xc6-0xc8: 21-bit packed, level = bits[14:21]; stored as 0xc6.
    (C lines 1052-1059.)"""
    raw = r.read(21, "sp_c6")
    return 0xC6, (raw >> 0xE) & 0x7F, raw, 21


def _sp_c9(r, version):
    """0xc9-0xcb: 21-bit packed, level = bits[14:21]; stored as 0xc9.
    (C lines 1060-1067.)"""
    raw = r.read(21, "sp_c9")
    return 0xC9, (raw >> 0xE) & 0x7F, raw, 21


def _sp_cc(r, version):
    """0xcc-0xd5: 29-bit (version < 0x4a) or 30-bit packed; combined value =
    (bits[22:30] << 8) | bits[14:22]; stored as 0xcc. (C lines 1068-1087.)"""
    nbits = 0x1D if version < V_0x4A else 0x1E
    raw = r.read(nbits, "sp_cc")
    combined = ((raw >> 0x16) & 0xFF) * 0x100 + ((raw >> 0xE) & 0xFF)
    return 0xCC, combined, raw, nbits


def _build_special_gate():
    g = {}
    for sid in (0x53, 0x54, 0x55, 0x56, 0x57, 0xB3, 0xB4):
        g[sid] = (V_0x5C, _sp_skill3)
    for sid in (0x6B, 0x6C, 0x6D, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xBB):
        g[sid] = (V_0x5C, _sp_singleskill)
    g[0x7E] = (V_0x5C, _sp_elemskill)
    g[0x86] = (V_0x59, _sp_freeze)
    for sid in range(0xBC, 0xC2):
        g[sid] = (V_0x5C, _sp_charges)
    for sid in (0xC3, 0xC4, 0xC5):
        g[sid] = (V_0x5C, _sp_c3)
    for sid in (0xC6, 0xC7, 0xC8):
        g[sid] = (V_0x5C, _sp_c6)
    for sid in (0xC9, 0xCA, 0xCB):
        g[sid] = (V_0x5C, _sp_c9)
    for sid in range(0xCC, 0xD6):
        g[sid] = (V_0x5C, _sp_cc)
    return g


# wire_id -> (gate_version, reader_fn). The special branch fires only when the
# item version is <= gate_version; otherwise the id uses the generic path.
SPECIAL_GATE = _build_special_gate()


def _read_stat_list(r: _Rec, stat_table, version: int) -> list:
    """Read a property list: 9-bit ids until 0x1FF. Faithful to FUN_6fd7a600's
    stat-list loop (lines 675-1091). Grouped damage families read multiple value
    fields per id; the 48 special-cased skill/charge/oskill/aura ids take their
    dedicated fixed-width branches when version <= their gate; every other id
    (and all of the above on version > gate) reads one value via the generic
    path."""
    stats = []
    while True:
        sid = r.read(9, "stat_id")
        if sid == TERM:
            break

        # --- 48 special-cased branches (only for version <= gate) ---
        gate = SPECIAL_GATE.get(sid)
        if gate is not None and version <= gate[0]:
            alias_id, value, raw_field, raw_bits = gate[1](r, version)
            st = ItemStat(stat_id=alias_id)
            st.special = True
            st.wire_id = sid
            st.special_raw = raw_field
            st.special_bits = raw_bits
            st.values.append(value)
            st.bits.append(raw_bits)
            stats.append(st)
            continue

        enc = stat_table.get(sid)
        if enc is None:
            raise ValueError(f"unknown stat id {sid} (not in ItemStatCost)")
        sb, _sa = _stat_width(enc, version)
        if sb == 0:
            raise ValueError(f"stat {sid} has save_bits=0")

        st = ItemStat(stat_id=sid)
        _read_one_value(r, enc, version, st, f"stat{sid}")

        # grouped stats: read the additional consecutive value fields. These are
        # group MEMBERS — the reader reads them with a direct value read (no v103
        # leading flag / param), unlike the group leader above.
        for extra_id in STAT_GROUPS.get(sid, []):
            enc2 = stat_table.get(extra_id)
            if enc2 is None:
                raise ValueError(f"unknown grouped stat id {extra_id}")
            _read_one_value(r, enc2, version, st, f"stat{extra_id}",
                            is_group_sub=True)

        stats.append(st)
    return stats


# ===========================================================================
# Writer — serialize_item (exact inverse, replays the op journal)
# ===========================================================================
def serialize_item(item: Item) -> list:
    """Return the list of bits for this item by replaying its read journal.

    The journal is a faithful record of every primitive read in field order, so
    replaying it reproduces the exact bitstream. Editing path: callers mutate
    typed fields then call `rebuild_ops` to regenerate the journal from struct;
    for the round-trip gate we replay the captured journal verbatim-by-field.
    """
    bw = BitWriter()
    for op in item.ops:
        bw.write(op.value, op.nbits)
    return bw.bits


def serialize_item_struct(item: Item, stat_table) -> list:
    """TRUE inverse of the reader: emit the item's bitstream from DECODED FIELDS
    (not the journal). Returns a list of bits. For items decoded verbatim (the
    structured parse failed / landed off-boundary) it emits the captured raw ops.
    This is the function the round-trip gate compares against the original bits.
    """
    if item.verbatim or not item.decode_ok:
        return ops_to_bits(item.ops)

    bw = BitWriter()
    w = bw.write
    fr = item.flags_raw

    # 'JM' marker
    w(ord("J"), 8)
    w(ord("M"), 8)

    # flag/header block (exact reader order)
    w(fr["flags_lo"], 4)
    w(1 if item.identified else 0, 1)
    w(fr["flags2"], 6)
    w(1 if item.socketed else 0, 1)
    w(fr["flags3"], 1)
    w(1 if item.is_new else 0, 1)
    w(fr["flags4"], 2)
    w(1 if item.is_ear else 0, 1)
    w(1 if item.starter else 0, 1)
    w(fr["flags5"], 3)
    w(1 if item.simple else 0, 1)
    w(1 if item.ethereal else 0, 1)
    w(fr["flags6"], 1)
    w(1 if item.personalized else 0, 1)
    w(fr["flags7"], 1)
    w(1 if item.runeword else 0, 1)
    w(fr["flags8"], 5)
    w(item.version, 8)
    w(fr["flags_pad"], 2)
    w(item.location_id, 3)
    w(item.equipped_id, 4)
    w(item.pos_x, 4)
    w(item.pos_y, 4)
    w(item.panel_id, 3)

    if item.is_ear:
        w(item.ear_class, 3)
        w(item.ear_level, 7)
        nm = item.ear_name.encode("latin-1", "replace")
        for ch in nm[:15]:
            w(ch, 7)
        w(0, 7)  # null terminator
        _emit_pad(bw, item)
        return bw.bits

    # type code (4 chars, space-padded)
    code = item.type_code.encode("latin-1", "replace")
    code = code + b" " * (4 - len(code)) if len(code) < 4 else code[:4]
    for ch in code:
        w(ch, 8)
    w(item.num_in_sockets, 3)

    if item.simple:
        _emit_pad(bw, item)
        return bw.bits

    _emit_extended(bw, item, stat_table)
    _emit_pad(bw, item)
    return bw.bits


def _emit_pad(bw: BitWriter, item: Item) -> None:
    # Byte-align the item to the next byte boundary. Items in the list are byte
    # aligned, so the number of pad bits is determined by the CURRENT bit position,
    # NOT by the original item's stored pad count. Recomputing here is what makes
    # synthesized/edited items byte-correct: if the content length changed (e.g.
    # different affix or added stat), the pad count must change with it. Copying
    # item.pad_values verbatim (the old behaviour) over-padded edited items by up
    # to a full byte, desyncing the game's reader -> "bad inventory data".
    #
    # Genuine non-zero leftover bits (the v103 per-stat-list trailer) are emitted
    # by the stat-list writer itself, BEFORE this call, so by the time we get here
    # only pure byte-alignment fill (zeros) remains.
    n = (-bw.bit_len) % 8
    for _ in range(n):
        bw.write(0, 1)


def _emit_extended(bw: BitWriter, item: Item, stat_table) -> None:
    v = item.version
    w = bw.write
    w(item.guid, 32)
    w(item.ilvl, 7)
    w(item.quality, 4)

    w(item.has_graphic, 1)
    if item.has_graphic:
        w(item.graphic, 3)
    w(item.has_autoprefix, 1)
    if item.has_autoprefix:
        w(item.autoprefix, 11)

    q = item.quality
    if q in (Q_INFERIOR, Q_SUPERIOR):
        w(item.prefix, 3)
    elif q == Q_MAGIC:
        w(item.prefix, 11)
        w(item.suffix, 11)
    elif q in (Q_SET, Q_UNIQUE):
        w(item.set_unique_id, 12)
    elif q in (Q_RARE, Q_CRAFTED):
        w(item.prefix, 8)
        w(item.suffix, 8)
        for a in item.rare_affixes:
            if a is None:
                w(0, 1)
            else:
                w(1, 1)
                w(a, 11)

    if item.runeword:
        w(item.runeword_id, 16)

    if item.personalized:
        nm = item.personal_name.encode("latin-1", "replace")
        for ch in nm:
            w(ch, 7)
        w(0, 7)

    if item.tome_bits >= 0:
        w(item.tome_bits, 5)

    if item.extra_flag >= 0:
        w(item.extra_flag, 1)
        if item.extra_flag:
            w(item.extra_a, 32)
            w(item.extra_b, 32)
            if item.extra_c >= 0:
                w(item.extra_c, 32)

    if item.is_armor:
        w(item.defense_raw, item.defense_bits)

    if item.is_armor or item.is_weapon:
        w(item.max_dur_raw, item.max_dur_bits)
        if item.max_dur != 0:
            w(item.cur_dur_raw, item.cur_dur_bits)

    if item.is_stackable:
        w(item.quantity, item.quantity_bits)

    if item.socketed:
        w(item.num_sockets, 4)

    if q == Q_SET:
        w(item.set_extra_mask, 5)

    # Emit each stat list followed by its per-list v103 trailer bit (read order:
    # main, set-extras..., runeword). For v101 there are no trailers.
    trailers = iter(item.v103_trailers)

    def _emit_list_with_trailer(stats):
        _emit_stat_list(bw, stats)
        t = next(trailers, None)
        if t is not None:
            bw.write(t, 1)

    _emit_list_with_trailer(item.stats)
    for lst in item.extra_lists:
        _emit_list_with_trailer(lst)
    if item.runeword:
        _emit_list_with_trailer(item.runeword_stats)


def _emit_stat_list(bw: BitWriter, stats: list) -> None:
    w = bw.write
    for st in stats:
        # Special-cased branches: re-emit the ORIGINAL wire id (9 bits) followed
        # by the single fixed-width raw field exactly as read (the decoded value
        # is a lossy derivation, so we cannot reconstruct from it). This is the
        # exact inverse of the version<=gate switch branches in _read_stat_list.
        if st.special:
            w(st.wire_id, 9)
            w(st.special_raw, st.special_bits)
            continue
        w(st.stat_id, 9)
        for i in range(len(st.values)):
            for nbits, pv in st.leads[i]:
                w(pv, nbits)
            # Emit from the EDITABLE decoded value (value + save_add), masked to
            # the field width, so stat edits propagate. For unedited items this is
            # identical to the raw bits read off the wire (invariant verified:
            # raw_values[i] == values[i] + adds[i] across the whole corpus).
            nbits = st.bits[i]
            raw = (st.values[i] + st.adds[i]) & ((1 << nbits) - 1)
            w(raw, nbits)
    w(TERM, 9)


def ops_to_bits(ops: list) -> list:
    bw = BitWriter()
    for op in ops:
        bw.write(op.value, op.nbits)
    return bw.bits


def serialize_item_typed(item: Item, stat_table) -> list:
    """Alias for serialize_item_struct — re-emit from the DECODED struct (the true
    inverse of the reader, used by the editor after mutating fields)."""
    return serialize_item_struct(item, stat_table)


# ===========================================================================
# List-level parse / serialize
# ===========================================================================
def parse_item_list(data: bytes, offset: int, stat_table) -> ItemList:
    """Parse the item list at `offset` (points at 'JM' list header):
    'JM' + uint16 count, then `count` top-level items (each starting 'JM'),
    each possibly followed by num_in_sockets child items."""
    assert data[offset:offset + 2] == ITEM_MARKER, "expected JM list header"
    count = int.from_bytes(data[offset + 2:offset + 4], "little")
    il = ItemList(raw=data, count=count, list_start_bit=(offset + 4) * 8)

    bit = (offset + 4) * 8
    for _ in range(count):
        if data[bit // 8:bit // 8 + 2] != ITEM_MARKER:
            nb = data.find(ITEM_MARKER, bit // 8)
            if nb < 0:
                break
            bit = nb * 8
            il.resyncs += 1
        item, bit = parse_one_item(data, bit, stat_table)
        il.items.append(item)
        for _c in range(item.num_in_sockets):
            if data[bit // 8:bit // 8 + 2] != ITEM_MARKER:
                break
            child, bit = parse_one_item(data, bit, stat_table)
            item.children.append(child)
    return il


def serialize_item_list(il: ItemList, data: bytes, stat_table=None) -> bytes:
    """Re-emit the item-list region from parsed items.

    If `stat_table` is given, every item is re-emitted STRUCTURALLY from its
    decoded fields (serialize_item_struct) — the true inverse of the reader and
    the byte-exact round-trip gate. Without it, falls back to the op-journal
    replay (used only as a debugging aid)."""
    if not il.items:
        # preserve header even with zero items
        hdr_start = (il.list_start_bit // 8) - 4
        return bytes(data[hdr_start:il.list_start_bit // 8])
    first = il.items[0].start_bit // 8
    header = data[(il.list_start_bit // 8) - 4:first]
    bw = BitWriter()

    def emit(it: Item):
        if stat_table is not None:
            bw.bits.extend(serialize_item_struct(it, stat_table))
        else:
            for op in it.ops:
                bw.write(op.value, op.nbits)
        for ch in it.children:
            emit(ch)

    for it in il.items:
        emit(it)
    return bytes(header) + bw.to_bytes()
