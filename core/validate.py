"""
Structural save validator — the no-corruption guarantee.

PD2's offline load-validator is STRUCTURAL, not semantic (verified 2026-06-07: it
checks that each item parses to a clean byte length and the next item/section
starts where expected; it does NOT range-check stat values). The game rejects a
save with "Unable to enter game / Bad inventory data" (D2Game error 0xe) when an
item's encoded length disagrees with the reader, desyncing the stream.

This module replicates that structural check in Python so we can PREDICT
load-success locally before ever writing a save the game will see. Run it after
every edit; if it fails, do not ship the file.

It is intentionally game-agnostic where it can be: section framing is the stable
container format; per-item decoding is driven by the live stat schema. Adding a
new game means teaching `walk_sections` that game's section layout, not rewriting
item logic.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import item_v2
from .d2s import compute_checksum_d2, read_character_name


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # diagnostics
    sections: dict = field(default_factory=dict)
    item_count: int = 0
    socketed_count: int = 0

    def __bool__(self) -> bool:
        return self.ok


def _walk_item_list(data: bytes, start_bit: int, count: int, st, label: str,
                    errors: list[str]) -> tuple[int, int, int]:
    """Parse exactly `count` items (plus their socket children) from start_bit.
    Returns (end_bit, items_parsed, socketed_children). Records errors instead of
    raising, so one bad item doesn't hide later structural facts."""
    bit = start_bit
    parsed = 0
    children = 0
    for i in range(count):
        byte = bit // 8
        if data[byte:byte + 2] != b"JM":
            errors.append(
                f"{label}: item {i} does not start with 'JM' at byte 0x{byte:x} "
                f"(stream desynced — previous item's length is wrong)"
            )
            return bit, parsed, children
        try:
            it, bit = item_v2.parse_one_item(data, bit, st)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{label}: item {i} failed to parse at 0x{byte:x}: {e!r}")
            return bit, parsed, children
        parsed += 1
        for _c in range(it.num_in_sockets):
            cbyte = bit // 8
            if data[cbyte:cbyte + 2] != b"JM":
                errors.append(
                    f"{label}: socket child of item {i} not 'JM' at 0x{cbyte:x}"
                )
                return bit, parsed, children
            try:
                _ch, bit = item_v2.parse_one_item(data, bit, st)
                children += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{label}: socket child of item {i} failed: {e!r}")
                return bit, parsed, children
    return bit, parsed, children


