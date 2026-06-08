"""
Editing engine — in-place bit patching of item stat values.

Strategy (safe-by-construction): we never re-serialize whole items from parsed
fields (that risks drift). Instead we patch the value field of a stat IN PLACE
in the save bytes, at the exact bit offset recorded during parse, then recompute
the .d2s checksum. Everything not edited stays byte-identical.

Editable stats: simple/encode-0 stats with a recorded value_bit_offset (the
common case — resistances, +life/mana, +attributes, %ED-on-some, MF, FRW, etc.).
Grouped/skill/encode-2/3 stats are not in-place editable yet (offset == -1).

set_stat_value() / max_roll() operate on a mutable bytearray of the whole save.
"""
from __future__ import annotations

from .d2s import D2SChar, compute_checksum_d2
from .tables import GameTables


def _write_bits_le(buf: bytearray, bit_offset: int, value: int, nbits: int) -> None:
    """Write `value` as nbits, LSB-first, into buf at absolute bit_offset."""
    for i in range(nbits):
        byte_i = (bit_offset + i) >> 3
        bit_i = (bit_offset + i) & 7
        if (value >> i) & 1:
            buf[byte_i] |= (1 << bit_i)
        else:
            buf[byte_i] &= ~(1 << bit_i) & 0xFF


def set_stat_value(save_bytes: bytearray, stat, new_value: int) -> None:
    """Patch one stat's value in place. `stat` is a parsed ItemStat with a valid
    value_bit_offset. new_value is the DISPLAY value (save_add is re-applied)."""
    if stat.value_bit_offset < 0:
        raise ValueError(f"stat {stat.stat_id} is not in-place editable")
    raw = new_value + stat.save_add
    maxraw = (1 << stat.value_bits) - 1
    if raw < 0 or raw > maxraw:
        raise ValueError(f"value {new_value} out of range for stat {stat.stat_id} "
                         f"({stat.value_bits} bits, add {stat.save_add})")
    _write_bits_le(save_bytes, stat.value_bit_offset, raw, stat.value_bits)


def finalize_d2s(save_bytes: bytearray) -> None:
    """Recompute size + checksum after edits (for .d2s character files)."""
    import struct
    struct.pack_into("<I", save_bytes, 0x08, len(save_bytes))
    cs = compute_checksum_d2(bytes(save_bytes))
    struct.pack_into("<I", save_bytes, 0x0C, cs)


def max_roll_item(save_bytes: bytearray, item, stat_table, gt: GameTables,
                  legit: bool = True) -> int:
    """Set every in-place-editable stat on `item` to its max value.
    legit=True (default): clamp to the highest LEGITIMATE affix value for that stat
    (from MagicPrefix/MagicSuffix ranges) so items look like real god-rolls, not
    obviously-hacked bit-ceiling junk. legit=False: raw bit ceiling.
    Returns count of stats maxed. Caller must finalize_d2s() afterwards."""
    affix_max = gt.build_affix_max() if legit else {}
    n = 0
    for s in item.stats:
        if s.value_bit_offset < 0 or s.value_bits <= 1:
            continue
        bit_ceiling = (1 << s.value_bits) - 1 - s.save_add
        enc = gt.stat_by_id.get(s.stat_id)
        target = bit_ceiling
        if legit and enc is not None:
            legit_max = affix_max.get(enc.name)
            if legit_max is not None and legit_max > 0:
                target = min(legit_max, bit_ceiling)
        # only raise, never lower an already-higher legit roll
        if target <= s.value:
            continue
        try:
            set_stat_value(save_bytes, s, target)
            n += 1
        except ValueError:
            pass
    return n
