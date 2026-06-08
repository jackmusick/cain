"""
Cross-platform web GUI + headless backend for Cain (the multi-game save editor).

Stdlib-only HTTP server (identical on Linux/Win/mac, no deps). Serves a single-
page app + JSON API over the PROVEN modern stack:
  core.item_v2  — 100% item decode, byte-identical synthesis
  core.stash    — shared-55BB / CSTM01 / SSS containers, byte-exact
  core.validate — the no-corruption gate (run before EVERY write)

  python3 gui/server.py [--port 8765] [--mpq <pd2data.mpq>]
  then open http://localhost:8765

API:
  GET  /api/health                       -> schema status
  GET  /api/save?path=...                -> parsed character/stash JSON
  GET  /api/browse?kind=bases|stats      -> item browser data from live tables
  POST /api/edit      {path,item,stat_id,value}        -> set one stat
  POST /api/moveitem  {path,item,x,y}                  -> move item in inventory
  POST /api/maxroll   {path,item}                      -> max a clean item's stats
  POST /api/additem   {path,code,quality,stats:[...],x,y}  -> build+insert from scratch
  POST /api/validate  {path}                           -> predict game-acceptance

Every write goes to a sibling '<name>.edited.d2s' (source untouched) and is
REJECTED if the validator says the game would not load it.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import struct
import time
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import item_v2 as iv
from core import stash as stash_mod
from core import validate as validate_mod
from core.tables import GameTables
from core.bitreader import BitWriter
from core.bitreader import BitReader
from core.d2s import compute_checksum_d2, read_character_name

def _autodetect_mpq():
    """Find pd2data.mpq without hardcoding a machine. Order:
    1. $PD2_MPQ env var  2. common install locations (Win/Linux/Proton/Mac)."""
    env = os.environ.get("PD2_MPQ")
    if env and os.path.exists(env):
        return env
    home = os.path.expanduser("~")
    candidates = [
        # next to the repo (if the install was copied alongside)
        os.path.join(os.path.dirname(__file__), "..", "..", "ProjectD2", "Live", "pd2data.mpq"),
        os.path.join(os.path.dirname(__file__), "..", "..", "ProjectD2", "pd2data.mpq"),
        # Windows
        r"C:\Program Files (x86)\Diablo II\ProjectD2\Live\pd2data.mpq",
        r"C:\Program Files (x86)\Diablo II\ProjectD2\pd2data.mpq",
        # Linux / Proton / Lutris common roots
        os.path.join(home, ".wine/drive_c/Program Files (x86)/Diablo II/ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "Games/diablo-ii/ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "Games/Diablo II/ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "Games/Diablo II/ProjectD2/pd2data.mpq"),
        os.path.join(home, "Games/ProjectDiablo2/drive_c/Program Files (x86)/Diablo II/ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "Games/ProjectDiablo2/drive_c/Program Files (x86)/Diablo II/ProjectD2/pd2data.mpq"),
        os.path.join(home, "Sync/Games/Diablo II/ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "Sync/Games/Diablo II/ProjectD2/pd2data.mpq"),
        os.path.join(home, "ProjectD2/Live/pd2data.mpq"),
        os.path.join(home, "ProjectD2/pd2data.mpq"),
    ]
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.exists(c):
            return c
    return ""  # caller can still pass --mpq or set it in the UI


DEFAULT_MPQ = _autodetect_mpq()

QMAP = {0: "none", 1: "inferior", 2: "normal", 3: "superior", 4: "magic",
        5: "set", 6: "rare", 7: "unique", 8: "crafted"}
CLASSMAP = {0: "Amazon", 1: "Sorceress", 2: "Necromancer", 3: "Paladin",
            4: "Barbarian", 5: "Druid", 6: "Assassin"}
CLASS_CODES = {0: "ama", 1: "sor", 2: "nec", 3: "pal", 4: "bar", 5: "dru", 6: "ass"}
EQUIP_SLOTS = {
    1: "Head", 2: "Amulet", 3: "Armor", 4: "Right Hand", 5: "Left Hand",
    6: "Right Ring", 7: "Left Ring", 8: "Belt", 9: "Boots", 10: "Gloves",
    11: "Alt Right", 12: "Alt Left",
}
DIFFICULTIES = ["Normal", "Nightmare", "Hell"]
WAYPOINTS = [
    ("Act I", "Rogue Encampment"),
    ("Act I", "Cold Plains"),
    ("Act I", "Stony Field"),
    ("Act I", "Dark Wood"),
    ("Act I", "Black Marsh"),
    ("Act I", "Outer Cloister"),
    ("Act I", "Jail Level 1"),
    ("Act I", "Inner Cloister"),
    ("Act I", "Catacombs Level 2"),
    ("Act II", "Lut Gholein"),
    ("Act II", "Sewers Level 2"),
    ("Act II", "Dry Hills"),
    ("Act II", "Halls of the Dead Level 2"),
    ("Act II", "Far Oasis"),
    ("Act II", "Lost City"),
    ("Act II", "Palace Cellar Level 1"),
    ("Act II", "Arcane Sanctuary"),
    ("Act II", "Canyon of the Magi"),
    ("Act III", "Kurast Docks"),
    ("Act III", "Spider Forest"),
    ("Act III", "Great Marsh"),
    ("Act III", "Flayer Jungle"),
    ("Act III", "Lower Kurast"),
    ("Act III", "Kurast Bazaar"),
    ("Act III", "Upper Kurast"),
    ("Act III", "Travincal"),
    ("Act III", "Durance of Hate Level 2"),
    ("Act IV", "The Pandemonium Fortress"),
    ("Act IV", "City of the Damned"),
    ("Act IV", "River of Flame"),
    ("Act V", "Harrogath"),
    ("Act V", "Frigid Highlands"),
    ("Act V", "Arreat Plateau"),
    ("Act V", "Crystalline Passage"),
    ("Act V", "Halls of Pain"),
    ("Act V", "Glacial Trail"),
    ("Act V", "Frozen Tundra"),
    ("Act V", "The Ancients' Way"),
    ("Act V", "Worldstone Keep Level 2"),
]
QUEST_WORD_LABELS = [
    ("Act I", "Den of Evil"),
    ("Act I", "Sisters' Burial Grounds"),
    ("Act I", "Tools of the Trade"),
    ("Act I", "The Search for Cain"),
    ("Act I", "The Forgotten Tower"),
    ("Act I", "Sisters to the Slaughter"),
    ("Act II", "Radament's Lair"),
    ("Act II", "The Horadric Staff"),
    ("Act II", "Tainted Sun"),
    ("Act II", "Arcane Sanctuary"),
    ("Act II", "The Summoner"),
    ("Act II", "The Seven Tombs"),
    ("Act III", "The Golden Bird"),
    ("Act III", "Blade of the Old Religion"),
    ("Act III", "Khalim's Will"),
    ("Act III", "Lam Esen's Tome"),
    ("Act III", "The Blackened Temple"),
    ("Act III", "The Guardian"),
    ("Act IV", "The Fallen Angel"),
    ("Act IV", "Hell's Forge"),
    ("Act IV", "Terror's End"),
    ("Act V", "Siege on Harrogath"),
    ("Act V", "Rescue on Mount Arreat"),
    ("Act V", "Prison of Ice"),
    ("Act V", "Betrayal of Harrogath"),
    ("Act V", "Rite of Passage"),
    ("Act V", "Eve of Destruction"),
]
CHAR_STAT_DEFS = {
    0: ("strength", "Strength", 10, 1),
    1: ("energy", "Energy", 10, 1),
    2: ("dexterity", "Dexterity", 10, 1),
    3: ("vitality", "Vitality", 10, 1),
    4: ("stat_points", "Unspent Stat Points", 10, 1),
    5: ("skill_points", "Unspent Skill Points", 8, 1),
    6: ("current_life", "Current Life", 21, 256),
    7: ("max_life", "Maximum Life", 21, 256),
    8: ("current_mana", "Current Mana", 21, 256),
    9: ("max_mana", "Maximum Mana", 21, 256),
    10: ("current_stamina", "Current Stamina", 21, 256),
    11: ("max_stamina", "Maximum Stamina", 21, 256),
    12: ("level", "Level", 7, 1),
    13: ("experience", "Experience", 32, 1),
    14: ("gold", "Gold", 25, 1),
    15: ("stash_gold", "Stash Gold", 25, 1),
}
CHAR_STAT_BY_KEY = {v[0]: (sid, *v[1:]) for sid, v in CHAR_STAT_DEFS.items()}
TYPE_LABELS = {
    "glov": "Gloves", "boot": "Boots", "belt": "Belt", "helm": "Helm",
    "tors": "Armor", "shie": "Shield", "ring": "Ring", "amul": "Amulet",
    "scha": "Small Charm", "mcha": "Large Charm", "lcha": "Grand Charm",
    "hpot": "Healing Potion", "mpot": "Mana Potion", "rpot": "Rejuvenation",
    "book": "Tome", "box": "Cube", "key": "Key",
}
STAT_LABELS = {
    "strength": "+{v} to Strength",
    "energy": "+{v} to Energy",
    "dexterity": "+{v} to Dexterity",
    "vitality": "+{v} to Vitality",
    "maxhp": "+{v} to Life",
    "maxmana": "+{v} to Mana",
    "maxstamina": "+{v} Maximum Stamina",
    "armorclass": "+{v} Defense",
    "item_armor_percent": "+{v}% Enhanced Defense",
    "item_maxdamage_percent": "+{v}% Enhanced Damage",
    "item_maxdamage_percent_bytime": "+{v}% Enhanced Damage",
    "item_maxdamage_percent_perlevel": "+{v}% Enhanced Damage per Character Level",
    "item_maxdurability_percent": "Increase Maximum Durability {v}%",
    "mindamage": "+{v} to Minimum Damage",
    "maxdamage": "+{v} to Maximum Damage",
    "secondary_mindamage": "+{v} to Minimum Damage",
    "secondary_maxdamage": "+{v} to Maximum Damage",
    "item_throw_mindamage": "+{v} to Throw Minimum Damage",
    "item_throw_maxdamage": "+{v} to Throw Maximum Damage",
    "firemindam": "Adds {v} Fire Damage",
    "lightmindam": "Adds {v} Lightning Damage",
    "magicmindam": "Adds {v} Magic Damage",
    "coldmindam": "Adds {v} Cold Damage",
    "poisonmindam": "Adds {v} Poison Damage",
    "fireresist": "+{v}% Fire Resist",
    "lightresist": "+{v}% Lightning Resist",
    "coldresist": "+{v}% Cold Resist",
    "poisonresist": "+{v}% Poison Resist",
    "item_fastercastrate": "+{v}% Faster Cast Rate",
    "item_fastermovevelocity": "+{v}% Faster Run/Walk",
    "item_fastergethitrate": "+{v}% Faster Hit Recovery",
    "item_fasterblockrate": "+{v}% Faster Block Rate",
    "item_fasterattackrate": "+{v}% Increased Attack Speed",
    "item_magicbonus": "+{v}% Better Chance of Getting Magic Items",
    "item_goldbonus": "+{v}% Extra Gold from Monsters",
    "item_find_gold_perlevel": "+{v}% Extra Gold per Character Level",
    "item_tohit_percent": "+{v}% Bonus to Attack Rating",
    "item_allskills": "+{v} to All Skills",
    "item_manaafterkill": "+{v} to Mana after each Kill",
    "item_maxmana_percent": "+{v}% Maximum Mana",
    "item_maxhp_percent": "+{v}% Maximum Life",
    "item_req_percent": "{v}% Requirements",
    "item_lightradius": "+{v} to Light Radius",
    "item_openwounds": "{v}% Chance of Open Wounds",
    "item_crushingblow": "{v}% Chance of Crushing Blow",
    "item_deadlystrike": "{v}% Deadly Strike",
    "item_preventheal": "Prevent Monster Heal",
    "item_cannotbefrozen": "Cannot Be Frozen",
    "item_poisonlengthresist": "Poison Length Reduced by {v}%",
    "item_absorbcold_percent": "+{v}% Cold Absorb",
    "item_absorbfire_percent": "+{v}% Fire Absorb",
    "item_absorbmagic": "Magic Absorb +{v}",
    "item_damagetomana": "{v}% Damage Taken Goes to Mana",
    "item_damagetargetac": "{v}% Target Defense",
    "item_fractionaltargetac": "{v}% Target Defense",
    "item_slow": "Slows Target by {v}%",
    "item_staminadrainpct": "{v}% Slower Stamina Drain",
    "item_normaldamage": "Damage +{v}",
    "item_restinpeace": "Slain Monsters Rest in Peace",
    "item_addexperience": "+{v}% to Experience Gained",
    "item_reducedprices": "Reduces All Vendor Prices {v}%",
    "item_healafterkill": "+{v} Life after each Kill",
    "item_attackertakesdamage": "Attacker Takes Damage of {v}",
    "item_attackertakeslightdamage": "Attacker Takes Lightning Damage of {v}",
    "item_demondamage_percent": "+{v}% Damage to Demons",
    "item_undeaddamage_percent": "+{v}% Damage to Undead",
    "item_tohit_vs_demon": "+{v} to Attack Rating against Demons",
    "item_tohit_vs_undead": "+{v} to Attack Rating against Undead",
    "hpregen": "Replenish Life +{v}",
    "passive_phys_pierce": "-{v}% to Enemy Physical Resistance",
    "passive_pois_pierce": "-{v}% to Enemy Poison Resistance",
    "passive_cold_pierce": "-{v}% to Enemy Cold Resistance",
    "passive_fire_pierce": "-{v}% to Enemy Fire Resistance",
    "passive_ltng_pierce": "-{v}% to Enemy Lightning Resistance",
    "normal_damage_reduction": "Damage Reduced by {v}",
    "magic_damage_reduction": "Magic Damage Reduced by {v}",
    "item_replenish_durability": "Repairs 1 Durability in {v} Seconds",
    "item_replenish_charges": "Replenishes Charges",
    "item_splashonhit": "Melee Attacks Deal Splash Damage",
    "corrupted": "Corrupted",
    "corruptor": "Corruption modifier",
}
CLASS_NAMES = {
    0: "Amazon", 1: "Sorceress", 2: "Necromancer", 3: "Paladin",
    4: "Barbarian", 5: "Druid", 6: "Assassin",
}
SKILL_TAB_NAMES = {
    (0, 0): "Bow and Crossbow Skills",
    (0, 1): "Passive and Magic Skills",
    (0, 2): "Javelin and Spear Skills",
    (1, 0): "Fire Skills",
    (1, 1): "Lightning Skills",
    (1, 2): "Cold Skills",
    (2, 0): "Curses",
    (2, 1): "Poison and Bone Skills",
    (2, 2): "Summoning Skills",
    (3, 0): "Combat Skills",
    (3, 1): "Offensive Auras",
    (3, 2): "Defensive Auras",
    (4, 0): "Combat Skills",
    (4, 1): "Masteries",
    (4, 2): "Warcries",
    (5, 0): "Summoning Skills",
    (5, 1): "Shape Shifting Skills",
    (5, 2): "Elemental Skills",
    (6, 0): "Traps",
    (6, 1): "Shadow Disciplines",
    (6, 2): "Martial Arts",
}
# Save param for item_addskill_tab packs (charclass << 3) | within-class tab index
# (0,1,2 = the class's tabs in skill-page order). UniqueItems/Properties instead
# use the global skilltab id (class*3 + tab). Verified against in-game items:
# Amazon Bow/Crossbow = param 0, Assassin Martial Arts = param 50 (6<<3 | 2).
ELEMENT_SKILLS = {
    0: "Fire Skills",
    1: "Lightning Skills",
    2: "Cold Skills",
    3: "Poison Skills",
    4: "Fire Skills",
    5: "Magic Skills",
}
SKILL_NAME_SWAPS = {
    "AmpDmg Proc": "Amplify Damage",
    "LowRes Proc": "Lower Resist",
    "LowRes": "Lower Resist",
    "Iron Maiden Proc": "Iron Maiden",
    "Life Tap Proc": "Life Tap",
}

_gt = None
_mpq = DEFAULT_MPQ
_item_meta = None

# --- in-memory edit session -------------------------------------------------
# path -> {"data": bytearray, "dirty": bool, "kind": "d2s"|"stash", "rev": int}
_SESSION: dict[str, dict] = {}


def _session_key(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _read_bytes(path: str) -> bytearray:
    """Return the live in-memory buffer for `path`, loading it from disk once.

    All save-file reads in this module go through here so edits are visible
    without touching disk until an explicit commit_save()."""
    key = _session_key(path)
    entry = _SESSION.get(key)
    if entry is None:
        with open(path, "rb") as fh:
            data = bytearray(fh.read())
        entry = {"data": data, "dirty": False,
                 "kind": "stash" if _is_stash(path, bytes(data)) else "d2s",
                 "rev": 0}
        _SESSION[key] = entry
    return entry["data"]


def _store_bytes(path: str, data: bytes | bytearray) -> None:
    """Replace the buffer for `path`, marking it dirty and bumping its revision."""
    key = _session_key(path)
    entry = _SESSION.get(key)
    buf = data if isinstance(data, bytearray) else bytearray(data)
    if entry is None:
        entry = {"data": buf, "dirty": True,
                 "kind": "stash" if _is_stash(path, bytes(buf)) else "d2s",
                 "rev": 1}
        _SESSION[key] = entry
    else:
        entry["data"] = buf
        entry["dirty"] = True
        entry["rev"] += 1


def discard(path: str) -> None:
    """Drop the in-memory buffer so the next read reloads from disk."""
    _SESSION.pop(_session_key(path), None)


def dirty_paths() -> list[str]:
    """Absolute keys of buffers with unsaved edits."""
    return [k for k, e in _SESSION.items() if e["dirty"]]


def revision(path: str) -> int:
    """Edit revision counter for `path` (0 if not loaded)."""
    entry = _SESSION.get(_session_key(path))
    return entry["rev"] if entry else 0


def reset_session() -> None:
    """Test/teardown hook: forget all buffers."""
    _SESSION.clear()


# ---------------------------------------------------------------------------


def _int(s, default: int = 0) -> int:
    try:
        return int(str(s or "").strip())
    except (TypeError, ValueError):
        return default


def tables():
    global _gt
    if _gt is None:
        _gt = GameTables(_mpq)
        _gt.stat_table()
    return _gt


def _parse_tbl(blob: bytes) -> dict[str, str]:
    """Parse a Diablo II .tbl string table: header, index list, then hash
    entries of (used, index, hash, key_offset, val_offset, val_len)."""
    out: dict[str, str] = {}
    if len(blob) < 21:
        return out
    num_elements = struct.unpack_from("<H", blob, 2)[0]
    hash_size = struct.unpack_from("<I", blob, 4)[0]
    base = 21 + num_elements * 2

    def cstr(off: int) -> str:
        end = blob.find(b"\x00", off)
        if off <= 0 or end < 0:
            return ""
        return blob[off:end].decode("latin-1", "replace")

    for i in range(hash_size):
        entry = base + i * 17
        if entry + 17 > len(blob):
            break
        if not blob[entry]:
            continue
        key_off, val_off = struct.unpack_from("<II", blob, entry + 7)
        key = cstr(key_off)
        if key:
            out[key.lower()] = cstr(val_off)
    return out


_strings_cache: dict[str, str] | None = None


def game_strings() -> dict[str, str]:
    """Merged D2 string tables (classic -> expansion -> patch -> PD2 patch);
    later tables override earlier keys, matching game load order."""
    global _strings_cache
    if _strings_cache is not None:
        return _strings_cache
    from core.mpq import MPQArchive
    here = os.path.dirname(os.path.abspath(_mpq or ""))
    roots = [here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))]
    ordered_mpqs = ["d2data.mpq", "d2exp.mpq", "patch_d2.mpq",
                    os.path.basename(_mpq or "")]
    tbls = [r"data\local\lng\eng\string.tbl",
            r"data\local\lng\eng\expansionstring.tbl",
            r"data\local\lng\eng\patchstring.tbl"]
    merged: dict[str, str] = {}
    seen = set()
    for name in ordered_mpqs:
        path = next((os.path.join(r, name) for r in roots
                     if name and os.path.isfile(os.path.join(r, name))), None)
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            arc = MPQArchive(path)
        except Exception:  # noqa: BLE001
            continue
        for tbl in tbls:
            try:
                blob = arc.read_file(tbl)
            except Exception:  # noqa: BLE001
                continue
            if blob:
                merged.update(_parse_tbl(blob))
    _strings_cache = merged
    return merged


_stat_display_cache: dict[str, str] | None = None


def stat_display_labels() -> dict[str, str]:
    """stat name -> in-game display template with '#' where the value goes,
    built from itemstatcost descfunc/descstrpos + the game string tables."""
    global _stat_display_cache
    if _stat_display_cache is not None:
        return _stat_display_cache
    strings = game_strings()
    out: dict[str, str] = {}
    try:
        rows = tables().load_table("itemstatcost")
    except Exception:  # noqa: BLE001
        rows = []
    for row in rows:
        stat = (row.get("Stat") or "").strip()
        key = (row.get("descstrpos") or "").strip().lower()
        if not stat or not key:
            continue
        s = strings.get(key)
        if not s:
            continue
        s = (s.replace("%%", "%").replace("%+d", "+#")
              .replace("%d", "#").replace("%i", "#").replace("%s", "").strip())
        if "#" not in s:
            try:
                func = int(row.get("descfunc") or 0)
            except ValueError:
                func = 0
            try:
                descval = int(row.get("descval") or 1)
            except ValueError:
                descval = 1
            if descval == 0:
                pass  # value not shown in-game; leave the bare text
            elif descval == 2:  # value rendered after the string
                s = f"{s} #"
            elif func in (2, 5):
                s = f"#% {s}"
            elif func == 4:
                s = f"+#% {s}"
            elif func == 3:
                s = f"# {s}"
            else:
                s = f"+# {s}"
        out[stat] = s
    _stat_display_cache = out
    return out


def item_meta():
    global _item_meta
    if _item_meta is not None:
        return _item_meta
    gt = tables()
    meta = {}
    for tbl, category in (("Armor", "armor"), ("Weapons", "weapon"), ("Misc", "misc")):
        for r in gt.load_table(tbl):
            code = r.get("code", "").strip()
            if not code:
                continue
            name = (r.get("name", "") or r.get("*name", "") or code).strip()
            type_code = (r.get("type", "") or r.get("type2", "")).strip()
            type2_code = (r.get("type2", "") or "").strip()
            try:
                w = max(1, int((r.get("invwidth", "") or "1").strip()))
                h = max(1, int((r.get("invheight", "") or "1").strip()))
            except ValueError:
                w, h = 1, 1
            max_sockets = _int(r.get("gemsockets", ""), 0)
            meta[code] = {
                "code": code, "name": name, "category": category,
                "type": type_code, "type2": type2_code,
                "type_label": TYPE_LABELS.get(type_code, type_code),
                "width": w, "height": h,
                "max_sockets": max_sockets,
                "invfile": (r.get("invfile", "") or "").strip(),
                "uniqueinvfile": (r.get("uniqueinvfile", "") or "").strip(),
                "setinvfile": (r.get("setinvfile", "") or "").strip(),
            }
    _item_meta = meta
    return meta


def _item_type_map():
    out = {}
    for row in tables().load_table("ItemTypes"):
        code = (row.get("Code") or row.get("code") or "").strip()
        if code:
            out[code] = row
    return out


def _type_ancestors(type_code: str, seen=None) -> set[str]:
    type_code = (type_code or "").strip()
    if not type_code:
        return set()
    if seen is None:
        seen = set()
    if type_code in seen:
        return seen
    seen.add(type_code)
    row = _item_type_map().get(type_code)
    if not row:
        return seen
    for key in ("Equiv1", "Equiv2"):
        parent = (row.get(key) or "").strip()
        if parent:
            _type_ancestors(parent, seen)
    return seen


def _base_type_codes(code: str) -> set[str]:
    meta = item_meta().get(code, {})
    out = set()
    for type_key in ("type", "type2"):
        typ = (meta.get(type_key) or "").strip()
        out.update(_type_ancestors(typ))
    return out


def _runeword_compatibility(code: str, row: dict) -> tuple[bool, str]:
    meta = item_meta().get(code, {})
    if not meta:
        return False, f"unknown base item: {code}"
    base_types = _base_type_codes(code)
    allowed = [
        (row.get(f"itype{n}") or "").strip()
        for n in range(1, 7)
        if (row.get(f"itype{n}") or "").strip()
    ]
    excluded = [
        (row.get(f"etype{n}") or "").strip()
        for n in range(1, 4)
        if (row.get(f"etype{n}") or "").strip()
    ]
    if allowed and not any(typ in base_types for typ in allowed):
        return False, f"{meta.get('name', code)} is not an allowed type for this runeword"
    if any(typ in base_types for typ in excluded):
        return False, f"{meta.get('name', code)} is excluded for this runeword"
    sockets = len(_runeword_runes(row))
    max_sockets = int(meta.get("max_sockets", 0) or 0)
    if max_sockets and sockets > max_sockets:
        return False, f"{meta.get('name', code)} supports at most {max_sockets} sockets"
    return True, ""


def _quality_title(q: int) -> str:
    return QMAP.get(q, str(q)).title()


def _clean_base_name(meta: dict, code: str) -> str:
    name = meta.get("name") or code
    swaps = {
        "ring": "Ring",
        "amulet": "Amulet",
        "Charm Small": "Small Charm",
        "Charm Medium": "Large Charm",
        "Charm Large": "Grand Charm",
        "Rejuv Potion": "Rejuvenation Potion",
        "Girdle(H)": "Heavy Belt",
        "Bracers(M)": "Heavy Bracers",
    }
    return swaps.get(name, name)


def _is_separator_row(r: dict) -> bool:
    """UniqueItems/SetItems have an 'Expansion' separator row (blank code) that
    the game does NOT count in its unique/set id numbering. Items store the
    game id, so we must skip these rows when mapping id <-> table row."""
    idx = (r.get("index", "") or "").strip()
    return not idx or idx.lower() == "expansion"


def _row_to_gameid(table: str, row_idx: int) -> int:
    """Table row index -> stored game id (row index minus separators before it)."""
    rows = tables().load_table(table)
    seps = sum(1 for i, r in enumerate(rows) if i < row_idx and _is_separator_row(r))
    return row_idx - seps


def _gameid_to_row(table: str, game_id: int):
    """Stored game id -> table row index (re-adding skipped separator rows)."""
    rows = tables().load_table(table)
    seen = -1
    for i, r in enumerate(rows):
        if _is_separator_row(r):
            continue
        seen += 1
        if seen == game_id:
            return i
    return None


def _quality_row_name(table: str, game_id: int, code: str) -> str:
    if game_id < 0:
        return ""
    try:
        rows = tables().load_table(table)
    except Exception:
        return ""
    row_id = _gameid_to_row(table, game_id)
    if row_id is None or row_id >= len(rows):
        return ""
    row = rows[row_id]
    if row.get("code", "").strip() and row.get("code", "").strip() != code:
        return ""
    index = (row.get("index", "") or row.get("name", "")).strip()
    return game_strings().get(index.lower(), index) or index


def _row_name(table: str, row_id: int, zero_is_none: bool = True) -> str:
    if row_id < 0 or (zero_is_none and row_id == 0):
        return ""
    try:
        rows = tables().load_table(table)
    except Exception:
        return ""
    if row_id >= len(rows):
        return ""
    row = rows[row_id]
    return _clean_table_name(row.get("Name", "") or row.get("name", "") or row.get("index", ""))


def _clean_table_name(name: str) -> str:
    name = (name or "").strip()
    swaps = {
        "GhoulRI": "Ghoul",
        "PlagueRI": "Plague",
        "Wraithra": "Wraith",
        "Fiendra": "Fiend",
    }
    return swaps.get(name, name)


def _rare_prefix_name(row_id: int) -> str:
    name = _row_name("RarePrefix", row_id, zero_is_none=False)
    if name:
        return name
    # Rare prefix ids in D2 saves are commonly offset into the 8-bit field.
    return _row_name("RarePrefix", row_id - 156, zero_is_none=False)


def _magic_affix_name(row_id: int) -> str:
    return (_row_name("MagicPrefix", row_id)
            or _row_name("MagicSuffix", row_id)
            or f"affix {row_id}")


def _affix_names(it) -> dict:
    if it.quality == 4:
        pre = _row_name("MagicPrefix", it.prefix)
        suf = _row_name("MagicSuffix", it.suffix)
        return {
            "prefix_id": it.prefix, "suffix_id": it.suffix,
            "prefix": pre, "suffix": suf, "rare_affixes": [],
        }
    if it.quality in (6, 8):
        pre = _rare_prefix_name(it.prefix)
        suf = _row_name("RareSuffix", it.suffix, zero_is_none=False)
        rare = []
        for aid in getattr(it, "rare_affixes", []):
            if aid is not None:
                rare.append({"id": aid, "name": _magic_affix_name(aid)})
        return {
            "prefix_id": it.prefix, "suffix_id": it.suffix,
            "prefix": pre, "suffix": suf, "rare_affixes": rare,
        }
    return {
        "prefix_id": it.prefix, "suffix_id": it.suffix,
        "prefix": "", "suffix": "", "rare_affixes": [],
    }


def _display_name(it) -> str:
    meta = item_meta().get(it.type_code, {})
    base = _clean_base_name(meta, it.type_code)
    if it.quality == 7:
        return _quality_row_name("UniqueItems", it.set_unique_id, it.type_code) or f"Unique {base}"
    if it.quality == 5:
        return _quality_row_name("SetItems", it.set_unique_id, it.type_code) or f"Set {base}"
    if it.quality == 4:
        aff = _affix_names(it)
        parts = [aff.get("prefix"), base, aff.get("suffix")]
        return " ".join(p for p in parts if p) or f"Magic {base}"
    if it.quality == 6:
        aff = _affix_names(it)
        rare_name = " ".join(p for p in (aff.get("prefix"), aff.get("suffix").title()) if p)
        return f"{rare_name} {base}".strip() or f"Rare {base}"
    if it.quality == 8:
        aff = _affix_names(it)
        crafted_name = " ".join(p for p in (aff.get("prefix"), aff.get("suffix").title()) if p)
        return f"{crafted_name} {base}".strip() or f"Crafted {base}"
    return base


# Items with per-item art variants: the saved `graphic` index picks the file.
_GRAPHIC_VARIANTS = {"rin": ("invrin", 5), "amu": ("invamu", 3), "jew": ("invjw", 6)}


def _display_invfile(it, meta: dict) -> str:
    if it.quality == 7 and meta.get("uniqueinvfile"):
        return meta.get("uniqueinvfile", "")
    if it.quality == 5 and meta.get("setinvfile"):
        return meta.get("setinvfile", "")
    # Charms: Misc.txt points large/grand charms at wand/short-staff graphics
    # (invwnd/invsst) — vestigial values. The real charm graphics are invch1/2/3
    # (small/large/grand, height matches the code suffix), in d2exp.mpq.
    code = (it.type_code or "").strip()
    if code in ("cm1", "cm2", "cm3"):
        return "invch" + code[-1]
    variant = _GRAPHIC_VARIANTS.get((it.type_code or "").strip())
    if variant and getattr(it, "has_graphic", 0):
        prefix, count = variant
        g = int(getattr(it, "graphic", 0) or 0)
        if 0 <= g < count:
            return f"{prefix}{g + 1}"
    return meta.get("invfile", "")


def _stat_param(s, min_bits: int = 1) -> int | None:
    for nbits, value in reversed((s.leads or [[]])[0] if getattr(s, "leads", None) else []):
        if int(nbits) >= min_bits:
            return int(value)
    return None


def _skill_name(skill_id: int | None) -> str:
    if skill_id is None:
        return "Unknown Skill"
    try:
        rows = tables().load_table("Skills")
    except Exception:
        return f"Skill {skill_id}"
    for row in rows:
        if _int(row.get("Id", ""), -1) == int(skill_id):
            name = (row.get("skill") or row.get("skilldesc") or f"Skill {skill_id}").strip()
            return SKILL_NAME_SWAPS.get(name, name.removesuffix(" Proc"))
    return f"Skill {skill_id}"


def _class_name(class_id: int | None) -> str:
    if class_id is None:
        return "Class"
    return CLASS_NAMES.get(int(class_id), f"Class {class_id}")


def _format_per_level(name: str, value: int) -> str | None:
    labels = {
        "item_strength_perlevel": "Strength",
        "item_dexterity_perlevel": "Dexterity",
        "item_vitality_perlevel": "Vitality",
        "item_energy_perlevel": "Energy",
        "item_hp_perlevel": "Life",
        "item_mana_perlevel": "Mana",
        "item_stamina_perlevel": "Stamina",
        "item_tohit_perlevel": "Attack Rating",
        "item_tohit_percent_perlevel": "Attack Rating",
        "item_maxdamage_perlevel": "Maximum Damage",
        "item_maxdamage_percent_perlevel": "Enhanced Damage",
        "item_find_magic_perlevel": "Magic Find",
        "item_goldbonus_perlevel": "Extra Gold",
        "item_find_gold_perlevel": "Extra Gold",
        "item_deadlystrike_perlevel": "Deadly Strike",
        "item_damage_undead_perlevel": "Damage to Undead",
        "item_tohit_undead_perlevel": "Attack Rating against Undead",
    }
    label = labels.get(name)
    if not label:
        return None
    amount = value / 8
    amount_text = str(int(amount)) if amount.is_integer() else f"{amount:.3f}".rstrip("0").rstrip(".")
    suffix = "%" if any(key in name for key in ("percent", "magic", "goldbonus", "deadlystrike", "damage_undead")) else ""
    return f"+{amount_text}{suffix} {label} per Character Level"


def _format_stat(enc, s):
    name = enc.name if enc else f"stat{s.stat_id}"
    vals = s.values or [0]
    v = vals[-1]
    param = _stat_param(s)
    if name in ("item_singleskill", "item_nonclassskill"):
        return f"+{v} to {_skill_name(param)}"
    if name == "item_aura":
        return f"Level {v} {_skill_name(param)} Aura When Equipped"
    if name in ("item_skillonhit", "item_skillongethit", "item_skillonattack",
                "item_skillonkill", "item_skillondeath", "item_skillonlevelup"):
        skill_id = (param or 0) >> 6
        level = (param or 0) & 0x3F
        trigger = {
            "item_skillonhit": "on Striking",
            "item_skillongethit": "when Struck",
            "item_skillonattack": "on Attack",
            "item_skillonkill": "after Each Kill",
            "item_skillondeath": "when You Die",
            "item_skillonlevelup": "when You Level-Up",
        }[name]
        return f"{v}% Chance to Cast Level {level} {_skill_name(skill_id)} {trigger}"
    if name == "item_charged_skill":
        skill_id = (param or 0) >> 6
        level = (param or 0) & 0x3F
        current = int(v) & 0xFF
        maximum = (int(v) >> 8) & 0xFF
        charges = f"{current}/{maximum}" if maximum else str(current)
        return f"Level {level} {_skill_name(skill_id)} ({charges} Charges)"
    if name == "item_addclassskills":
        return f"+{v} to {_class_name(param)} Skills"
    if name == "item_addskill_tab":
        class_id = (param or 0) // 8
        tab_id = (param or 0) % 8
        tab = SKILL_TAB_NAMES.get((class_id, tab_id), f"Skill Tab {param}")
        return f"+{v} to {tab} ({_class_name(class_id)} Only)"
    if name == "item_elemskill":
        return f"+{v} to {ELEMENT_SKILLS.get(param, f'Element {param} Skills')}"
    if name.startswith("item_elemskill_"):
        elem = name.removeprefix("item_elemskill_").title()
        return f"+{v} to {elem} Skills"
    per_level = _format_per_level(name, int(v))
    if per_level:
        return per_level
    if name in ("firemindam", "lightmindam", "magicmindam") and len(vals) >= 2:
        elem = {"firemindam": "Fire", "lightmindam": "Lightning",
                "magicmindam": "Magic"}[name]
        return f"Adds {vals[0]}-{vals[1]} {elem} Damage"
    if name == "coldmindam" and len(vals) >= 3:
        return f"Adds {vals[0]}-{vals[1]} Cold Damage over {vals[2]} frames"
    if name == "poisonmindam" and len(vals) >= 3:
        return f"Adds {vals[0]}-{vals[1]} Poison Damage over {vals[2]} frames"
    if name == "mindamage" and len(vals) >= 2:
        return f"+{vals[0]} to Minimum Damage / +{vals[1]} to Maximum Damage"
    if name == "secondary_mindamage":
        return f"+{v} to Minimum Damage"
    if name == "secondary_maxdamage":
        return f"+{v} to Maximum Damage"
    if name == "item_throw_mindamage":
        return f"+{v} to Throw Minimum Damage"
    if name == "item_throw_maxdamage":
        return f"+{v} to Throw Maximum Damage"
    if name in STAT_LABELS:
        return STAT_LABELS[name].format(v=v)
    if len(vals) > 1:
        return f"{name.replace('_', ' ')} {' / '.join(str(x) for x in vals)}"
    return f"{name.replace('_', ' ')} {v}"


def _mpq_status():
    if not _mpq:
        return {"ok": False, "mpq": "", "stats": 0, "error": "No pd2data.mpq selected"}
    try:
        gt = tables()
        return {"ok": True, "mpq": _mpq, "stats": len(gt.stat_by_id)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "mpq": _mpq, "stats": 0, "error": repr(e)}


def set_mpq(path: str):
    global _gt, _item_meta, _mpq
    path = os.path.abspath(os.path.expanduser(path or ""))
    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"not found: {path}"}
    _mpq = path
    _gt = None
    _item_meta = None
    return _mpq_status()


def _pick_with_command(kind: str):
    title = "Select pd2data.mpq" if kind == "mpq" else "Select Diablo II save"
    filters = "*.mpq" if kind == "mpq" else "*.d2s *.d2x *.sss *.stash"
    start = os.path.expanduser("~")
    if sys.platform.startswith("linux"):
        if shutil.which("kdialog"):
            cmd = ["kdialog", "--title", title, "--getopenfilename", start, filters]
        elif shutil.which("zenity"):
            if kind == "mpq":
                cmd = ["zenity", "--file-selection", "--title", title,
                       "--file-filter", "MPQ files | *.mpq"]
            else:
                cmd = ["zenity", "--file-selection", "--title", title,
                       "--file-filter", "D2 saves | *.d2s *.d2x *.sss *.stash",
                       "--file-filter", "All files | *"]
        else:
            return ""
        r = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    return ""


def pick_path(kind: str):
    picked = _pick_with_command(kind)
    if picked:
        return {"path": picked}
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if kind == "mpq":
            path = filedialog.askopenfilename(
                title="Select pd2data.mpq",
                filetypes=(("MPQ files", "*.mpq"), ("All files", "*.*")),
            )
        else:
            path = filedialog.askopenfilename(
                title="Select Diablo II save",
                filetypes=(
                    ("D2 saves", "*.d2s *.d2x *.sss *.stash"),
                    ("All files", "*.*"),
                ),
            )
        root.destroy()
        return {"path": path or ""}
    except Exception as e:  # noqa: BLE001
        return {"path": "", "error": repr(e)}


def _pack(bits):
    bw = BitWriter()
    bw.write_bits_list(bits)
    return bw.to_bytes()


def _char_stat_block(data: bytes):
    off = data.find(b"gf")
    if off < 0:
        raise ValueError("character stat block marker 'gf' not found")
    br = BitReader(data, (off + 2) * 8)
    entries = []
    values = {}
    while True:
        sid = br.read(9)
        if sid == 0x1FF:
            break
        spec = CHAR_STAT_DEFS.get(sid)
        if not spec:
            raise ValueError(f"unknown character stat id {sid}")
        key, label, bits, scale = spec
        raw = br.read(bits)
        value = raw // scale if scale != 1 else raw
        entries.append({
            "id": sid, "key": key, "label": label,
            "bits": bits, "scale": scale, "raw": raw, "value": value,
        })
        values[key] = value
    end = (br.bit_pos + 7) // 8
    return off, end, entries, values


def character_stats(data: bytes):
    _off, _end, entries, values = _char_stat_block(data)
    return {"entries": entries, "values": values}


def _serialize_char_stats(entries):
    bw = BitWriter()
    for entry in entries:
        sid = int(entry["id"])
        bits = int(entry["bits"])
        bw.write(sid, 9)
        bw.write(int(entry["raw"]), bits)
    bw.write(0x1FF, 9)
    return bw.to_bytes()


def _class_skill_rows(char_class: int):
    code = CLASS_CODES.get(char_class, "")
    rows = [r for r in tables().load_table("Skills") if r.get("charclass", "").strip() == code]
    return rows


def _skill_block(data: bytes):
    off = data.find(b"if")
    if off < 0:
        raise ValueError("skill block marker 'if' not found")
    jm = data.index(b"JM", off)
    char_class = data[0x28]
    rows = _class_skill_rows(char_class)
    raw = list(data[off + 2:jm])
    skills = []
    for i, value in enumerate(raw):
        row = rows[i] if i < len(rows) else {}
        sid = int(row.get("Id", i) or i)
        name = (row.get("skill", "") or f"skill {sid}").strip()
        skills.append({"index": i, "id": sid, "name": name, "level": value})
    return off, jm, skills


def character_skills(data: bytes):
    _off, _end, skills = _skill_block(data)
    return {"skills": skills}


def _waypoint_block(data: bytes):
    off = data.find(b"WS")
    if off < 0:
        raise ValueError("waypoint block marker 'WS' not found")
    if off + 8 > len(data):
        raise ValueError("waypoint block is truncated")
    size = struct.unpack_from("<H", data, off + 6)[0]
    if size < 80 or off + size > len(data):
        size = 80
    records_start = off + 8
    records = []
    for diff in range(3):
        start = records_start + diff * 24
        record = data[start:start + 24]
        if len(record) < 24:
            raise ValueError("waypoint difficulty record is truncated")
        mask = int.from_bytes(record[2:7], "little")
        records.append({"start": start, "record": record, "mask": mask})
    return off, size, records


def character_waypoints(data: bytes):
    _off, _size, records = _waypoint_block(data)
    out = []
    for diff, rec in enumerate(records):
        points = []
        for idx, (act, name) in enumerate(WAYPOINTS):
            points.append({
                "id": idx, "act": act, "name": name,
                "unlocked": bool(rec["mask"] & (1 << idx)),
            })
        out.append({"difficulty": diff, "name": DIFFICULTIES[diff], "waypoints": points})
    return {"difficulties": out}


def _quest_block(data: bytes):
    off = data.find(b"Woo!")
    if off < 0:
        raise ValueError("quest block marker 'Woo!' not found")
    if off + 10 > len(data):
        raise ValueError("quest block is truncated")
    size = struct.unpack_from("<H", data, off + 8)[0]
    if size < 298 or off + size > len(data):
        size = 298
    records_start = off + 10
    records = []
    for diff in range(3):
        start = records_start + diff * 96
        rec = data[start:start + 96]
        if len(rec) < 96:
            raise ValueError("quest difficulty record is truncated")
        words = [struct.unpack_from("<H", rec, i * 2)[0] for i in range(48)]
        records.append({"start": start, "words": words})
    return off, size, records


def character_quests(data: bytes):
    _off, _size, records = _quest_block(data)
    out = []
    for diff, rec in enumerate(records):
        quests = []
        for idx, value in enumerate(rec["words"]):
            if idx < len(QUEST_WORD_LABELS):
                act, name = QUEST_WORD_LABELS[idx]
            else:
                act, name = "Progress", f"Reserved Flag {idx - len(QUEST_WORD_LABELS) + 1}"
            quests.append({
                "id": idx, "act": act, "name": name,
                "flags": value, "hex": f"{value:04x}",
                "completed": bool(value & 0x0001),
            })
        out.append({"difficulty": diff, "name": DIFFICULTIES[diff], "quests": quests})
    return {"difficulties": out}


# --------------------------------------------------------------------------- #
# Read side
# --------------------------------------------------------------------------- #
def _stat_to_dict(s, gt=None):
    gt = gt or tables()
    enc = gt.stat_by_id.get(s.stat_id)
    component_bounds = _stat_component_bounds(s)
    editable = component_bounds is not None
    bounds = _stat_edit_bounds(s) or (0, 0)
    group_ids = [int(s.stat_id)] + [int(x) for x in iv.STAT_GROUPS.get(int(s.stat_id), [])]
    components = []
    for pos, value in enumerate(list(s.values or [])):
        cid = group_ids[pos] if pos < len(group_ids) else int(s.stat_id)
        cenc = gt.stat_by_id.get(cid)
        lo, hi = component_bounds[pos] if component_bounds and pos < len(component_bounds) else (0, 0)
        components.append({
            "id": cid,
            "name": cenc.name if cenc else f"stat{cid}",
            "value": value,
            "min": lo,
            "max": hi,
        })
    return {
        "id": s.stat_id,
        "name": enc.name if enc else f"stat{s.stat_id}",
        "text": _format_stat(enc, s),
        "value": s.values[-1] if s.values else 0,
        "values": list(s.values or []),
        "components": components,
        "grouped": len(components) > 1,
        "editable": editable,
        "min": bounds[0],
        "max": bounds[1],
    }


def _runeword_specs(row: dict):
    specs = []
    for n in range(1, 8):
        code = (row.get(f"T1Code{n}") or row.get(f"T1code{n}") or "").strip()
        if not code:
            continue
        specs.append({
            "code": code,
            "param": (row.get(f"T1Param{n}") or row.get(f"T1param{n}") or "").strip(),
            "min": _int(row.get(f"T1Min{n}", row.get(f"T1min{n}", ""))),
            "max": _int(row.get(f"T1Max{n}", row.get(f"T1max{n}", ""))),
        })
    return specs


def _runeword_number(row: dict) -> int | None:
    match = re.fullmatch(r"Runeword(\d+)", (row.get("Name") or "").strip())
    if not match:
        return None
    return int(match.group(1))


def _runeword_row(it, gt=None):
    gt = gt or tables()
    try:
        rows = gt.load_table("Runes")
    except Exception:
        return None
    if not rows:
        return None

    stats = list(getattr(it, "runeword_stats", []) or [])
    actual_ids = {int(s.stat_id) for s in stats}
    decoded = int(getattr(it, "runeword_id", -1))
    low12 = decoded & 0x0fff
    candidate_numbers = {low12, low12 - 26, decoded, decoded - 20406}
    candidate_rows = set()
    for idx, row in enumerate(rows):
        num = _runeword_number(row)
        if idx in candidate_numbers or (num is not None and num in candidate_numbers):
            candidate_rows.add(idx)

    best = None
    best_score = -1
    best_overlap = -1
    for idx, row in enumerate(rows):
        expected = _simple_stats_from_specs(_runeword_specs(row), int(getattr(it, "version", 0x67)), gt)
        expected_ids = {int(s.stat_id) for s in expected}
        if stats and not expected_ids:
            continue
        overlap = len(actual_ids & expected_ids) if stats else 0
        score = overlap * 10
        if idx in candidate_rows:
            score += 3
        if (row.get("complete") or "").strip() == "1":
            score += 1
        if not stats and idx not in candidate_rows:
            continue
        if score > best_score or (score == best_score and overlap > best_overlap):
            best = row
            best_score = score
            best_overlap = overlap
    if best is not None and (not stats or best_overlap > 0 or rows.index(best) in candidate_rows):
        return best
    for idx in sorted(candidate_rows):
        if 0 <= idx < len(rows):
            return rows[idx]
    return None


def _runeword_name(it, gt=None):
    row = _runeword_row(it, gt)
    if not row:
        return ""
    return (row.get("Rune Name") or row.get("Name") or "").strip()


def _runeword_save_id(row: dict, idx: int) -> int:
    num = _runeword_number(row)
    low = int(num if num is not None else idx) & 0x0FFF
    return 0x5000 | low


def _runeword_runes(row: dict) -> list[str]:
    return [
        (row.get(f"Rune{n}") or row.get(f"rune{n}") or "").strip()
        for n in range(1, 7)
        if (row.get(f"Rune{n}") or row.get(f"rune{n}") or "").strip()
    ]


def _runeword_types(row: dict) -> list[str]:
    out = []
    for prefix in ("itype", "etype"):
        for n in range(1, 7):
            val = (row.get(f"{prefix}{n}") or "").strip()
            if val:
                out.append(val)
    return out


def item_to_dict(it):
    gt = tables()
    meta = item_meta().get(it.type_code, {})
    runeword_name = _runeword_name(it, gt) if it.runeword else ""
    display_name = runeword_name or _display_name(it)
    d = {
        "type_code": it.type_code,
        "name": display_name,
        "base_name": _clean_base_name(meta, it.type_code),
        "type_label": meta.get("type_label", ""),
        "category": meta.get("category", ""),
        "width": meta.get("width", 1),
        "height": meta.get("height", 1),
        "max_sockets": meta.get("max_sockets", 0),
        "invfile": _display_invfile(it, meta),
        "set_unique_id": it.set_unique_id,
        "quality": QMAP.get(it.quality, str(it.quality)),
        "ilvl": it.ilvl,
        "identified": it.identified,
        "ethereal": it.ethereal,
        "personalized": bool(it.personalized),
        "personal_name": it.personal_name or "",
        "runeword": bool(it.runeword),
        "runeword_id": it.runeword_id,
        "runeword_name": runeword_name,
        "num_sockets": it.num_sockets,
        "filled_sockets": len(it.children),
        "location": it.location_id,
        "equipped_id": it.equipped_id,
        "pos_x": it.pos_x, "pos_y": it.pos_y, "panel": it.panel_id,
        "clean": it.clean,
        "stats": [],
        "runeword_stats": [],
    }
    d["affixes"] = _affix_names(it)
    if it.defense >= 0:
        d["defense"] = it.defense
    if it.max_dur > 0:
        d["durability"] = f"{it.cur_dur}/{it.max_dur}"
        d["current_durability"] = it.cur_dur
        d["max_durability"] = it.max_dur
    if it.quantity >= 0:
        d["quantity"] = it.quantity
    for s in it.stats:
        d["stats"].append(_stat_to_dict(s, gt))
    for s in getattr(it, "runeword_stats", []) or []:
        d["runeword_stats"].append(_stat_to_dict(s, gt))
    if it.children:
        d["sockets"] = [item_to_dict(c) for c in it.children]
    return d


def _is_stash(path, data):
    return path.lower().endswith((".d2x", ".sss", ".stash")) or data[:2] != b"\x55\xaa"


def _walk_player(data, st):
    off = data.index(b"JM", 0x14F)
    return _walk_item_list_at(data, st, off)


def _walk_item_list_at(data, st, off: int):
    if data[off:off + 2] != b"JM":
        raise ValueError(f"item list marker 'JM' missing at 0x{off:x}")
    pcount = struct.unpack_from("<H", data, off + 2)[0]
    bit = (off + 4) * 8
    items = []
    for _ in range(pcount):
        it, bit = iv.parse_one_item(data, bit, st)
        items.append(it)
        for _c in range(it.num_in_sockets):
            ch, bit = iv.parse_one_item(data, bit, st)
            it.children.append(ch)
    return off, pcount, items, bit // 8


def _character_item_sections(data: bytes, st):
    sections = []
    off, pcount, items, pend = _walk_player(data, st)
    sections.append({
        "id": "player",
        "name": "Player",
        "offset": off,
        "end": pend,
        "count": pcount,
        "items": items,
        "editable": True,
    })

    pos = pend
    if data[pos:pos + 2] == b"JM" and pos + 4 <= len(data):
        corpse_count = struct.unpack_from("<H", data, pos + 2)[0]
        pos += 4
        for corpse_idx in range(corpse_count):
            if pos + 12 > len(data):
                break
            pos += 12
            if data[pos:pos + 2] != b"JM":
                break
            coff, ccount, citems, pos = _walk_item_list_at(data, st, pos)
            sections.append({
                "id": f"corpse{corpse_idx}",
                "name": f"Corpse {corpse_idx + 1}",
                "offset": coff,
                "end": pos,
                "count": ccount,
                "items": citems,
                "editable": False,
            })

    if data[pos:pos + 2] == b"jf" and pos + 6 <= len(data) and data[pos + 2:pos + 4] == b"JM":
        moff, mcount, mitems, pos = _walk_item_list_at(data, st, pos + 2)
        sections.append({
            "id": "merc",
            "name": "Mercenary",
            "offset": moff,
            "end": pos,
            "count": mcount,
            "items": mitems,
            "editable": True,
        })

    if data[pos:pos + 2] == b"kf":
        marker = pos + 2
        if data[marker:marker + 2] == b"JM":
            goff, gcount, gitems, gend = _walk_item_list_at(data, st, marker)
            sections.append({
                "id": "golem",
                "name": "Iron Golem",
                "offset": goff,
                "end": gend,
                "count": gcount,
                "items": gitems,
                "editable": True,
            })
    return sections


def parse_save(path):
    data = bytes(_read_bytes(path))
    st = tables().stat_table()
    if _is_stash(path, data):
        s = stash_mod.parse_stash(data, st)
        pages = []
        if getattr(s, "kind", "") == "shared55bb":
            for tab, items in _shared_tab_groups(s):
                pages.append({"name": f"Tab {tab}", "count": len(items),
                              "items": [item_to_dict(it) for it in items]})
        else:
            pages = [
                {"name": getattr(p, "name", ""), "count": len(p.items),
                 "items": [item_to_dict(it) for it in p.items]}
                for p in (s.pages or [])
            ]
        return {
            "kind": "stash:" + getattr(s, "kind", "?"),
            "pages": pages,
        }
    sections = _character_item_sections(data, st)
    player = sections[0]
    pcount = int(player.get("count", 0))
    items = player.get("items", [])
    name = read_character_name(data)
    return {
        "kind": "character",
        "name": name or os.path.splitext(os.path.basename(path))[0],
        "class": CLASSMAP.get(data[0x28], data[0x28]),
        "level": data[0x2B],
        "character_stats": character_stats(data),
        "skills": character_skills(data),
        "waypoints": character_waypoints(data),
        "quests": character_quests(data),
        "version": f"0x{struct.unpack_from('<I', data, 4)[0]:x}",
        "item_count": pcount,
        "clean": sum(1 for it in items if it.clean),
        "items": [item_to_dict(it) for it in items],
        "item_sections": [
            {
                "id": sec["id"],
                "name": sec["name"],
                "count": sec["count"],
                "editable": sec.get("editable", False),
                "items": [item_to_dict(it) for it in sec.get("items", [])],
            }
            for sec in sections
        ],
    }


# --------------------------------------------------------------------------- #
# Character summary (read-only, LLM/CLI-friendly flattening of parse_save)
# --------------------------------------------------------------------------- #
_EQUIP_SLOT_ORDER = ["Head", "Amulet", "Armor", "Right Hand", "Left Hand",
                     "Belt", "Right Ring", "Left Ring", "Gloves", "Boots",
                     "Alt Right", "Alt Left"]


def _item_stat_texts(it: dict) -> list[str]:
    """All human-readable stat lines for an item: its own stats, runeword
    stats, and any socketed-item stats (the things actually on the gear)."""
    texts = []
    for s in it.get("stats", []) or []:
        if s.get("text"):
            texts.append(s["text"])
    for s in it.get("runeword_stats", []) or []:
        if s.get("text"):
            texts.append(s["text"])
    for sock in it.get("sockets", []) or []:
        for s in sock.get("stats", []) or []:
            if s.get("text"):
                texts.append(s["text"])
    return texts


def character_summary(path: str) -> dict:
    """A flat, JSON-clean view of a character for an LLM or CLI: identity,
    attributes, allocated skills, and equipped gear with resolved names and
    human-readable stat lines. Read-only; reuses parse_save's tested decode.

    Resists are reported as per-item gear lines only — effective resist totals
    (sum of gear/charms, minus the difficulty penalty Normal 0 / NM -40 / Hell
    -100) are left to the caller, since the save stores mods, not totals."""
    try:
        save = parse_save(path)
        data = bytes(_read_bytes(path))
    except Exception as e:  # noqa: BLE001 — contract is "always JSON, never crash"
        return {"error": f"{type(e).__name__}: {e}"}
    if save.get("kind") != "character":
        return {"error": f"not a character (.d2s) save; got '{save.get('kind')}'"}
    cs = (save.get("character_stats") or {}).get("values", {})

    attributes = {
        "strength": cs.get("strength", 0),
        "dexterity": cs.get("dexterity", 0),
        "vitality": cs.get("vitality", 0),
        "energy": cs.get("energy", 0),
        "life": cs.get("current_life", 0), "max_life": cs.get("max_life", 0),
        "mana": cs.get("current_mana", 0), "max_mana": cs.get("max_mana", 0),
        "stamina": cs.get("current_stamina", 0),
        "max_stamina": cs.get("max_stamina", 0),
        "unspent_stat_points": cs.get("stat_points", 0),
        "unspent_skill_points": cs.get("skill_points", 0),
        "experience": cs.get("experience", 0),
        "gold": cs.get("gold", 0), "gold_stash": cs.get("stash_gold", 0),
    }

    skills = [
        {"id": s.get("id"), "name": s.get("name"), "level": s.get("level")}
        for s in (save.get("skills") or {}).get("skills", [])
        if s.get("level")
    ]

    # Where a carried item actually sits. ONLY inventory-grid items (location 0,
    # panel 1) are "active" — charms there count toward resists; charms in the
    # stash/cube do nothing. Belt holds potions. (panel: 1=inv, 4=cube, 5/6=stash;
    # location: 1=equipped, 2=belt, 0=stored.)
    def _where(it) -> str:
        loc, panel = it.get("location"), it.get("panel")
        if loc == 1:
            return "equipped"
        if loc == 2:
            return "belt"
        if loc == 0 and panel == 1:
            return "inventory"
        if loc == 0 and panel == 4:
            return "cube"
        if loc == 0 and panel in (5, 6):
            return "stash"
        return f"loc{loc}/panel{panel}"

    equipped, inventory, stash, cube = [], [], [], []
    for it in save.get("items", []):
        where = _where(it)
        entry = {
            "name": it.get("name"),
            "base": it.get("base_name"),
            "quality": "runeword" if it.get("runeword") else it.get("quality"),
            "ethereal": bool(it.get("ethereal")),
            "sockets": it.get("num_sockets", 0),
            "stats": _item_stat_texts(it),
        }
        if where == "equipped":
            entry["slot"] = EQUIP_SLOTS.get(it.get("equipped_id"),
                                            f"Slot {it.get('equipped_id')}")
            equipped.append(entry)
        elif where == "inventory":
            inventory.append(entry)
        elif where == "stash":
            stash.append(entry)
        elif where == "cube":
            cube.append(entry)
        # belt items (potions) are consumables — omitted from the build view.
    order = {name: i for i, name in enumerate(_EQUIP_SLOT_ORDER)}
    equipped.sort(key=lambda e: order.get(e["slot"], 99))

    difficulty_progress = {}
    try:
        for d in character_quests(data).get("difficulties", []):
            started = any(q.get("flags") for q in d.get("quests", []))
            difficulty_progress[d["name"].lower()] = started
    except Exception:  # noqa: BLE001 — progress is best-effort metadata
        difficulty_progress = {}

    return {
        "identity": {
            "name": save.get("name"),
            "class": save.get("class"),
            "level": save.get("level"),
            "version": save.get("version"),
            "hardcore": bool(data[0x24] & 0x04),
            "difficulty_progress": difficulty_progress,
        },
        "attributes": attributes,
        "skills": skills,
        "equipped": equipped,
        "inventory": inventory,
        "stash": stash,
        "cube": cube,
        "notes": [
            "Resist lines under each item are per-item gear mods only.",
            "Effective resist = sum of mods on 'equipped' + 'inventory' charms "
            "ONLY (charms in 'stash'/'cube' are INACTIVE), capped at 75 base, "
            "minus the difficulty penalty: Normal 0, Nightmare -40, Hell -100.",
            "'stash'/'cube' items are owned but not worn — swap candidates only.",
        ],
    }


# --------------------------------------------------------------------------- #
# Item browser (build-from-scratch data, from the LIVE tables — game-agnostic)
# --------------------------------------------------------------------------- #
def browse(kind):
    gt = tables()
    if kind == "stats":
        out = []
        grouped_substats = {x for xs in iv.STAT_GROUPS.values() for x in xs}
        for sid, e in sorted(gt.stat_by_id.items()):
            if not e.save_bits_base and not e.save_bits:
                continue  # not directly storable
            if int(sid) in grouped_substats:
                continue  # grouped members are edited through their leader
            group = [int(sid)] + [int(x) for x in iv.STAT_GROUPS.get(int(sid), [])]
            components = []
            for cid in group:
                enc = gt.stat_by_id.get(cid)
                if not enc:
                    continue
                bits = enc.save_bits_base or enc.save_bits
                add = enc.save_add_base if enc.save_bits_base else enc.save_add
                if bits <= 0:
                    continue
                components.append({
                    "id": cid, "name": enc.name, "bits": bits,
                    "min": -add, "max": (1 << bits) - 1 - add,
                })
            if not components:
                continue
            row = {
                "id": sid, "name": e.name,
                "bits": components[0]["bits"],
                "min": components[0]["min"],
                "max": components[0]["max"],
                "components": components,
                "value_count": len(components),
            }
            out.append(row)
        out.sort(key=lambda r: (r.get("name") or "").lower())
        return {"stats": out}
    if kind == "uniques":
        out = []
        strings = game_strings()
        for i, r in enumerate(gt.load_table("UniqueItems")):
            index = (r.get("index", "") or r.get("name", "")).strip()
            code = r.get("code", "").strip()
            if not index or not code:
                continue
            # the index ("Cutthroat1") is a string-table key; resolve it to the
            # real display name ("Bartuc's Cut-Throat"), falling back to the index.
            name = strings.get(index.lower(), index) or index
            out.append({
                "id": i, "name": name, "code": code,
                "level": _int(r.get("lvl", "")),
                "level_req": _int(r.get("lvl req", "")),
                "enabled": _int(r.get("enabled", ""), 1),
            })
        out.sort(key=lambda r: (r.get("name") or "").lower())
        return {"uniques": out}
    if kind == "sets":
        out = []
        strings = game_strings()
        for i, r in enumerate(gt.load_table("SetItems")):
            index = (r.get("index", "") or r.get("name", "")).strip()
            code = r.get("item", "").strip()
            if not index or not code:
                continue
            name = strings.get(index.lower(), index) or index
            out.append({
                "id": i, "name": name, "set": r.get("set", "").strip(),
                "code": code, "level": _int(r.get("lvl", "")),
                "level_req": _int(r.get("lvl req", "")),
            })
        out.sort(key=lambda r: (r.get("name") or "").lower())
        return {"sets": out}
    if kind in ("magic_prefixes", "magic_suffixes"):
        table = "MagicPrefix" if kind == "magic_prefixes" else "MagicSuffix"
        key = "prefixes" if kind == "magic_prefixes" else "suffixes"
        out = []
        for i, r in enumerate(gt.load_table(table)):
            name = _clean_table_name(r.get("Name", "") or r.get("name", ""))
            if not name:
                continue
            mods = []
            for n in range(1, 4):
                code = r.get(f"mod{n}code", "").strip()
                if code:
                    mods.append({
                        "code": code,
                        "param": r.get(f"mod{n}param", "").strip(),
                        "min": _int(r.get(f"mod{n}min", "")),
                        "max": _int(r.get(f"mod{n}max", "")),
                    })
            out.append({
                "id": i, "name": name, "level": _int(r.get("level", "")),
                "level_req": _int(r.get("levelreq", "")),
                "spawnable": _int(r.get("spawnable", ""), 0),
                "rare": _int(r.get("rare", ""), 0),
                "mods": mods,
            })
        out.sort(key=lambda r: (r.get("name") or "").lower())
        return {key: out}
    if kind in ("rare_prefixes", "rare_suffixes"):
        table = "RarePrefix" if kind == "rare_prefixes" else "RareSuffix"
        key = "prefixes" if kind == "rare_prefixes" else "suffixes"
        out = []
        for i, r in enumerate(gt.load_table(table)):
            name = _clean_table_name(r.get("name", "") or r.get("Name", ""))
            if not name:
                continue
            save_id = i + 156 if kind == "rare_prefixes" else i
            out.append({"id": save_id, "row": i, "name": name})
        out.sort(key=lambda r: (r.get("name") or "").lower())
        return {key: out}
    if kind == "socket_fillers":
        out = []
        for r in gt.load_table("Misc"):
            code = r.get("code", "").strip()
            typ = r.get("type", "").strip()
            if not code:
                continue
            if typ == "rune" or typ.startswith("gem") or typ == "jewl":
                name = (r.get("name", "") or r.get("*name", "") or r.get("namestr", "") or code).strip()
                out.append({
                    "code": code,
                    "name": _clean_base_name({"name": name}, code),
                    "type": typ,
                    "invfile": (r.get("invfile", "") or "").strip(),
                })
        out.sort(key=lambda r: (r.get("name") or "").lower())
        return {"socket_fillers": out}
    if kind == "runewords":
        filler_names = {row["code"]: row["name"] for row in browse("socket_fillers").get("socket_fillers", [])}
        out = []
        for i, r in enumerate(gt.load_table("Runes")):
            name = (r.get("Rune Name") or r.get("Name") or "").strip()
            runes = _runeword_runes(r)
            if not name or not runes or (r.get("complete") or "").strip() != "1":
                continue
            stats = _simple_stats_from_specs(_runeword_specs(r), 0x67, gt)
            out.append({
                "id": i,
                "save_id": _runeword_save_id(r, i),
                "key": (r.get("Name") or "").strip(),
                "name": name,
                "runes": runes,
                "rune_names": [filler_names.get(code, code) for code in runes],
                "sockets": len(runes),
                "types": _runeword_types(r),
                "allowed_types": [
                    (r.get(f"itype{n}") or "").strip()
                    for n in range(1, 7)
                    if (r.get(f"itype{n}") or "").strip()
                ],
                "excluded_types": [
                    (r.get(f"etype{n}") or "").strip()
                    for n in range(1, 4)
                    if (r.get(f"etype{n}") or "").strip()
                ],
                "stats": [_stat_to_dict(s, gt) for s in stats],
            })
        out.sort(key=lambda r: (r.get("name") or "").lower())
        return {"runewords": out}
    # bases: item codes by category from Armor/Weapons/Misc
    out = []
    for tbl, cat in (("Armor", "armor"), ("Weapons", "weapon"), ("Misc", "misc")):
        for r in gt.load_table(tbl):
            code = r.get("code", "").strip()
            name = (r.get("name", "") or r.get("namestr", "")).strip()
            if code:
                meta = item_meta().get(code, {})
                out.append({
                    "code": code, "name": name or code, "cat": cat,
                    "type": meta.get("type", ""),
                    "type2": meta.get("type2", ""),
                    "type_codes": sorted(_base_type_codes(code)),
                    "max_sockets": meta.get("max_sockets", 0),
                })
    out.sort(key=lambda r: (r.get("name") or "").lower())
    return {"bases": out}


# --------------------------------------------------------------------------- #
# Write side — every write is validated before it lands
# --------------------------------------------------------------------------- #
def _backup_original(path: str) -> str | None:
    """Copy the current file into <folder>/backups/<name>.<stamp>.bak before
    overwriting it. Returns the backup path (None if the file didn't exist)."""
    if not os.path.isfile(path):
        return None
    folder = os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(path)
    candidate = os.path.join(folder, f"{base}.{stamp}.bak")
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base}.{stamp}-{n}.bak")
        n += 1
    shutil.copy2(path, candidate)
    return candidate