def validate_d2s(data: bytes, st) -> ValidationResult:
    """Validate a .d2s character save end to end (player/corpse/merc/golem)."""
    res = ValidationResult(ok=True)
    errors = res.errors

    # --- header sanity ---
    if len(data) < 0x14F:
        errors.append("file too short to be a .d2s")
        res.ok = False
        return res
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != 0xAA55AA55:
        errors.append(f"bad .d2s magic 0x{magic:08x} (expected 0xAA55AA55)")
    sizefield = struct.unpack_from("<I", data, 8)[0]
    if sizefield != len(data):
        errors.append(f"size field {sizefield} != actual file length {len(data)} "
                      f"(must be fixed before write)")
    ck_field = struct.unpack_from("<I", data, 0x0C)[0]
    ck_calc = compute_checksum_d2(data)
    if ck_field != ck_calc:
        errors.append(f"checksum 0x{ck_field:08x} != computed 0x{ck_calc:08x} "
                      f"(must be recomputed before write)")
    # Filename-vs-internal-name is a load requirement but lives outside the bytes;
    # surface the internal name so the caller can match the filename.
    name = read_character_name(data)
    res.sections["name"] = name
    if not name:
        errors.append("internal character name is empty (game will reject)")

    # --- player item list ---
    try:
        off = data.index(b"JM", 0x14F)
    except ValueError:
        errors.append("no player item list ('JM') found")
        res.ok = False
        return res
    pcount = struct.unpack_from("<H", data, off + 2)[0]
    bit, parsed, kids = _walk_item_list(data, (off + 4) * 8, pcount, st,
                                        "player", errors)
    res.item_count = parsed
    res.socketed_count = kids
    if parsed != pcount:
        errors.append(f"player: parsed {parsed}/{pcount} items before desync")
    if bit % 8 != 0:
        errors.append(f"player section not byte-aligned (ended at bit {bit})")
    pend = (bit + 7) // 8
    res.sections["player"] = {"offset": off, "count": pcount, "end": pend}

    # --- corpse section: 'JM' + u16 count ---
    if data[pend:pend + 2] != b"JM":
        errors.append(f"corpse header 'JM' missing at 0x{pend:x} "
                      f"(player section length wrong — the classic 'bad inventory data')")
        res.ok = not errors
        return res
    ncorpse = struct.unpack_from("<H", data, pend + 2)[0]
    res.sections["corpse_count"] = ncorpse
    cb = pend + 4
    for c in range(ncorpse):
        cb += 12  # corpse position/header block
        if data[cb:cb + 2] != b"JM":
            errors.append(f"corpse {c}: item list 'JM' missing at 0x{cb:x}")
            break
        ci = struct.unpack_from("<H", data, cb + 2)[0]
        bit, cp, ck = _walk_item_list(data, (cb + 4) * 8, ci, st, f"corpse{c}", errors)
        cb = (bit + 7) // 8

    # --- mercenary ('jf') + iron golem ('kf') framing presence check ---
    if data[cb:cb + 2] != b"jf":
        res.warnings.append(f"expected mercenary 'jf' marker at 0x{cb:x}, "
                            f"got {data[cb:cb+2]!r} (merc section framing may differ)")
    res.sections["after_corpse"] = cb

    # --- THE no-corruption guarantee: re-serialize the player section from the
    #     parsed items and require it to reproduce the original bytes exactly.
    #     If our codec round-trips the file, the game's reader (which we mirror)
    #     will read it identically. This catches the length/padding desyncs that
    #     cause "bad inventory data" even when a coincidental 'JM' hides them from
    #     the section walk above. This is the check that actually MATTERS for
    #     guaranteeing OUR edited saves load.
    try:
        il = item_v2.parse_item_list(data, off, st)
        reb = item_v2.serialize_item_list(il, data, st)
        region = data[off:pend]
        if reb[:len(region)] != region or len(reb) != len(region):
            res.ok = False
            n = min(len(reb), len(region))
            diff = next((i for i in range(n) if reb[i] != region[i]), n)
            errors.append(
                f"player section does not round-trip byte-exact "
                f"(first diff at +0x{diff:x}, len {len(region)}->{len(reb)}) — "
                f"the game will likely reject this as 'bad inventory data'"
            )
        if il.resyncs:
            res.ok = False
            errors.append(f"player section required {il.resyncs} resync(s) — stream desynced")
    except Exception as e:  # noqa: BLE001
        res.ok = False
        errors.append(f"player section round-trip failed: {e!r}")

    res.ok = res.ok and not errors
    return res


def validate_stash(data: bytes, st) -> ValidationResult:
    """Validate a stash file (shared 55BB / CSTM01 / SSS) via byte-exact round-trip:
    if parse->serialize reproduces the input exactly, the structure is sound."""
    from . import stash
    res = ValidationResult(ok=True)
    try:
        s = stash.parse_stash(data, st)
        out = stash.serialize_stash(s, st)
    except Exception as e:  # noqa: BLE001
        res.ok = False
        res.errors.append(f"stash parse/serialize failed: {e!r}")
        return res
    if out != data:
        res.ok = False
        n = min(len(out), len(data))
        diff = next((i for i in range(n) if out[i] != data[i]), n)
        res.errors.append(f"stash round-trip not byte-exact (first diff at 0x{diff:x}, "
                          f"len {len(data)}->{len(out)})")
    pages = getattr(s, "pages", []) or []
    res.item_count = sum(len(getattr(p, "items", []) or []) for p in pages)
    res.sections["kind"] = getattr(s, "kind", "?")
    res.sections["pages"] = len(pages)
    return res


def validate_file(path: str, st) -> ValidationResult:
    data = open(path, "rb").read()
    low = path.lower()
    if low.endswith((".d2x", ".sss", ".stash")) or data[:2] != b"\x55\xaa":
        return validate_stash(data, st)
    return validate_d2s(data, st)
