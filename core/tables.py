"""
Live table loader + stat-encoding schema.

Reads the .txt data tables straight from a PD2 MPQ (in memory) and builds the
schema the save codec needs — most importantly the stat-encoding map derived
from ItemStatCost.txt (id -> save bits / save add / save param bits).

Nothing is extracted to disk. This is the runtime "learn the format from the
install" step (see PD2_SAVE_EDITOR_PLAN.md §3.0).
"""
from __future__ import annotations

from dataclasses import dataclass

from .mpq import MPQArchive


EXCEL = r"data\global\excel"

# Tables the codec needs. Loaded lazily/on demand.
WANTED_TABLES = [
    "ItemStatCost", "Armor", "Weapons", "Misc", "ItemTypes",
    "MagicPrefix", "MagicSuffix", "UniqueItems", "SetItems",
    "Runes", "Properties", "Skills", "CharStats",
]


def parse_tsv(blob: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Parse a D2 tab-separated table into (columns, rows-as-dicts)."""
    text = blob.decode("latin-1")
    lines = text.split("\n")
    # strip trailing CR and drop a trailing empty line
    lines = [ln.rstrip("\r") for ln in lines]
    while lines and lines[-1] == "":
        lines.pop()
    header = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        cells = ln.split("\t")
        # tolerate ragged rows
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        rows.append(dict(zip(header, cells)))
    return header, rows


@dataclass
class ItemSchema:
    armor_codes: set
    weapon_codes: set
    stackable_codes: set


class StatTable:
    """Dict-like stat lookup that also carries the item schema, so the item
    codec can reach both the stat encoding and the category sets via one object."""

    def __init__(self, by_id: dict, schema: "ItemSchema"):
        self._by_id = by_id
        self.schema = schema

    def get(self, sid):
        return self._by_id.get(sid)


@dataclass
class StatEncoding:
    stat_id: int
    name: str
    save_bits: int       # v101: S12 override columns
    save_add: int
    save_param_bits: int
    encode: int          # ItemStatCost "Encode" — affects how value is packed
    saved: bool
    # v103 (PD2 S13) reverted to the VANILLA (non-S12) widths/add. Verified vs
    # corpus (workflow wf_12f20496-97e): uniques decode to exact UniqueItems values.
    save_bits_base: int = 0
    save_add_base: int = 0
    save_param_bits_base: int = 0


def _int(s: str, default: int = 0) -> int:
    s = (s or "").strip()
    try:
        return int(s)
    except ValueError:
        return default


class GameTables:
    """Holds parsed tables and derived schema for one install."""

    def __init__(self, mpq_path: str):
        self.mpq = MPQArchive(mpq_path)
        self._cache: dict[str, list[dict[str, str]]] = {}
        self._headers: dict[str, list[str]] = {}
        self.stat_by_id: dict[int, StatEncoding] = {}
        self.stat_by_name: dict[str, StatEncoding] = {}

    def load_table(self, name: str) -> list[dict[str, str]]:
        if name in self._cache:
            return self._cache[name]
        blob = self.mpq.read_file(f"{EXCEL}\\{name}.txt")
        if blob is None:
            raise FileNotFoundError(f"{name}.txt not found in MPQ")
        header, rows = parse_tsv(blob)
        self._headers[name] = header
        self._cache[name] = rows
        return rows

    def build_affix_max(self) -> dict[str, int]:
        """Map stat NAME -> highest legitimate affix value (max of modNmax across
        all MagicPrefix/MagicSuffix mods that grant that stat). Used to clamp
        max-roll to legal values instead of the raw bit ceiling."""
        if getattr(self, "affix_max", None) is not None:
            return self.affix_max
        # property code -> stat names (Properties.txt). Blank stat => code is the stat.
        code2stats: dict[str, list[str]] = {}
        for r in self.load_table("Properties"):
            code = r.get("code", "").strip()
            if not code:
                continue
            stats = [r.get(f"stat{i}", "").strip() for i in range(1, 8)]
            stats = [s for s in stats if s]
            code2stats[code] = stats or [code]  # fallback: code names the stat
        amax: dict[str, int] = {}
        for tbl in ("MagicPrefix", "MagicSuffix"):
            for r in self.load_table(tbl):
                for n in range(1, 4):
                    code = r.get(f"mod{n}code", "").strip()
                    if not code:
                        continue
                    mx = _int(r.get(f"mod{n}max", ""))
                    for stat in code2stats.get(code, [code]):
                        if mx > amax.get(stat, -10**9):
                            amax[stat] = mx
        self.affix_max = amax
        return amax

    def build_schema(self) -> "ItemSchema":
        """Build category code-sets (armor/weapon/stackable/book) from live tables."""
        armor, weapon, stackable = set(), set(), set()
        for r in self.load_table("Armor"):
            c = r.get("code", "").strip()
            if c:
                armor.add(c)
        for r in self.load_table("Weapons"):
            c = r.get("code", "").strip()
            if c:
                weapon.add(c)
        for t in ("Armor", "Weapons", "Misc"):
            for r in self.load_table(t):
                if (r.get("stackable", "").strip() == "1"):
                    c = r.get("code", "").strip()
                    if c:
                        stackable.add(c)
        self.schema = ItemSchema(armor, weapon, stackable)
        # make stat_by_id carry a back-ref so the item codec can reach schema
        return self.schema

    def build_stat_encoding(self) -> dict[int, StatEncoding]:
        rows = self.load_table("ItemStatCost")
        for r in rows:
            name = r.get("Stat", "").strip()
            if not name:
                continue
            sid = _int(r.get("ID", ""), -1)
            if sid < 0:
                continue
            # PD2 stores narrower save widths in S12 override columns; prefer
            # them when present, falling back to the vanilla columns. (94 stats
            # differ — e.g. mindamage 11->10/add 300->0.) Verified vs live MPQ.
            def pick(s12_col, base_col):
                v = (r.get(s12_col) or "").strip()
                return _int(v) if v != "" else _int(r.get(base_col, ""))

            enc = StatEncoding(
                stat_id=sid,
                name=name,
                save_bits=pick("Save Bits S12", "Save Bits"),
                save_add=pick("Save Add S12", "Save Add"),
                save_param_bits=pick("Save Param Bits S12", "Save Param Bits"),
                encode=_int(r.get("Encode", "")),  # no S12 override for Encode
                saved=bool(_int(r.get("Saved", ""))),
                save_bits_base=_int(r.get("Save Bits", "")),
                save_add_base=_int(r.get("Save Add", "")),
                save_param_bits_base=_int(r.get("Save Param Bits", "")),
            )
            self.stat_by_id[sid] = enc
            self.stat_by_name[name] = enc
        return self.stat_by_id

    def stat_table(self) -> "StatTable":
        """One object carrying stat encoding + category schema for the item codec."""
        if not self.stat_by_id:
            self.build_stat_encoding()
        schema = getattr(self, "schema", None) or self.build_schema()
        return StatTable(self.stat_by_id, schema)

    def available(self) -> dict[str, bool]:
        names = [f"{EXCEL}\\{t}.txt" for t in WANTED_TABLES]
        present = self.mpq.list_known(names)
        return {t: present[f"{EXCEL}\\{t}.txt"] for t in WANTED_TABLES}


if __name__ == "__main__":
    import sys
    gt = GameTables(sys.argv[1])
    print("=== table availability ===")
    for t, ok in gt.available().items():
        print(f"  {'OK ' if ok else 'MISS'} {t}")
    print("=== stat encoding ===")
    enc = gt.build_stat_encoding()
    print(f"loaded {len(enc)} stats")
    for sid in (0, 1, 2, 3, 7, 39, 31):  # str/ene/dex/vit/maxhp/fireresist/defense
        e = enc.get(sid)
        if e:
            print(f"  id={e.stat_id:>3} {e.name:<18} bits={e.save_bits:>2} "
                  f"add={e.save_add:>4} parambits={e.save_param_bits}")