def _finalize(data: bytearray):
    struct.pack_into("<I", data, 8, len(data))
    struct.pack_into("<I", data, 0x0C, 0)
    struct.pack_into("<I", data, 0x0C, compute_checksum_d2(bytes(data)))


def _gate_and_write(data, path: str):
    """Apply an edit to the in-memory buffer. No disk write, no backup, no
    validation here — validation is debounced in the UI and run authoritatively
    by commit_save()."""
    _store_bytes(path, data)
    return {"ok": True, "out": path, "pending": True}


def _gate_and_write_stash(data, path: str):
    """Stash edits buffer the same way as character edits; kept as a named
    call site for the stash do_* handlers."""
    return _gate_and_write(data, path)


def validate_buffer(path: str):
    """Validate the in-memory buffer for `path` without writing. Returns
    {"ok": True} or {"ok": False, "errors": [...]}. Requires tables (MPQ)."""
    st = tables().stat_table()
    data = bytes(_read_bytes(path))   # ensures the entry exists
    kind = _SESSION[_session_key(path)]["kind"]
    res = (validate_mod.validate_stash(data, st) if kind == "stash"
           else validate_mod.validate_d2s(data, st))
    return {"ok": res.ok, "errors": list(res.errors)}


def commit_save(path: str):
    """Validate the buffer, back up the original, write to disk, clear dirty."""
    key = _session_key(path)
    entry = _SESSION.get(key)
    if entry is None or not entry["dirty"]:
        return {"ok": True, "out": path, "nothing_to_do": True}
    payload = bytes(entry["data"])   # snapshot; assumes single writer (Qt UI is
                                     # disabled during save) — validate & write the
                                     # same bytes
    v = validate_buffer(path)
    if not v["ok"]:
        return {"ok": False, "error": "edit rejected by validator (would not load)",
                "details": v["errors"], "path": path}
    backup = _backup_original(path)
    with open(path, "wb") as f:
        f.write(payload)
    entry["dirty"] = False
    return {"ok": True, "out": path, "backup": backup, "validated": True}


