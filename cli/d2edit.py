"""
d2edit — CLI for the PD2 save editor.

Commands:
  info   <save.d2s>                 — character header summary
  items  <save.d2s>                 — list items with decoded fields
  verify <save.d2s>                 — byte-exact round-trip gate
  verify-stash <stash>             — whole-file byte-exact gate for
                                      55BB55BB / CSTM01 .d2x / SSS .sss stashes
  schema <pd2data.mpq>              — table availability + stat-encoding sample

MPQ path defaults to the PD2 install next to the save if not given via --mpq.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.d2s import D2SChar
from core.tables import GameTables

def _autodetect_mpq():
    """Portable MPQ discovery: $PD2_MPQ, then common install locations."""
    env = os.environ.get("PD2_MPQ")
    if env and os.path.exists(env):
        return env
    here = os.path.dirname(__file__)
    home = os.path.expanduser("~")
    for c in [
        os.path.join(here, "..", "..", "ProjectD2", "Live", "pd2data.mpq"),
        os.path.join(here, "..", "..", "ProjectD2", "pd2data.mpq"),
        r"C:\Program Files (x86)\Diablo II\ProjectD2\Live\pd2data.mpq",
        os.path.join(home, ".wine/drive_c/Program Files (x86)/Diablo II/ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "Games/Diablo II/ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "Games/Diablo II/ProjectD2/pd2data.mpq"),
        os.path.join(home, "Games/ProjectDiablo2/drive_c/Program Files (x86)/Diablo II/ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "Games/ProjectDiablo2/drive_c/Program Files (x86)/Diablo II/ProjectD2/pd2data.mpq"),
        os.path.join(home, "Sync/Games/Diablo II/ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "Sync/Games/Diablo II/ProjectD2/pd2data.mpq"),
        os.path.join(home, "ProjectD2/Live/pd2data.mpq"),
    ]:
        c = os.path.abspath(c)
        if os.path.exists(c):
            return c
    return ""


DEFAULT_MPQ = _autodetect_mpq()

QMAP = {1: "inferior", 2: "normal", 3: "superior", 4: "magic",
        5: "set", 6: "rare", 7: "unique", 8: "crafted"}
CLASSMAP = {0: "Amazon", 1: "Sorceress", 2: "Necromancer", 3: "Paladin",
            4: "Barbarian", 5: "Druid", 6: "Assassin"}


def _load(save, mpq):
    data = open(save, "rb").read()
    gt = GameTables(mpq)
    st = gt.stat_table()
    c = D2SChar.parse(data)
    il = c.parse_items(st)
    return data, gt, c, il


def cmd_info(args):
    data, gt, c, il = _load(args.save, args.mpq)
    name = c.name or os.path.splitext(os.path.basename(args.save))[0]
    print(f"name:    {name}")
    print(f"class:   {CLASSMAP.get(c.char_class, c.char_class)}")
    print(f"level:   {c.level}")
    print(f"version: 0x{c.version:x}")
    print(f"size:    {len(data)} bytes")
    print(f"items:   {il.count}")


def cmd_items(args):
    data, gt, c, il = _load(args.save, args.mpq)
    ok = sum(1 for it in il.items if it.decode_ok)
    print(f"{len(il.items)} items ({ok} fully decoded)")
    for i, it in enumerate(il.items):
        q = QMAP.get(it.quality, "?")
        extra = []
        if it.defense >= 0:
            extra.append(f"def={it.defense}")
        if it.max_durability > 0:
            extra.append(f"dur={it.cur_durability}/{it.max_durability}")
        if it.quantity >= 0:
            extra.append(f"qty={it.quantity}")
        if it.socketed:
            extra.append(f"sock={it.num_sockets}")
        if it.ethereal:
            extra.append("eth")
        flag = "ok" if it.decode_ok else "~"
        print(f"  [{i:2}] {it.type_code:<5} {q:<8} "
              f"stats={len(it.stats):<3} {' '.join(extra):<28} {flag}")


def cmd_verify(args):
    data, gt, c, il = _load(args.save, args.mpq)
    out = c.serialize()
    same = out == data
    print(f"round-trip: in={len(data)} out={len(out)} identical={same}")
    if not same:
        for i in range(min(len(out), len(data))):
            if out[i] != data[i]:
                print(f"  first diff @0x{i:x}")
                break
    sys.exit(0 if same else 1)


def cmd_verify_v2(args):
    """Structural round-trip gate using core.item_v2: re-serialize every item from
    its DECODED fields and byte-compare the whole item region to the original."""
    from core import item_v2

    data = open(args.save, "rb").read()
    gt = GameTables(args.mpq)
    st = gt.stat_table()

    is_stash = args.save.lower().endswith((".d2x", ".sss")) or data[:2] != b"\x55\xaa"
    grand = True

    def check_list(off):
        nonlocal grand
        il = item_v2.parse_item_list(data, off, st)
        end_byte = max((it.end_bit for it in _flat(il)), default=(off + 4) * 8) // 8
        if not il.items:
            return (off + 4)  # empty page: header only
        orig = data[off:end_byte]
        out = item_v2.serialize_item_list(il, data, st)
        same = out == orig
        items = _flat(il)
        ok = sum(1 for it in items if it.decode_ok)
        clean = sum(1 for it in items if it.clean)
        print(f"  list@0x{off:x} count={il.count} parsed={len(items)} "
              f"decode_ok={ok} clean={clean} resyncs={il.resyncs} identical={same}")
        if not same:
            grand = False
            n = min(len(out), len(orig))
            for i in range(n):
                if out[i] != orig[i]:
                    print(f"    first byte diff @+0x{i:x}: out={out[i]:02x} orig={orig[i]:02x}")
                    break
            if len(out) != len(orig):
                print(f"    length mismatch out={len(out)} orig={len(orig)}")
        if il.resyncs:
            grand = False
        return end_byte

    def _flat(il):
        out = []
        for it in il.items:
            out.append(it)
            out.extend(it.children)
        return out

    if is_stash:
        i = 0
        while i + 4 <= len(data):
            if data[i:i + 2] != b"JM":
                i += 1
                continue
            i = check_list(i)
    else:
        c = D2SChar.parse(data)
        check_list(c.items_offset)

    print(f"STRUCTURAL_BYTE_EXACT={grand}")
    sys.exit(0 if grand else 1)


def cmd_verify_stash(args):
    """Whole-file byte-exact round-trip gate for the three stash containers
    (55BB55BB shared, CSTM01 .d2x, SSS .sss). Parses the container + every JM
    item via core.stash, re-serializes the ENTIRE file, and byte-compares."""
    from core import stash as stash_mod

    data = open(args.save, "rb").read()
    gt = GameTables(args.mpq)
    st = gt.stat_table()

    r = stash_mod.verify_stash(data, st)
    print(f"file:      {args.save}")
    print(f"kind:      {r.kind}")
    print(f"size:      in={r.in_len} out={r.out_len}")
    print(f"pages:     {r.n_pages}")
    print(f"items:     {r.n_items} (decode_ok={r.decode_ok} clean={r.clean})")
    print(f"resyncs:   {r.resyncs}")
    print(f"BYTE_EXACT={r.byte_exact}")
    if not r.byte_exact:
        print(f"  first diff @0x{r.first_diff:x}")
    ok = r.byte_exact and r.resyncs == 0 and r.decode_ok == r.n_items
    sys.exit(0 if ok else 1)


def cmd_validate(args):
    """Predict whether the game will load a save: full structural validation
    (player/corpse/merc framing, every item parses to a clean boundary, size+
    checksum correct, filename matches internal name). Exit 0 = safe, 1 = rejected."""
    from core import validate as _v

    gt = GameTables(args.mpq)
    st = gt.stat_table()
    res = _v.validate_file(args.save, st)
    name = res.sections.get("name")
    if name:
        fname = os.path.splitext(os.path.basename(args.save))[0]
        if fname != name:
            res.warnings.append(
                f"filename '{fname}' != internal name '{name}' — rename to "
                f"'{name}.d2s' or the game says 'Bad character version'"
            )
    print(f"file:  {args.save}")
    print(f"items: {res.item_count} (+{res.socketed_count} socketed)")
    for k, val in res.sections.items():
        print(f"  {k}: {val}")
    for w in res.warnings:
        print(f"  WARN:  {w}")
    for e in res.errors:
        print(f"  ERROR: {e}")
    print(f"VALID={res.ok}")
    sys.exit(0 if res.ok else 1)


def cmd_schema(args):
    gt = GameTables(args.mpq)
    for t, present in gt.available().items():
        print(f"  {'OK ' if present else 'MISS'} {t}")
    enc = gt.build_stat_encoding()
    print(f"stats: {len(enc)}")


def main():
    ap = argparse.ArgumentParser(prog="d2edit")
    ap.add_argument("--mpq", default=DEFAULT_MPQ)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("info", cmd_info), ("items", cmd_items),
                     ("verify", cmd_verify), ("verify-v2", cmd_verify_v2),
                     ("verify-stash", cmd_verify_stash), ("validate", cmd_validate)]:
        p = sub.add_parser(name)
        p.add_argument("save")
        p.set_defaults(func=fn)
    p = sub.add_parser("schema")
    p.add_argument("mpq_arg", nargs="?")
    p.set_defaults(func=lambda a: cmd_schema(a))
    args = ap.parse_args()
    if args.cmd == "schema" and getattr(args, "mpq_arg", None):
        args.mpq = args.mpq_arg
    args.func(args)


if __name__ == "__main__":
    main()