def commit_all():
    """Save every dirty buffer. Aggregates results; the single Save action."""
    results = []
    ok = True
    for path in list(dirty_paths()):
        r = commit_save(path)
        results.append(r)
        ok = ok and bool(r.get("ok"))
    return {"ok": ok, "results": results}


def _stat_edit_bounds(stat):
    if getattr(stat, "special", False):
        return None
    if len(getattr(stat, "values", []) or []) < 1:
        return None
    if len(getattr(stat, "values", []) or []) != len(getattr(stat, "bits", []) or []):
        return None
    if len(getattr(stat, "values", []) or []) != len(getattr(stat, "adds", []) or []):
        return None
    bits = int(stat.bits[-1])
    add = int((stat.adds or [0])[-1])
    if bits <= 0:
        return None
    return -add, (1 << bits) - 1 - add


def _stat_component_bounds(stat):
    if getattr(stat, "special", False):
        return None
    values = list(getattr(stat, "values", []) or [])
    bits = list(getattr(stat, "bits", []) or [])
    adds = list(getattr(stat, "adds", []) or [])
    if not values or len(values) != len(bits) or len(values) != len(adds):
        return None
    out = []
    for nbits, add in zip(bits, adds):
        nbits, add = int(nbits), int(add)
        if nbits <= 0:
            return None
        out.append((-add, (1 << nbits) - 1 - add))
    return out


def _set_item_stat_value(it, stat_id: int, value: int) -> bool:
    return _set_item_stat_values(it, stat_id, [value])


def _set_item_stat_values(it, stat_id: int, values) -> bool:
    stat = next((x for x in it.stats if int(x.stat_id) == int(stat_id)), None)
    if stat is None:
        raise ValueError(f"stat {stat_id} is not on this item")
    bounds = _stat_component_bounds(stat)
    if bounds is None:
        raise ValueError(f"stat {stat_id} is not safely editable yet")
    if not isinstance(values, (list, tuple)):
        values = [values]
    if len(values) != len(bounds):
        raise ValueError(f"stat {stat_id} expects {len(bounds)} value(s)")
    vals = []
    raws = []
    for value, (lo, hi), add in zip(values, bounds, stat.adds):
        val = max(lo, min(hi, int(value)))
        vals.append(val)
        raws.append(val + int(add))
    stat.values = vals
    stat.raw_values = raws
    return True


def _set_numeric_raw(value: int, bits: int, add: int) -> tuple[int, int]:
    raw = max(0, min((1 << int(bits)) - 1, int(value) + int(add)))
    return raw - int(add), raw


def _item_stat_from_value(sid: int, value: int, version: int, gt: GameTables):
    values = value if isinstance(value, (list, tuple)) else [value]
    return _item_stat_from_values(sid, values, version, gt)


def _item_stat_from_values(sid: int, values, version: int, gt: GameTables):
    enc = gt.stat_by_id.get(int(sid))
    if not enc:
        return None
    stat_ids = [int(sid)] + [int(x) for x in iv.STAT_GROUPS.get(int(sid), [])]
    if not isinstance(values, (list, tuple)):
        values = [values]
    if len(values) != len(stat_ids):
        return None
    out_values = []
    bits_out = []
    adds = []
    raw_values = []
    leads = []
    for pos, (cid, raw_value) in enumerate(zip(stat_ids, values)):
        enc = gt.stat_by_id.get(int(cid))
        if not enc:
            return None
        if version >= 0x67 and enc.save_bits_base:
            bits = enc.save_bits_base
            add = enc.save_add_base
            param_bits = enc.save_param_bits_base
        else:
            bits = enc.save_bits
            add = enc.save_add
            param_bits = enc.save_param_bits
        if bits <= 0:
            return None
        lo, hi = -int(add), (1 << int(bits)) - 1 - int(add)
        val = max(lo, min(hi, int(raw_value)))
        lead = []
        if pos == 0:
            if version >= 0x67:
                lead.append((1, 0))
            if param_bits > 0:
                lead.append((param_bits, 0))
        out_values.append(val)
        bits_out.append(bits)
        adds.append(add)
        raw_values.append(val + add)
        leads.append(lead)
    stat = iv.ItemStat(stat_id=int(sid), values=out_values, bits=bits_out, adds=adds,
                       raw_values=raw_values, leads=leads)
    stat.wire_id = int(sid)
    return stat


def _set_stat_param(stat, param: int):
    if not stat or not getattr(stat, "leads", None):
        return stat
    leads = list(stat.leads[0] or [])
    for idx in range(len(leads) - 1, -1, -1):
        nbits, _old = leads[idx]
        if int(nbits) > 1:
            mask = (1 << int(nbits)) - 1
            leads[idx] = (int(nbits), int(param) & mask)
            stat.leads[0] = leads
            return stat
    return stat


def _item_stat_with_param(stat_name: str, value: int, param: int, version: int, gt: GameTables):
    enc = gt.stat_by_name.get(stat_name)
    if not enc:
        return None
    stat = _item_stat_from_value(enc.stat_id, value, version, gt)
    return _set_stat_param(stat, param)


def _skill_id(param: str) -> int | None:
    text = str(param or "").strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        return int(text)
    aliases = {
        "AmpDmg Proc": "Amplify Damage",
        "LowRes Proc": "Lower Resist",
    }
    wanted = aliases.get(text, text).lower()
    try:
        rows = tables().load_table("Skills")
    except Exception:
        return None
    for row in rows:
        names = [
            row.get("skill", ""),
            row.get("skilldesc", ""),
        ]
        if any(str(name or "").strip().lower() == wanted for name in names):
            return _int(row.get("Id", ""), -1)
    return None


def _add_item_stat_value(it, stat_id: int, value: int, gt: GameTables) -> bool:
    sid = int(stat_id)
    if any(int(x.stat_id) == sid for x in it.stats):
        _set_item_stat_values(it, sid, value if isinstance(value, (list, tuple)) else [value])
        return False
    stat = _item_stat_from_values(sid, value if isinstance(value, (list, tuple)) else [value], int(it.version), gt)
    if stat is None:
        raise ValueError(f"stat {sid} is not safely addable yet")
    it.stats.append(stat)
    return True


def _remove_item_stat_value(it, stat_id: int) -> bool:
    sid = int(stat_id)
    for pos, stat in enumerate(list(it.stats)):
        if int(stat.stat_id) != sid:
            continue
        bounds = _stat_edit_bounds(stat)
        if bounds is None:
            raise ValueError(f"stat {sid} is not safely removable yet")
        del it.stats[pos]
        return True
    return False


def _property_map():
    gt = tables()
    out = {}
    for row in gt.load_table("Properties"):
        code = row.get("code", "").strip()
        if code:
            out[code] = row
    return out


def _property_specs(row: dict, prefix: str, limit: int):
    specs = []
    for n in range(1, limit + 1):
        code = row.get(f"{prefix}{n}", row.get(f"{prefix}{n}code", "")).strip()
        if not code:
            continue
        specs.append({
            "code": code,
            "param": row.get(f"par{n}", row.get(f"mod{n}param", "")).strip(),
            "min": _int(row.get(f"min{n}", row.get(f"mod{n}min", ""))),
            "max": _int(row.get(f"max{n}", row.get(f"mod{n}max", ""))),
        })
    return specs


def _simple_stats_from_specs(specs: list[dict], version: int, gt: GameTables):
    props = _property_map()
    name_values = {}
    stats = []
    for spec in specs:
        prop = props.get(spec.get("code", ""))
        if not prop:
            continue
        value = spec.get("max", 0)
        param = spec.get("param", "")
        for n in range(1, 8):
            stat_name = prop.get(f"stat{n}", "").strip()
            func = _int(prop.get(f"func{n}", ""))
            # The simple functions use min/max as the stat value. More complex
            # property functions are handled explicitly below when their D2 item
            # stat packing is well understood.
            if func == 10 and stat_name:
                # skill tab (e.g. Bartuc's "+2 Martial Arts"). The property's par
                # is the GLOBAL skilltab id (class*3 + tab); the saved stat packs
                # it as (class << 3) | tab. Convert so it serializes correctly.
                tab = _int(param, -1)
                if tab >= 0:
                    save_param = (tab // 3) * 8 + (tab % 3)
                    stat = _item_stat_with_param(stat_name, value, save_param, version, gt)
                    if stat:
                        stats.append(stat)
                continue
            if func == 21 and stat_name:
                # class-specific skills (e.g. "+2 to Assassin Skills"): the class
                # lives in the property's val1 column (ama=0, sor=1, nec=2, pal=3,
                # bar=4, dru=5, ass=6). Without writing it as the stat param the
                # item serializes with param 0 = Amazon — the "+2 Amazon skills"
                # bug on assassin uniques like Bartuc's. Carry the class through.
                class_id = _int(prop.get("val1", ""), 0)
                stat = _item_stat_with_param(stat_name, value, class_id, version, gt)
                if stat:
                    stats.append(stat)
                continue
            if func in (1, 2, 3, 8, 14) and stat_name:
                name_values[stat_name] = value
                continue
            if func == 5:
                name_values["mindamage"] = value
                name_values["secondary_mindamage"] = value
                name_values["item_throw_mindamage"] = value
                continue
            if func == 6:
                name_values["maxdamage"] = value
                name_values["secondary_maxdamage"] = value
                name_values["item_throw_maxdamage"] = value
                continue
            if func == 7:
                enc = gt.stat_by_name.get(stat_name or "item_maxdamage_percent")
                if enc:
                    stat = _item_stat_from_values(enc.stat_id, [value, value], version, gt)
                    if stat:
                        stats.append(stat)
                continue
            if func == 11 and stat_name:
                sid = _skill_id(param)
                if sid is not None:
                    level = spec.get("max", 0)
                    chance = spec.get("min", 0)
                    stat = _item_stat_with_param(stat_name, chance, (sid << 6) | int(level), version, gt)
                    if stat:
                        stats.append(stat)
                continue
            if func == 17 and stat_name:
                per_level = _int(param, value)
                enc = gt.stat_by_name.get(stat_name)
                if enc:
                    stat = _item_stat_from_value(enc.stat_id, per_level, version, gt)
                    if stat:
                        stats.append(stat)
                continue
            if func == 19 and stat_name:
                sid = _skill_id(param)
                if sid is not None:
                    charges = spec.get("min", 0)
                    level = spec.get("max", 0)
                    packed_charges = int(charges) | (int(charges) << 8)
                    stat = _item_stat_with_param(stat_name, packed_charges, (sid << 6) | int(level), version, gt)
                    if stat:
                        stats.append(stat)
                continue
            if func == 22 and stat_name:
                sid = _skill_id(param)
                if sid is not None:
                    stat = _item_stat_with_param(stat_name, value, sid, version, gt)
                    if stat:
                        stats.append(stat)

    grouped_names = {
        "mindamage": ["mindamage", "maxdamage"],
        "firemindam": ["firemindam", "firemaxdam"],
        "lightmindam": ["lightmindam", "lightmaxdam"],
        "magicmindam": ["magicmindam", "magicmaxdam"],
        "coldmindam": ["coldmindam", "coldmaxdam", "coldlength"],
        "poisonmindam": ["poisonmindam", "poisonmaxdam", "poisonlength"],
    }
    consumed = set()
    for lead_name, names in grouped_names.items():
        if all(name in name_values for name in names):
            lead_enc = gt.stat_by_name.get(lead_name)
            if not lead_enc:
                continue
            st = _item_stat_from_value(lead_enc.stat_id, name_values[names[0]], version, gt)
            if not st:
                continue
            for extra_name in names[1:]:
                enc = gt.stat_by_name.get(extra_name)
                if not enc:
                    continue
                extra = _item_stat_from_value(enc.stat_id, name_values[extra_name], version, gt)
                if not extra:
                    continue
                st.values.append(extra.values[0])
                st.bits.append(extra.bits[0])
                st.adds.append(extra.adds[0])
                st.raw_values.append(extra.raw_values[0])
                st.leads.append(extra.leads[0])
            stats.append(st)
            consumed.update(names)
    for name, value in name_values.items():
        if name in consumed:
            continue
        enc = gt.stat_by_name.get(name)
        if not enc:
            continue
        st = _item_stat_from_value(enc.stat_id, value, version, gt)
        if st:
            stats.append(st)
    return stats


def do_edit(body):
    path, idx = body["path"], int(body["item"])
    stat_id, value = int(body["stat_id"]), int(body["value"])
    st = tables().stat_table()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)
    it = items[idx]
    try:
        _set_item_stat_value(it, stat_id, value)
    except ValueError as e:
        return {"error": str(e)}
    # rebuild the player section with the edited item
    rebuilt = _rebuild_player(bytes(data), st, items, off, pend, pcount)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        _, _, it2, _ = _walk_player(bytes(rebuilt), st)
        out["item"] = item_to_dict(it2[idx])
    return out


def do_maxroll(body):
    path, idx = body["path"], int(body["item"])
    st = tables().stat_table()
    gt = tables()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)
    it = items[idx]
    maxed = 0
    for s in it.stats:
        enc = gt.stat_by_id.get(s.stat_id)
        if not enc:
            continue
        bits = enc.save_bits_base or enc.save_bits
        add = enc.save_add_base if enc.save_bits_base else enc.save_add
        if bits <= 0:
            continue
        ceil = (1 << bits) - 1 - add
        s.values = [ceil]
        s.raw_values = [ceil + add]
        maxed += 1
    rebuilt = _rebuild_player(bytes(data), st, items, off, pend, pcount)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        _, _, it2, _ = _walk_player(bytes(rebuilt), st)
        out["item"] = item_to_dict(it2[idx])
        out["maxed_stats"] = maxed
    return out


def _apply_item_edit(it, body, st):
    fields = body.get("fields", {}) or {}
    changed = {}

    if "identified" in fields:
        it.identified = bool(fields["identified"])
        changed["identified"] = it.identified
    if "ethereal" in fields:
        it.ethereal = bool(fields["ethereal"])
        changed["ethereal"] = it.ethereal
    if "personalized" in fields or "personal_name" in fields:
        raw_name = str(fields.get("personal_name", getattr(it, "personal_name", "") or ""))
        safe = "".join(ch for ch in raw_name if 32 <= ord(ch) < 127)[:15]
        personalized = bool(fields.get("personalized", bool(safe)))
        it.personalized = personalized
        it.personal_name = safe if personalized else ""
        changed["personalized"] = it.personalized
        changed["personal_name"] = it.personal_name
    if "ilvl" in fields:
        it.ilvl = max(0, min(127, int(fields["ilvl"])))
        changed["ilvl"] = it.ilvl
    if "defense" in fields and it.defense >= 0:
        enc = st.get(0x1F)
        add = (enc.save_add_base if it.version >= 0x67 and enc and enc.save_bits_base
               else enc.save_add if enc else 10)
        it.defense, it.defense_raw = _set_numeric_raw(
            int(fields["defense"]), int(it.defense_bits), int(add))
        changed["defense"] = it.defense
    if "max_durability" in fields and it.max_dur >= 0:
        enc = st.get(0x49)
        add = (enc.save_add_base if it.version >= 0x67 and enc and enc.save_bits_base
               else enc.save_add if enc else 0)
        it.max_dur, it.max_dur_raw = _set_numeric_raw(
            int(fields["max_durability"]), int(it.max_dur_bits), int(add))
        changed["max_durability"] = it.max_dur
        if it.cur_dur > it.max_dur:
            it.cur_dur = it.max_dur
    if "current_durability" in fields and it.cur_dur >= 0:
        enc = st.get(0x48)
        add = (enc.save_add_base if it.version >= 0x67 and enc and enc.save_bits_base
               else enc.save_add if enc else 0)
        val = min(int(fields["current_durability"]), int(it.max_dur if it.max_dur >= 0 else fields["current_durability"]))
        it.cur_dur, it.cur_dur_raw = _set_numeric_raw(val, int(it.cur_dur_bits), int(add))
        changed["current_durability"] = it.cur_dur
    if "quantity" in fields and it.quantity >= 0:
        it.quantity = max(0, min((1 << int(it.quantity_bits)) - 1, int(fields["quantity"])))
        changed["quantity"] = it.quantity
    if "num_sockets" in fields:
        sockets = max(0, min(15, int(fields["num_sockets"])))
        if sockets < len(it.children):
            raise ValueError(f"cannot set sockets below filled socket count ({len(it.children)}); unsocket first")
        it.socketed = sockets > 0
        it.num_sockets = sockets
        changed["num_sockets"] = sockets

    stat_updates = body.get("stats", {}) or {}
    stat_changes = {}
    for sid, val in stat_updates.items():
        try:
            values = val if isinstance(val, (list, tuple)) else [val]
            values = [int(x) for x in values]
            _set_item_stat_values(it, int(sid), values)
            stat_changes[str(sid)] = values[0] if len(values) == 1 else values
        except ValueError as e:
            raise ValueError(str(e)) from e

    gt = tables()
    stat_adds = {}
    for entry in body.get("add_stats", []) or []:
        try:
            sid = int(entry.get("stat_id", entry.get("id")))
            raw_val = entry.get("values", entry.get("value", 0))
            values = raw_val if isinstance(raw_val, (list, tuple)) else [raw_val]
            values = [int(x) for x in values]
            created = _add_item_stat_value(it, sid, values, gt)
            stat_adds[str(sid)] = {"value": values[0] if len(values) == 1 else values, "created": created}
        except (TypeError, ValueError) as e:
            raise ValueError(str(e)) from e

    stat_removes = []
    for sid_raw in body.get("remove_stats", []) or []:
        try:
            sid = int(sid_raw)
            if _remove_item_stat_value(it, sid):
                stat_removes.append(str(sid))
        except ValueError as e:
            raise ValueError(str(e)) from e

    if not changed and not stat_changes and not stat_adds and not stat_removes:
        raise ValueError("no editable item changes supplied")
    return {
        "fields": changed,
        "stats": stat_changes,
        "add_stats": stat_adds,
        "remove_stats": stat_removes,
    }


def do_edititem(body):
    """Edit core item properties and simple/grouped item stats, then validate."""
    path, idx = body["path"], int(body["item"])
    st = tables().stat_table()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)
    if idx < 0 or idx >= len(items):
        return {"error": f"item index out of range: {idx}"}
    it = items[idx]
    try:
        changed = _apply_item_edit(it, body, st)
    except ValueError as e:
        return {"error": str(e)}
    rebuilt = _rebuild_player(bytes(data), st, items, off, pend, pcount)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        _, _, it2, _ = _walk_player(bytes(rebuilt), st)
        out["item"] = item_to_dict(it2[idx])
        out["changed"] = changed
    return out


def do_unsocketitem(body):
    """Remove socketed child items from a character item and validate the save."""
    path, idx = body["path"], int(body["item"])
    st = tables().stat_table()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)
    if idx < 0 or idx >= len(items):
        return {"error": f"item index out of range: {idx}"}
    it = items[idx]
    removed = len(it.children)
    if removed <= 0:
        return {"error": f"{_display_name(it)} has no socketed items"}
    removed_items = [item_to_dict(ch) for ch in it.children]
    it.children = []
    it.num_in_sockets = 0
    rebuilt = _rebuild_player(bytes(data), st, items, off, pend, pcount)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        _, _, it2, _ = _walk_player(bytes(rebuilt), st)
        out["item"] = item_to_dict(it2[idx])
        out["removed"] = removed_items
        out["removed_count"] = removed
    return out


def do_socketitem(body):
    """Add a simple rune/gem/jewel child item to an open socket and validate."""
    path, idx = body["path"], int(body["item"])
    code = str(body.get("code", "")).strip()
    if not code:
        return {"error": "choose a socket filler"}
    fillers = {row["code"] for row in browse("socket_fillers").get("socket_fillers", [])}
    if code not in fillers:
        return {"error": f"{code} is not a rune, gem, or jewel filler"}
    st = tables().stat_table()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)
    if idx < 0 or idx >= len(items):
        return {"error": f"item index out of range: {idx}"}
    parent = items[idx]
    if not parent.socketed or parent.num_sockets <= 0:
        return {"error": f"{_display_name(parent)} has no sockets"}
    if len(parent.children) >= parent.num_sockets:
        return {"error": f"{_display_name(parent)} has no open sockets"}

    tmpl = next((it for it in items if it.clean and it.type_code == code and it.simple), None)
    if tmpl is None:
        for it in items:
            if it.clean and it.simple and not it.is_ear:
                tmpl = it
                break
            for ch in it.children:
                if ch.clean and ch.simple and not ch.is_ear:
                    tmpl = ch
                    break
            if tmpl is not None:
                break
    if tmpl is None:
        return {"error": "no usable simple item template in this save"}

    child = copy.deepcopy(tmpl)
    child.type_code = code
    child.children = []
    child.num_in_sockets = 0
    child.socketed = False
    child.num_sockets = 0
    child.location_id = 6
    child.equipped_id = 0
    child.pos_x = len(parent.children)
    child.pos_y = 0
    child.panel_id = 0
    child.simple = True
    child.identified = True
    parent.children.append(child)
    parent.num_in_sockets = len(parent.children)

    rebuilt = _rebuild_player(bytes(data), st, items, off, pend, pcount)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        _, _, it2, _ = _walk_player(bytes(rebuilt), st)
        out["item"] = item_to_dict(it2[idx])
        out["socketed"] = item_to_dict(child)
    return out


def _socket_child_from_template(tmpl, code: str, pos: int):
    child = copy.deepcopy(tmpl)
    child.type_code = code
    child.children = []
    child.num_in_sockets = 0
    child.socketed = False
    child.num_sockets = 0
    child.location_id = 6
    child.equipped_id = 0
    child.pos_x = pos
    child.pos_y = 0
    child.panel_id = 0
    child.simple = True
    child.identified = True
    child.runeword = False
    child.runeword_id = -1
    child.runeword_stats = []
    child.stats = []
    return child


def _assign_fresh_guids(item, existing: set[int]):
    guid = 0x71000000
    while guid in existing:
        guid += 1
    item.guid = guid
    existing.add(guid)
    for child in item.children:
        _assign_fresh_guids(child, existing)


def do_deleteitem(body):
    """Delete one top-level character item, including socketed children."""
    path, idx = body["path"], int(body["item"])
    st = tables().stat_table()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)
    if idx < 0 or idx >= len(items):
        return {"error": f"item index out of range: {idx}"}
    removed = item_to_dict(items[idx])
    del items[idx]
    rebuilt = _rebuild_player(bytes(data), st, items, off, pend, pcount - 1)
    struct.pack_into("<H", rebuilt, off + 2, pcount - 1)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        out["removed"] = removed
        out["item_count"] = pcount - 1
    return out


def do_duplicateitem(body):
    """Duplicate one character item into the first free inventory location."""
    path, idx = body["path"], int(body["item"])
    st = tables().stat_table()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)
    if idx < 0 or idx >= len(items):
        return {"error": f"item index out of range: {idx}"}
    new = copy.deepcopy(items[idx])
    try:
        new.pos_x, new.pos_y, new.location_id, new.panel_id = _placement_for_code(items, new.type_code)
    except RuntimeError as e:
        return {"error": str(e)}
    new.equipped_id = 0
    existing = {getattr(it, "guid", 0) for it in items}
    for it in items:
        existing.update(getattr(ch, "guid", 0) for ch in it.children)
    _assign_fresh_guids(new, existing)
    items.append(new)
    rebuilt = _rebuild_player(bytes(data), st, items, off, pend, pcount + 1)
    struct.pack_into("<H", rebuilt, off + 2, pcount + 1)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        out["added"] = item_to_dict(new)
        out["item_count"] = pcount + 1
        out["index"] = len(items) - 1
    return out


def do_editchar(body):
    path = body["path"]
    updates = body.get("stats", {})
    data = bytearray(_read_bytes(path))
    start, end, entries, _values = _char_stat_block(bytes(data))
    by_key = {entry["key"]: entry for entry in entries}
    changed = {}
    for key, value in updates.items():
        if key not in CHAR_STAT_BY_KEY:
            continue
        if key not in by_key:
            sid, label, bits, scale = CHAR_STAT_BY_KEY[key]
            by_key[key] = {
                "id": sid, "key": key, "label": label,
                "bits": bits, "scale": scale, "raw": 0, "value": 0,
            }
            entries.append(by_key[key])
        entry = by_key[key]
        val = int(value)
        if val < 0:
            val = 0
        raw = val * int(entry["scale"])
        max_raw = (1 << int(entry["bits"])) - 1
        if raw > max_raw:
            raw = max_raw
            val = raw // int(entry["scale"])
        entry["raw"] = raw
        entry["value"] = val
        changed[key] = val
    if not changed:
        return {"error": "no editable character stats supplied"}

    entries.sort(key=lambda entry: int(entry["id"]))
    stat_bytes = _serialize_char_stats(entries)
    data = bytearray(bytes(data[:start + 2]) + stat_bytes + bytes(data[end:]))
    if "level" in changed:
        data[0x2B] = max(1, min(99, int(changed["level"])))
    _finalize(data)
    out = _gate_and_write(data, path)
    if out.get("ok"):
        out["character_stats"] = character_stats(bytes(data))
        out["changed"] = changed
    return out


def do_editskills(body):
    path = body["path"]
    updates = body.get("skills", {})
    data = bytearray(_read_bytes(path))
    off, end, skills = _skill_block(bytes(data))
    by_id = {int(skill["id"]): skill for skill in skills}
    by_index = {int(skill["index"]): skill for skill in skills}
    changed = {}
    for key, value in updates.items():
        try:
            nkey = int(key)
        except (TypeError, ValueError):
            continue
        skill = by_id.get(nkey) or by_index.get(nkey)
        if not skill:
            continue
        level = max(0, min(255, int(value)))
        data[off + 2 + int(skill["index"])] = level
        changed[str(skill["id"])] = level
    if not changed:
        return {"error": "no editable skills supplied"}
    _finalize(data)
    out = _gate_and_write(data, path)
    if out.get("ok"):
        out["skills"] = character_skills(bytes(data))
        out["changed"] = changed
    return out


def do_editwaypoints(body):
    path = body["path"]
    updates = body.get("waypoints", {})
    data = bytearray(_read_bytes(path))
    _off, _size, records = _waypoint_block(bytes(data))
    changed = {}
    for diff_key, value in updates.items():
        try:
            diff = int(diff_key)
        except (TypeError, ValueError):
            continue
        if diff < 0 or diff >= 3:
            continue
        if isinstance(value, dict):
            unlocked = {int(k) for k, v in value.items() if bool(v)}
        else:
            unlocked = {int(v) for v in (value or [])}
        mask = 0
        for idx in unlocked:
            if 0 <= idx < len(WAYPOINTS):
                mask |= 1 << idx
        start = records[diff]["start"]
        data[start + 2:start + 7] = int(mask).to_bytes(5, "little")
        changed[str(diff)] = sorted(unlocked)
    if not changed:
        return {"error": "no waypoint changes supplied"}
    _finalize(data)
    out = _gate_and_write(data, path)
    if out.get("ok"):
        out["waypoints"] = character_waypoints(bytes(data))
        out["changed"] = changed
    return out


def do_editquests(body):
    path = body["path"]
    updates = body.get("quests", {})
    data = bytearray(_read_bytes(path))
    _off, _size, records = _quest_block(bytes(data))
    changed = {}
    for diff_key, values in updates.items():
        try:
            diff = int(diff_key)
        except (TypeError, ValueError):
            continue
        if diff < 0 or diff >= 3 or not isinstance(values, dict):
            continue
        start = records[diff]["start"]
        diff_changed = {}
        for idx_key, raw_value in values.items():
            try:
                idx = int(idx_key)
                if isinstance(raw_value, str):
                    value = int(raw_value.strip().removeprefix("0x"), 16)
                else:
                    value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < 48:
                value = max(0, min(0xFFFF, value))
                struct.pack_into("<H", data, start + idx * 2, value)
                diff_changed[str(idx)] = f"{value:04x}"
        if diff_changed:
            changed[str(diff)] = diff_changed
    if not changed:
        return {"error": "no quest flag changes supplied"}
    _finalize(data)
    out = _gate_and_write(data, path)
    if out.get("ok"):
        out["quests"] = character_quests(bytes(data))
        out["changed"] = changed
    return out


def _dims_for(it):
    meta = item_meta().get(it.type_code, {})
    return max(1, int(meta.get("width", 1))), max(1, int(meta.get("height", 1)))


def _dims_for_code(code: str):
    meta = item_meta().get(code, {})
    return max(1, int(meta.get("width", 1))), max(1, int(meta.get("height", 1)))


def _occupied_cells(items, skip_idx: int | None = None):
    occupied = {}
    for i, it in enumerate(items):
        if skip_idx is not None and i == skip_idx:
            continue
        if it.location_id != 0 or it.panel_id != 1:
            continue
        w, h = _dims_for(it)
        for dy in range(h):
            for dx in range(w):
                occupied[(it.pos_x + dx, it.pos_y + dy)] = _display_name(it)
    return occupied


def _inventory_collision(items, moving_idx: int, x: int, y: int, W=10, H=8):
    moving = items[moving_idx]
    mw, mh = _dims_for(moving)
    if x < 0 or y < 0 or x + mw > W or y + mh > H:
        return f"{_display_name(moving)} does not fit at {x},{y}"

    occupied = _occupied_cells(items, moving_idx)

    for dy in range(mh):
        for dx in range(mw):
            name = occupied.get((x + dx, y + dy))
            if name:
                return f"target cell {x + dx},{y + dy} is occupied by {name}"
    return ""


def _placement_for_code(items, code: str, x: int | None = None, y: int | None = None, W=10, H=8):
    w, h = _dims_for_code(code)
    occupied = _occupied_cells(items)

    def fits(px: int, py: int):
        if px < 0 or py < 0 or px + w > W or py + h > H:
            return False
        return all((px + dx, py + dy) not in occupied for dy in range(h) for dx in range(w))

    if x is not None and y is not None:
        if not fits(x, y):
            raise RuntimeError(f"{code} does not fit at {x},{y}")
        return x, y, 0, 1

    for py in range(H):
        for px in range(W):
            if fits(px, py):
                return px, py, 0, 1
    raise RuntimeError("inventory full")


def do_moveitem(body):
    """Move a character item to an inventory grid position and validate the save."""
    path, idx = body["path"], int(body["item"])
    st = tables().stat_table()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)
    if idx < 0 or idx >= len(items):
        return {"error": f"item index out of range: {idx}"}

    if "x" in body and "y" in body:
        x, y = int(body["x"]), int(body["y"])
    else:
        try:
            x, y, _loc, _panel = _placement_for_code(
                [it for i, it in enumerate(items) if i != idx], items[idx].type_code)
        except RuntimeError as e:
            return {"error": str(e)}

    err = _inventory_collision(items, idx, x, y)
    if err:
        return {"error": err}

    it = items[idx]
    it.pos_x, it.pos_y = x, y
    it.location_id, it.panel_id = 0, 1
    it.equipped_id = 0

    rebuilt = _rebuild_player(bytes(data), st, items, off, pend, pcount)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        _, _, it2, _ = _walk_player(bytes(rebuilt), st)
        out["item"] = item_to_dict(it2[idx])
    return out


def _equip_compatible(it, slot: int) -> bool:
    meta = item_meta().get(it.type_code, {})
    label = str(meta.get("type_label", "")).lower()
    category = meta.get("category", "")
    raw_type = str(meta.get("type", "")).lower()
    if slot == 1:
        return "helm" in label
    if slot == 2:
        return "amulet" in label
    if slot == 3:
        return "armor" in label
    if slot in (4, 11):
        return category == "weapon"
    if slot in (5, 12):
        return category == "weapon" or "shield" in label or raw_type == "shie"
    if slot in (6, 7):
        return "ring" in label
    if slot == 8:
        return "belt" in label
    if slot == 9:
        return "boot" in label
    if slot == 10:
        return "glove" in label
    return False


def do_equipitem(body):
    path, idx = body["path"], int(body["item"])
    slot = int(body["slot"])
    if slot not in EQUIP_SLOTS:
        return {"error": f"unknown equipment slot {slot}"}
    st = tables().stat_table()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)
    if idx < 0 or idx >= len(items):
        return {"error": f"item index out of range: {idx}"}
    it = items[idx]
    if not _equip_compatible(it, slot):
        return {"error": f"{_display_name(it)} cannot be equipped in {EQUIP_SLOTS[slot]}"}
    occupant = next((other for i, other in enumerate(items)
                     if i != idx and other.location_id == 1 and other.equipped_id == slot), None)
    if occupant:
        return {"error": f"{EQUIP_SLOTS[slot]} is occupied by {_display_name(occupant)}"}
    it.location_id = 1
    it.equipped_id = slot
    it.panel_id = 0
    it.pos_x = slot
    it.pos_y = 0
    rebuilt = _rebuild_player(bytes(data), st, items, off, pend, pcount)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        _, _, it2, _ = _walk_player(bytes(rebuilt), st)
        out["item"] = item_to_dict(it2[idx])
    return out


def _shared_tab_groups(stash):
    """PD2 shared stash: a flat item list where equipped_id is the tab number.
    Returns [(tab, [items])] sorted by tab; sublists alias the real item objects."""
    items = getattr(getattr(stash, "flat_list", None), "items", []) or []
    tabs = {}
    for it in items:
        tabs.setdefault(int(getattr(it, "equipped_id", 0)), []).append(it)
    return sorted(tabs.items())


def _stash_page_list(stash):
    if getattr(stash, "kind", "") == "shared55bb":
        return [items for _tab, items in _shared_tab_groups(stash)]
    return [page.items for page in (stash.pages or [])]


def _stash_collision(items, moving_idx: int, x: int, y: int, W=10, H=15):
    moving = items[moving_idx]
    mw, mh = _dims_for(moving)
    if x < 0 or y < 0 or x + mw > W or y + mh > H:
        return f"{_display_name(moving)} does not fit at {x},{y}"
    occupied = {}
    for i, it in enumerate(items):
        if i == moving_idx:
            continue
        w, h = _dims_for(it)
        for dy in range(h):
            for dx in range(w):
                occupied[(it.pos_x + dx, it.pos_y + dy)] = _display_name(it)
    for dy in range(mh):
        for dx in range(mw):
            name = occupied.get((x + dx, y + dy))
            if name:
                return f"target cell {x + dx},{y + dy} is occupied by {name}"
    return ""


def _stash_placement(items, code: str, W=10, H=15):
    w, h = _dims_for_code(code)
    occupied = {}
    for it in items:
        iw, ih = _dims_for(it)
        for dy in range(ih):
            for dx in range(iw):
                occupied[(it.pos_x + dx, it.pos_y + dy)] = _display_name(it)
    for py in range(H):
        for px in range(W):
            if px + w > W or py + h > H:
                continue
            if all((px + dx, py + dy) not in occupied for dy in range(h) for dx in range(w)):
                return px, py
    raise RuntimeError("stash page full")


def do_movestashitem(body):
    path = body["path"]
    page_idx, item_idx = int(body["page"]), int(body["item"])
    x, y = int(body["x"]), int(body["y"])
    st = tables().stat_table()
    raw = bytes(_read_bytes(path))
    stash = stash_mod.parse_stash(raw, st)
    pages = _stash_page_list(stash)
    if page_idx < 0 or page_idx >= len(pages):
        return {"error": f"stash page out of range: {page_idx}"}
    items = pages[page_idx]
    if item_idx < 0 or item_idx >= len(items):
        return {"error": f"stash item out of range: {item_idx}"}
    err = _stash_collision(items, item_idx, x, y)
    if err:
        return {"error": err}
    it = items[item_idx]
    it.pos_x, it.pos_y = x, y
    it.location_id = 0
    if getattr(stash, "kind", "") != "shared55bb":
        # PlugY page placement; the shared55bb stash keeps panel_id=6 and
        # uses equipped_id as the tab number — never clobber those.
        it.panel_id = 5
        it.equipped_id = 0
    rebuilt = stash_mod.serialize_stash(stash, st)
    out = _gate_and_write_stash(rebuilt, path)
    if out.get("ok"):
        out["item"] = item_to_dict(it)
    return out


def do_copyitemtostash(body):
    """Copy a character item into a stash page. Source character is untouched."""
    char_path = body["path"]
    stash_path = body["stash_path"]
    idx = int(body["item"])
    page_idx = int(body.get("page", 0))
    st = tables().stat_table()
    char_raw = bytes(_read_bytes(char_path))
    _off, _pcount, char_items, _pend = _walk_player(char_raw, st)
    if idx < 0 or idx >= len(char_items):
        return {"error": f"item index out of range: {idx}"}

    stash_raw = bytes(_read_bytes(stash_path))
    stash = stash_mod.parse_stash(stash_raw, st)
    pages = _stash_page_list(stash)
    if page_idx < 0 or page_idx >= len(pages):
        return {"error": f"stash page out of range: {page_idx}"}
    dest_items = pages[page_idx]
    new = copy.deepcopy(char_items[idx])
    try:
        new.pos_x, new.pos_y = _stash_placement(dest_items, new.type_code)
    except RuntimeError as e:
        return {"error": str(e)}
    new.location_id = 0
    if getattr(stash, "kind", "") == "shared55bb":
        new.panel_id = 6
        new.equipped_id = _shared_tab_groups(stash)[page_idx][0]
    else:
        new.panel_id = 5
        new.equipped_id = 0
    existing = {getattr(it, "guid", 0) for page_items in pages for it in page_items}
    for page_items in pages:
        for it in page_items:
            existing.update(getattr(ch, "guid", 0) for ch in it.children)
    _assign_fresh_guids(new, existing)
    if getattr(stash, "kind", "") == "shared55bb" and stash.flat_list is not None:
        # dest_items is a per-tab view; the serialized list is flat_list.items
        stash.flat_list.items.append(new)
        stash.flat_list.count = len(stash.flat_list.items)
        stash.flat_list_header = b"JM" + struct.pack("<H", len(stash.flat_list.items))
    else:
        dest_items.append(new)
        if getattr(stash, "pages", None):
            stash.pages[page_idx].count = len(dest_items)
            stash.pages[page_idx].prologue = (
                stash.pages[page_idx].prologue[:-2]
                + struct.pack("<H", len(dest_items))
            )
    rebuilt = stash_mod.serialize_stash(stash, st)
    out = _gate_and_write_stash(rebuilt, stash_path)
    if out.get("ok"):
        out["item"] = item_to_dict(new)
        out["page"] = page_idx
    return out


def do_copystashitemtochar(body):
    """Copy one stash-page item into a character inventory. Source stash is untouched."""
    stash_path = body["path"]
    char_path = body["char_path"]
    page_idx = int(body.get("page", 0))
    item_idx = int(body["item"])
    st = tables().stat_table()

    stash_raw = bytes(_read_bytes(stash_path))
    stash = stash_mod.parse_stash(stash_raw, st)
    pages = _stash_page_list(stash)
    if page_idx < 0 or page_idx >= len(pages):
        return {"error": f"stash page out of range: {page_idx}"}
    stash_items = pages[page_idx]
    if item_idx < 0 or item_idx >= len(stash_items):
        return {"error": f"stash item out of range: {item_idx}"}

    data = bytearray(_read_bytes(char_path))
    off, pcount, char_items, pend = _walk_player(bytes(data), st)
    new = copy.deepcopy(stash_items[item_idx])
    try:
        new.pos_x, new.pos_y, new.location_id, new.panel_id = _placement_for_code(char_items, new.type_code)
    except RuntimeError as e:
        return {"error": str(e)}
    new.equipped_id = 0
    existing = {getattr(it, "guid", 0) for it in char_items}
    for it in char_items:
        existing.update(getattr(ch, "guid", 0) for ch in it.children)
    _assign_fresh_guids(new, existing)
    char_items.append(new)

    rebuilt = _rebuild_player(bytes(data), st, char_items, off, pend, pcount + 1)
    struct.pack_into("<H", rebuilt, off + 2, pcount + 1)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, char_path)
    if out.get("ok"):
        out["item"] = item_to_dict(new)
        out["index"] = len(char_items) - 1
        out["item_count"] = pcount + 1
    return out


def do_copysectionitemtochar(body):
    """Copy a non-player character-section item into that character's inventory."""
    char_path = body["path"]
    section_id = str(body.get("section", "")).strip()
    item_idx = int(body["item"])
    if not section_id or section_id == "player":
        return {"error": "choose a mercenary, corpse, or golem section item"}
    st = tables().stat_table()
    data = bytearray(_read_bytes(char_path))
    sections = _character_item_sections(bytes(data), st)
    source = next((sec for sec in sections if sec.get("id") == section_id), None)
    if source is None:
        return {"error": f"section not found: {section_id}"}
    source_items = source.get("items", [])
    if item_idx < 0 or item_idx >= len(source_items):
        return {"error": f"section item out of range: {item_idx}"}

    off, pcount, char_items, pend = _walk_player(bytes(data), st)
    new = copy.deepcopy(source_items[item_idx])
    try:
        new.pos_x, new.pos_y, new.location_id, new.panel_id = _placement_for_code(char_items, new.type_code)
    except RuntimeError as e:
        return {"error": str(e)}
    new.equipped_id = 0
    existing = {getattr(it, "guid", 0) for it in char_items}
    for it in char_items:
        existing.update(getattr(ch, "guid", 0) for ch in it.children)
    _assign_fresh_guids(new, existing)
    char_items.append(new)

    rebuilt = _rebuild_player(bytes(data), st, char_items, off, pend, pcount + 1)
    struct.pack_into("<H", rebuilt, off + 2, pcount + 1)
    _finalize(rebuilt)
    out = _gate_and_write(rebuilt, char_path)
    if out.get("ok"):
        out["item"] = item_to_dict(new)
        out["index"] = len(char_items) - 1
        out["source"] = {"section": section_id, "name": source.get("name", section_id), "item": item_idx}
        out["item_count"] = pcount + 1
    return out


def do_editsectionitem(body):
    """Edit an item inside a non-player character section and validate/reparse."""
    path = body["path"]
    section_id = str(body.get("section", "")).strip()
    idx = int(body["item"])
    if not section_id or section_id == "player":
        return {"error": "choose a mercenary, corpse, or golem section item"}
    st = tables().stat_table()
    data = bytearray(_read_bytes(path))
    sections = _character_item_sections(bytes(data), st)
    section = next((sec for sec in sections if sec.get("id") == section_id), None)
    if section is None:
        return {"error": f"section not found: {section_id}"}
    if not section.get("editable"):
        return {"error": f"{section.get('name', section_id)} is not editable yet"}
    items = section.get("items", [])
    if idx < 0 or idx >= len(items):
        return {"error": f"section item out of range: {idx}"}
    try:
        changed = _apply_item_edit(items[idx], body, st)
    except ValueError as e:
        return {"error": str(e)}

    rebuilt = _rebuild_item_list_region(
        bytes(data), st, items, int(section["offset"]), int(section["end"]))
    _finalize(rebuilt)
    try:
        reparsed_sections = _character_item_sections(bytes(rebuilt), st)
        reparsed = next((sec for sec in reparsed_sections if sec.get("id") == section_id), None)
        if reparsed is None or idx >= len(reparsed.get("items", [])):
            return {"error": f"{section.get('name', section_id)} could not be re-parsed after edit"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"section reparse failed after edit: {e}"}

    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        out["item"] = item_to_dict(reparsed["items"][idx])
        out["section"] = {"id": section_id, "name": section.get("name", section_id)}
        out["changed"] = changed
    return out


def do_additem(body):
    """Build an item from scratch and insert it into the player inventory."""
    path = body["path"]
    code = body["code"]
    quality = int(body.get("quality", 4))
    want_stats = body.get("stats", [])  # [{stat_id, value}]
    st = tables().stat_table()
    gt = tables()
    data = bytearray(_read_bytes(path))
    off, pcount, items, pend = _walk_player(bytes(data), st)

    # Use an existing clean item of the same broad kind as a structural skeleton,
    # then overwrite its identity. (Synthesis is byte-identical to real items.)
    target_category = item_meta().get(code, {}).get("category", "")
    tmpl = next((it for it in items if it.clean and it.type_code == code), None)
    if tmpl is None:
        tmpl = next((it for it in items if it.clean and not it.is_ear
                     and item_meta().get(it.type_code, {}).get("category", "") == target_category), None)
    if tmpl is None:
        return {"error": f"no usable {target_category or 'item'} template in this save"}

    new = copy.deepcopy(tmpl)
    new.type_code = code
    schema = getattr(st, "schema", None)
    new.is_armor = bool(schema) and code in schema.armor_codes
    new.is_weapon = bool(schema) and code in schema.weapon_codes
    new.is_stackable = bool(schema) and code in schema.stackable_codes
    new.quality = quality
    new.children = []
    new.num_in_sockets = 0
    new.socketed = False
    new.num_sockets = 0
    new.identified = bool(body.get("identified", True))
    new.prefix = -1
    new.suffix = -1
    new.set_unique_id = -1
    new.rare_affixes = []
    generated_specs = []
    if quality == 4:
        new.prefix = max(0, min(2047, int(body.get("magic_prefix", 0) or 0)))
        new.suffix = max(0, min(2047, int(body.get("magic_suffix", 0) or 0)))
        mp = gt.load_table("MagicPrefix")
        ms = gt.load_table("MagicSuffix")
        if 0 <= new.prefix < len(mp):
            generated_specs.extend(_property_specs(mp[new.prefix], "mod", 3))
        if 0 <= new.suffix < len(ms):
            generated_specs.extend(_property_specs(ms[new.suffix], "mod", 3))
    elif quality in (5, 7):
        selected = body.get("set_id" if quality == 5 else "unique_id", body.get("set_unique_id", -1))
        if selected is None or int(selected) < 0:
            return {"error": "choose a matching set/unique item for this quality"}
        table = "SetItems" if quality == 5 else "UniqueItems"
        rows = gt.load_table(table)
        # the UI sends the TABLE ROW index; the file stores the GAME id, which
        # skips the 'Expansion' separator row — otherwise the item shows the next
        # unique's name in-game (e.g. Bartuc's row 287 -> Jalal's Mane).
        row_idx = max(0, min(len(rows) - 1, int(selected)))
        if row_idx >= len(rows):
            return {"error": f"{table} row out of range: {row_idx}"}
        row_code = (rows[row_idx].get("item", "") if quality == 5 else rows[row_idx].get("code", "")).strip()
        if row_code and row_code != code:
            return {"error": f"{rows[row_idx].get('index', table)} is for {row_code}, not {code}"}
        generated_specs.extend(_property_specs(rows[row_idx], "prop", 12))
        new.set_unique_id = _row_to_gameid(table, row_idx)
    elif quality in (6, 8):
        new.prefix = max(0, min(255, int(body.get("rare_prefix", 156) or 156)))
        new.suffix = max(0, min(255, int(body.get("rare_suffix", 0) or 0)))
        raw_affixes = body.get("rare_affixes", []) or []
        mp = gt.load_table("MagicPrefix")
        ms = gt.load_table("MagicSuffix")
        affixes = []
        for entry in raw_affixes[:6]:
            if entry is None or str(entry) == "":
                affixes.append(None)
            else:
                if isinstance(entry, dict):
                    val = max(0, min(2047, int(entry.get("id", 0))))
                    table = entry.get("table", "")
                else:
                    val = max(0, min(2047, int(entry)))
                    table = ""
                affixes.append(val)
                if table == "MagicSuffix" and 0 <= val < len(ms):
                    generated_specs.extend(_property_specs(ms[val], "mod", 3))
                elif table == "MagicPrefix" and 0 <= val < len(mp):
                    generated_specs.extend(_property_specs(mp[val], "mod", 3))
                elif 0 <= val < len(mp):
                    generated_specs.extend(_property_specs(mp[val], "mod", 3))
                elif 0 <= val < len(ms):
                    generated_specs.extend(_property_specs(ms[val], "mod", 3))
        while len(affixes) < 6:
            affixes.append(None)
        new.rare_affixes = affixes
    if "ilvl" in body:
        new.ilvl = max(1, min(127, int(body.get("ilvl") or 1)))
    runeword_row = None
    if body.get("runeword_id") is not None:
        rw_idx = int(body.get("runeword_id"))
        rows = gt.load_table("Runes")
        if rw_idx < 0 or rw_idx >= len(rows):
            return {"error": f"runeword row out of range: {rw_idx}"}
        runeword_row = rows[rw_idx]
        compatible, reason = _runeword_compatibility(code, runeword_row)
        if not compatible:
            return {"error": reason}
        runes = _runeword_runes(runeword_row)
        if not runes:
            return {"error": "selected runeword has no rune sequence"}
        if len(runes) > 6:
            return {"error": "selected runeword has too many runes"}
        if quality not in (2, 3):
            new.quality = 3 if quality == 3 else 2
        new.runeword = True
        new.runeword_id = _runeword_save_id(runeword_row, rw_idx)
        new.runeword_stats = _simple_stats_from_specs(_runeword_specs(runeword_row), new.version, gt)
        if int(getattr(new, "version", 0)) >= 0x67:
            new.v103_trailers = [1, 1]
        new.simple = False
        new.socketed = True
        new.num_sockets = len(runes)
        new.num_in_sockets = len(runes)
        filler_codes = {row["code"] for row in browse("socket_fillers").get("socket_fillers", [])}
        bad = [code for code in runes if code not in filler_codes]
        if bad:
            return {"error": f"runeword references unknown socket fillers: {', '.join(bad)}"}
        child_template = next((it for it in items if it.clean and it.simple and not it.is_ear), None)
        if child_template is None:
            for it in items:
                for ch in it.children:
                    if ch.clean and ch.simple and not ch.is_ear:
                        child_template = ch
                        break
                if child_template is not None:
                    break
        if child_template is None:
            return {"error": "no usable simple item template for socketed runes in this save"}
        new.children = [
            _socket_child_from_template(child_template, rune_code, pos)
            for pos, rune_code in enumerate(runes)
        ]
    # fresh guid not colliding
    egu = {getattr(it, "guid", 0) for it in items}
    g = 0x70000000
    while g in egu:
        g += 1
    new.guid = g
    # placement: requested cell when supplied, otherwise first free inventory fit
    px = int(body["x"]) if "x" in body else None
    py = int(body["y"]) if "y" in body else None
    try:
        new.pos_x, new.pos_y, new.location_id, new.panel_id = _placement_for_code(items, code, px, py)
    except RuntimeError as e:
        return {"error": str(e)}
    # stats
    new.stats = _simple_stats_from_specs(generated_specs, new.version, gt)
    for sd in want_stats:
        sid = int(sd["stat_id"])
        s = _item_stat_from_value(sid, int(sd["value"]), new.version, gt)
        if s:
            new.stats.append(s)

    item_bytes = _pack(iv.serialize_item_typed(new, st))
    for ch in new.children:
        item_bytes += _pack(iv.serialize_item_typed(ch, st))
    # splice at true player end, bump count
    rebuilt = bytearray(bytes(data[:pend]) + item_bytes + bytes(data[pend:]))
    struct.pack_into("<H", rebuilt, off + 2, pcount + 1)
    _finalize(rebuilt)
    try:
        _, _, preview_items, _ = _walk_player(bytes(rebuilt), st)
        preview_item = preview_items[pcount]
    except Exception as e:  # noqa: BLE001
        return {"error": f"generated item could not be re-parsed: {e}"}
    if not getattr(preview_item, "clean", False) or not getattr(preview_item, "decode_ok", False):
        return {"error": "generated item was rejected by the item decoder; try a different base/template"}
    out = _gate_and_write(rebuilt, path)
    if out.get("ok"):
        out["added"] = item_to_dict(preview_item)
        out["generated_stats"] = len(new.stats)
        out["generated_runeword_stats"] = len(getattr(new, "runeword_stats", []) or [])
    return out


def _free_cell(items, W=10, H=8):
    dims = {}
    gt = tables()
    for t in ("Armor", "Weapons", "Misc"):
        for r in gt.load_table(t):
            c = r.get("code", "").strip()
            w = r.get("invwidth", "").strip()
            h = r.get("invheight", "").strip()
            if c and w and h:
                try:
                    dims[c] = (int(w), int(h))
                except ValueError:
                    pass
    grid = [[0] * W for _ in range(H)]
    for it in items:
        if it.location_id != 0 or it.panel_id != 1:
            continue
        w, h = dims.get(it.type_code, (1, 1))
        for dy in range(h):
            for dx in range(w):
                if 0 <= it.pos_y + dy < H and 0 <= it.pos_x + dx < W:
                    grid[it.pos_y + dy][it.pos_x + dx] = 1
    for y in range(H):
        for x in range(W):
            if grid[y][x] == 0:
                return x, y, 0, 1
    raise RuntimeError("inventory full")


def _rebuild_player(data, st, items, off, pend, pcount):
    out = bytearray(data[:(off + 4)])
    for it in items:
        out += _pack(iv.serialize_item_typed(it, st))
        for ch in it.children:
            out += _pack(iv.serialize_item_typed(ch, st))
    out += data[pend:]
    return out


def _rebuild_item_list_region(data, st, items, off: int, end: int):
    out = bytearray(data[:(off + 4)])
    for it in items:
        out += _pack(iv.serialize_item_typed(it, st))
        for ch in it.children:
            out += _pack(iv.serialize_item_typed(ch, st))
    out += data[end:]
    return out


def do_validate(body):
    st = tables().stat_table()
    res = validate_mod.validate_file(body["path"], st)
    return {"valid": res.ok, "errors": res.errors, "warnings": res.warnings,
            "items": res.item_count, "sections": res.sections}


# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/health":
                self._json(_mpq_status())
            elif u.path == "/api/mpq":
                self._json(_mpq_status())
            elif u.path == "/api/pick":
                self._json(pick_path(q.get("kind", ["save"])[0]))
            elif u.path == "/api/save":
                p = q.get("path", [""])[0]
                if not p or not os.path.exists(p):
                    self._json({"error": f"not found: {p}"}, 404)
                else:
                    self._json(parse_save(p))
            elif u.path == "/api/browse":
                self._json(browse(q.get("kind", ["bases"])[0]))
            elif u.path == "/":
                body = SPA.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            import traceback
            self._json({"error": repr(e), "trace": traceback.format_exc()}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        routes = {"/api/edit": do_edit, "/api/edititem": do_edititem,
                  "/api/editsectionitem": do_editsectionitem,
                  "/api/unsocketitem": do_unsocketitem,
                  "/api/socketitem": do_socketitem,
                  "/api/deleteitem": do_deleteitem,
                  "/api/duplicateitem": do_duplicateitem,
                  "/api/editchar": do_editchar,
                  "/api/editskills": do_editskills,
                  "/api/editwaypoints": do_editwaypoints,
                  "/api/editquests": do_editquests,
                  "/api/equipitem": do_equipitem,
                  "/api/movestashitem": do_movestashitem,
                  "/api/copyitemtostash": do_copyitemtostash,
                  "/api/copystashitemtochar": do_copystashitemtochar,
                  "/api/copysectionitemtochar": do_copysectionitemtochar,
                  "/api/moveitem": do_moveitem,
                  "/api/maxroll": do_maxroll,
                  "/api/additem": do_additem, "/api/validate": do_validate,
                  "/api/mpq": lambda b: set_mpq(b.get("path", ""))}
        fn = routes.get(u.path)
        try:
            self._json(fn(body) if fn else {"error": "not found"},
                       200 if fn else 404)
        except Exception as e:  # noqa: BLE001
            import traceback
            self._json({"error": repr(e), "trace": traceback.format_exc()}, 500)


SPA = r"""<!doctype html><html><head><meta charset=utf-8>
<title>Cain</title>
<style>
 :root{--bg:#14141a;--panel:#1e1e26;--panel2:#262630;--gold:#c8a45c;--line:#33333e;--txt:#d8d8de}
 *{box-sizing:border-box}
 body{font:14px/1.4 system-ui,Segoe UI,sans-serif;margin:0;background:var(--bg);color:var(--txt)}
 header{background:linear-gradient(#2a2a33,#1e1e26);padding:12px 18px;border-bottom:2px solid #000;
   display:flex;gap:14px;align-items:center;box-shadow:0 2px 8px #0008}
 h1{font-size:17px;margin:0;color:var(--gold);letter-spacing:.5px}
 .bar{padding:10px 18px;background:var(--panel);display:flex;gap:8px;border-bottom:1px solid var(--line);align-items:end}
 .bar label{display:flex;flex-direction:column;gap:4px;min-width:0;flex:1;color:#999;font-size:11px}
 input,select{background:#0e0e12;color:var(--txt);border:1px solid #444;padding:7px 9px;border-radius:5px;font-size:13px}
 #path,#mpq{width:100%}
 button{background:#3a3a46;color:var(--txt);border:1px solid #565664;padding:7px 14px;cursor:pointer;
   border-radius:5px;font-size:13px;transition:.12s}
 button:hover{background:#4a4a5a;border-color:var(--gold)}
 button.primary{background:#5a4a2a;border-color:var(--gold);color:#f0d9a0}
 #meta{padding:9px 18px;color:#9aa;min-height:20px;font-size:13px}
 #meta .ok{color:#7ec77e}#meta .err{color:#e07070}
 #mpqstat{min-width:190px;color:#888;font-size:12px;padding-bottom:8px}
 .wrap{display:flex;gap:20px;padding:18px;align-items:flex-start;flex-wrap:wrap}
 .section{margin-bottom:18px}
 .section h2{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:0 0 8px}
 .grid{display:grid;gap:3px;background:#0a0a0e;padding:4px;border:1px solid var(--line);border-radius:6px;grid-auto-rows:42px}
 .cell{background:#1a1a22;border:1px solid #2a2a34;border-radius:3px}
 .cell.it{background:#26262e;border-color:#3a3a48;cursor:pointer;display:flex;align-items:center;
   justify-content:center;font-size:11px;color:var(--gold);font-weight:600;overflow:hidden;transition:.1s;padding:4px;text-align:center}
 .cell.it:hover{outline:2px solid var(--gold);z-index:5;background:#30303a}
 .cell.bg{pointer-events:none}
 .cell.q-unique{border-color:#a59263;color:#c7b377}.cell.q-set{border-color:#3ea53e;color:#5fd35f}
 .cell.q-magic{border-color:#5a7cd6;color:#7e9ce6}.cell.q-rare{border-color:#cfcc4a;color:#dfdc6a}
 .cell.bad{opacity:.5}
 .items{display:flex;flex-wrap:wrap;gap:8px}
 .card{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:9px;width:200px;font-size:12px}
 .card.q-unique{border-color:#a59263}.card.q-set{border-color:#3ea53e}
 .card.q-magic{border-color:#5a7cd6}.card.q-rare{border-color:#cfcc4a}
 .card .code{color:var(--gold);font-weight:700;font-size:13px}
 .card .q{font-size:10px;text-transform:uppercase;color:#888;margin-bottom:4px}
 .card .base{color:#bbb;font-size:11px;margin-bottom:6px}
 .stat{color:#8ab4ff;cursor:pointer;padding:1px 0}.stat:hover{color:#b8d0ff}
 .card.bad{opacity:.5}
 .detail{width:360px;max-width:100%;background:#16161c;border-left:1px solid var(--line);padding:0 0 0 16px}
 .detail h2{font-size:15px;color:var(--gold);margin:0 0 4px}
 .detail .sub{font-size:11px;color:#888;text-transform:uppercase;margin-bottom:10px}
 .detail .line{margin:3px 0}.detail .stat{cursor:default}
 .pageh{width:100%;color:var(--gold);font-weight:700;padding:4px 0;border-bottom:1px solid var(--line);margin-top:6px}
 #tip{position:fixed;display:none;background:#0c0c10;border:1px solid var(--gold);border-radius:6px;
   padding:10px;max-width:260px;font-size:12px;z-index:100;box-shadow:0 4px 16px #000c;pointer-events:none}
 #tip .code{color:var(--gold);font-weight:700}#tip .s{color:#8ab4ff}
 #builder{background:var(--panel);padding:14px 18px;display:none;gap:10px;flex-wrap:wrap;align-items:end;border-bottom:1px solid var(--line)}
 #builder.show{display:flex}
 #builder label{font-size:11px;color:#999;display:flex;flex-direction:column;gap:3px}
</style></head><body>
<header>
 <h1>&#9876; Cain</h1>
 <button onclick="toggleBuilder()">+ Add Item</button>
 <button onclick="validate()">Validate</button>
 <span style="margin-left:auto;color:#666;font-size:12px">writes to *.edited (source untouched)</span>
</header>
<div class=bar>
 <label>Game data MPQ
  <input id=mpq placeholder="path to pd2data.mpq" onkeydown="if(event.key=='Enter')useMpq()">
 </label>
 <button onclick="browseMpq()">Browse MPQ&hellip;</button>
 <button onclick="useMpq()">Use MPQ</button>
 <span id=mpqstat></span>
</div>
<div class=bar>
 <label>Save or stash
  <input id=path placeholder="path to .d2s / .d2x / .sss / .stash" onkeydown="if(event.key=='Enter')load()">
 </label>
 <button onclick=browseFile()>Browse Save&hellip;</button>
 <button class=primary onclick=load()>Open</button>
</div>
<div id=builder></div>
<div id=meta>Open a save to begin.</div>
<div class=wrap id=out></div>
<div id=tip></div>
<script>
let CURPATH='',BASES=[],STATS=[],DATA=null;
const QC={unique:'q-unique',set:'q-set',magic:'q-magic',rare:'q-rare'};
init();
async function init(){
 const d=await j('/api/mpq');showMpq(d);
 if(d.mpq)document.getElementById('mpq').value=d.mpq;
}
function showMpq(d){
 const s=document.getElementById('mpqstat');
 s.innerHTML=d.ok?'<span class=ok>'+d.stats+' stats loaded</span>':'<span class=err>MPQ not ready</span>';
 if(d.error)document.getElementById('meta').innerHTML='<span class=err>'+d.error+'</span>';
}
async function browseMpq(){
 let p='';
 if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_mpq)p=await window.pywebview.api.pick_mpq();
 else p=(await j('/api/pick?kind=mpq')).path||'';
 if(p){document.getElementById('mpq').value=p;await useMpq();}
}
async function browseFile(){
 let p='';
 if(window.pywebview&&window.pywebview.api)p=await window.pywebview.api.pick_file();
 else p=(await j('/api/pick?kind=save')).path||'';
 if(p){document.getElementById('path').value=p;load();}
}
async function useMpq(){
 BASES=[];STATS=[];
 const d=await post('/api/mpq',{path:document.getElementById('mpq').value.trim()});
 showMpq(d);
}
async function load(){
 CURPATH=document.getElementById('path').value.trim();if(!CURPATH)return;
 const d=await j('/api/save?path='+encodeURIComponent(CURPATH));DATA=d;
 const meta=document.getElementById('meta'),out=document.getElementById('out');out.innerHTML='';
 if(d.error){meta.innerHTML='<span class=err>'+d.error+'</span>';return}
 if(d.kind=='character'){
   meta.innerHTML='<b>'+d.name+'</b> &mdash; '+d.class+' L'+d.level+' <span style=color:#666>('+d.version+')</span> &mdash; '+d.clean+'/'+d.item_count+' decoded';
   renderChar(d,out);
 }else{
   meta.innerHTML='<b>'+d.kind+'</b> &mdash; '+d.pages.length+' pages';
   const wrap=document.createElement('div');wrap.style.width='100%';
   d.pages.forEach(pg=>{const h=document.createElement('div');h.className='pageh';
     h.textContent=(pg.name||'(page)')+' ('+pg.count+')';wrap.appendChild(h);
     const row=document.createElement('div');row.className='items';
     pg.items.forEach(it=>row.appendChild(listCard(it)));wrap.appendChild(row);});
   out.appendChild(wrap);
 }
}
function renderChar(d,out){
 const inv=d.items.map((it,i)=>({it,i})).filter(o=>o.it.panel===1);
 const other=d.items.map((it,i)=>({it,i})).filter(o=>o.it.panel!==1);
 const sec=document.createElement('div');sec.className='section';sec.innerHTML='<h2>Inventory</h2>';
 const g=document.createElement('div');g.className='grid';g.style.gridTemplateColumns='repeat(10,42px)';
 const W=10,H=8;
 for(let y=0;y<H;y++)for(let x=0;x<W;x++){
   const c=document.createElement('div');c.className='cell bg';
   c.style.gridColumn=(x+1);c.style.gridRow=(y+1);
   g.appendChild(c);
 }
 inv.forEach(o=>{
   const c=document.createElement('div');
   c.className='cell it '+(QC[o.it.quality]||'')+(o.it.clean?'':' bad');
   c.style.gridColumn=(o.it.pos_x+1)+' / span '+Math.max(1,o.it.width||1);
   c.style.gridRow=(o.it.pos_y+1)+' / span '+Math.max(1,o.it.height||1);
   c.innerHTML='<span>'+shortName(o.it)+'</span>';
   bindItem(c,o.it,o.i);g.appendChild(c);
 });
 sec.appendChild(g);out.appendChild(sec);
 const detail=document.createElement('div');detail.id='detail';detail.className='detail';
 detail.innerHTML='<h2>Select an item</h2><div class=sub>Inventory, equipped, stash, and charms</div>';
 out.appendChild(detail);
 if(other.length){const s2=document.createElement('div');s2.className='section';
   s2.innerHTML='<h2>Equipped &amp; Other</h2>';
   const row=document.createElement('div');row.className='items';
   other.forEach(o=>row.appendChild(listCard(o.it,o.i)));s2.appendChild(row);out.appendChild(s2);}
}
function listCard(it,idx){
 const e=document.createElement('div');e.className='card '+(QC[it.quality]||'')+(it.clean?'':' bad');
 let h='<div class=code>'+it.name+'</div><div class=base>'+it.base_name+' &middot; '+it.type_label+'</div><div class=q>'+it.quality;
 if(it.ethereal)h+=' &middot; eth';if(it.num_sockets)h+=' &middot; '+it.num_sockets+'os';h+='</div>';
 if(it.defense!=null)h+='<div>def '+it.defense+'</div>';
 if(it.durability)h+='<div>dur '+it.durability+'</div>';
 if(it.quantity!=null)h+='<div>qty '+it.quantity+'</div>';
 (it.stats||[]).forEach((s,si)=>h+='<div class=stat data-si='+si+'>'+s.text+'</div>');
 if(!it.clean)h+='<div class=err>not fully decoded</div>';
 e.innerHTML=h;
 e.onclick=()=>showDetail(it,idx);
 if(idx!=null&&it.clean){
   e.querySelectorAll('.stat').forEach((el,si)=>el.onclick=()=>{
     const s=it.stats[si];const v=prompt(s.name+' =',s.value);
     if(v!=null)editStat(idx,s.id,parseInt(v));});
   const b=document.createElement('button');b.textContent='Max Roll';
   b.style.cssText='margin-top:6px;font-size:11px;padding:3px 9px';
   b.onclick=()=>maxroll(idx);e.appendChild(b);
 }
 return e;
}
function bindItem(c,it,idx){
 c.onmouseenter=e=>showTip(it,e);c.onmousemove=e=>moveTip(e);c.onmouseleave=hideTip;
 c.onclick=()=>showDetail(it,idx);
}
function shortName(it){return (it.type_label||it.base_name||it.type_code).replace(' Charm','');}
function showDetail(it,idx){
 const d=document.getElementById('detail');if(!d)return;
 let h='<h2>'+it.name+'</h2><div class=sub>'+it.quality+' '+it.base_name;
 if(it.ethereal)h+=' &middot; ethereal';if(it.num_sockets)h+=' &middot; '+it.num_sockets+' sockets';h+='</div>';
 if(it.defense!=null)h+='<div class=line>Defense: '+it.defense+'</div>';
 if(it.durability)h+='<div class=line>Durability: '+it.durability+'</div>';
 if(it.quantity!=null)h+='<div class=line>Quantity: '+it.quantity+'</div>';
 h+='<div class=line>Item level: '+it.ilvl+'</div>';
 (it.stats||[]).forEach(s=>h+='<div class=stat>'+s.text+'</div>');
 if(idx!=null&&it.clean)h+='<button style="margin-top:10px" onclick="maxroll('+idx+')">Max Roll</button>';
 d.innerHTML=h;
}
function showTip(it,e){const t=document.getElementById('tip');
 let h='<div class=code>'+it.name+'</div><div style="color:#888;font-size:10px;text-transform:uppercase">'+it.quality+' '+it.base_name+'</div>';
 if(it.defense!=null)h+='<div>Defense: '+it.defense+'</div>';
 if(it.durability)h+='<div>Durability: '+it.durability+'</div>';
 (it.stats||[]).forEach(s=>h+='<div class=s>'+s.text+'</div>');
 if(!it.clean)h+='<div style=color:#e07070>not fully decoded</div>';
 t.innerHTML=h;t.style.display='block';moveTip(e);}
function moveTip(e){const t=document.getElementById('tip');
 t.style.left=Math.min(e.clientX+14,innerWidth-280)+'px';t.style.top=(e.clientY+14)+'px';}
function hideTip(){document.getElementById('tip').style.display='none';}
async function editStat(idx,sid,val){done(await post('/api/edit',{path:CURPATH,item:idx,stat_id:sid,value:val}));}
async function maxroll(idx){done(await post('/api/maxroll',{path:CURPATH,item:idx}));}
function done(d){const m=document.getElementById('meta');
 if(d.error){m.innerHTML='<span class=err>'+d.error+(d.details?': '+d.details.join('; '):'')+'</span>';return}
 m.innerHTML='<span class=ok>&#10003; saved &rarr; '+fn(d.out)+' (validated, source untouched)</span> '+
   '<button style="font-size:11px;padding:3px 9px" onclick="reopen(\''+esc(d.out)+'\')">Open edited</button>';
}
function reopen(p){document.getElementById('path').value=p;load();}
async function toggleBuilder(){const b=document.getElementById('builder');b.classList.toggle('show');
 if(!b.classList.contains('show'))return;
 if(!BASES.length){BASES=(await j('/api/browse?kind=bases')).bases;STATS=(await j('/api/browse?kind=stats')).stats;}
 b.innerHTML='<label>Base<select id=bcode>'+
   BASES.map(x=>'<option value="'+x.code+'">'+x.code+' &mdash; '+x.name+' ('+x.cat+')</option>').join('')+'</select></label>'+
   '<label>Quality<select id=bq><option value=2>normal</option><option value=4 selected>magic</option>'+
   '<option value=7>unique</option></select></label>'+
   '<label>Stat<select id=bstat>'+
   STATS.map(s=>'<option value="'+s.id+'" data-max="'+s.max+'">'+s.name+' (max '+s.max+')</option>').join('')+'</select></label>'+
   '<label>Value<input id=bval type=number value=100 style=width:100px></label>'+
   '<button class=primary onclick=addItem()>Build + Insert</button>'+
   '<span style="font-size:11px;color:#888;align-self:center">item appears in the first free inventory cell</span>';
 document.getElementById('bstat').onchange=function(){
   document.getElementById('bval').value=this.selectedOptions[0].dataset.max;};
}
async function addItem(){
 const d=await post('/api/additem',{path:CURPATH,code:val('bcode'),quality:+val('bq'),
   stats:[{stat_id:+val('bstat'),value:+val('bval')}]});
 const m=document.getElementById('meta');
 if(d.error){m.innerHTML='<span class=err>'+d.error+(d.details?': '+d.details.join('; '):'')+'</span>';return}
 m.innerHTML='<span class=ok>&#10003; built '+d.added.type_code+' &rarr; '+fn(d.out)+' (validated)</span> '+
   '<button style="font-size:11px;padding:3px 9px" onclick="reopen(\''+esc(d.out)+'\')">Open edited</button>';
}
async function validate(){const d=await post('/api/validate',{path:CURPATH});const m=document.getElementById('meta');
 m.innerHTML=d.valid?'<span class=ok>&#10003; VALID &mdash; the game will load this ('+d.items+' items)</span>'
   :'<span class=err>&#10007; INVALID: '+(d.errors||[]).join('; ')+'</span>';
 if((d.warnings||[]).length)m.innerHTML+=' <span style=color:#d4a44a>&#9888; '+d.warnings.join('; ')+'</span>';}
function val(id){return document.getElementById(id).value;}
function fn(p){return (p||'').split(/[\\/]/).pop();}
function esc(p){return (p||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'");}
async function j(u){return (await fetch(u)).json();}
async function post(u,b){return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json();}
</script></body></html>"""


def main():
    global _mpq
    port = 8765
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port":
            port = int(args[i + 1])
        elif a == "--mpq":
            _mpq = args[i + 1]
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Cain GUI on http://localhost:{port}  (mpq={_mpq})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
