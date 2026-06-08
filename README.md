# Save Editor — a modern, multi-game Diablo II save editor

A cross-platform save editor for Diablo II / LoD and mods (built and verified
against **Project Diablo 2, Season 13**). Reads & writes characters (`.d2s`),
PlugY personal stashes (`.d2x`), the PD2 shared stash (`pd2_shared.stash`,
`55BB55BB`), and legacy shared stashes (`.sss`).

Everything game-specific (stats, item bases, bit-widths) is read **live from the
game's own data** (`pd2data.mpq`) at runtime — so it adapts to a season/mod
without code changes. Only the container formats are hardcoded.

## Requirements
- **Python 3.10+** — the core editor + legacy browser GUI need **no** third-party packages
- For the native **desktop app**: `pip install PySide6`
- For a packaged executable: `pip install PySide6 pyinstaller`
- The game's `pd2data.mpq` (from your PD2 install) reachable on disk

## Run it

### Native desktop app
```sh
pip install PySide6
python3 native/app.py
```
On first run, choose `pd2data.mpq` and a character/stash save. The app remembers
both paths in native settings and starts directly next time.

### Or as a single double-clickable executable
```sh
pip install PySide6 pyinstaller
python3 build.py             # -> dist/SaveEditor(.exe), one file
```
Run `build.py` on each OS you want a binary for (PyInstaller doesn't cross-compile:
Windows .exe, Linux ELF, macOS .app).

On Linux, install the built executable into your app launcher with the included
Diablo-like icon:
```sh
scripts/install-linux-desktop.sh
```

### Legacy browser prototype
```sh
python3 gui/app.py --browser            # auto-opens your browser, or:
python3 gui/server.py --port 8765 --mpq /path/to/ProjectD2/Live/pd2data.mpq
# then open http://localhost:8765
```

The MPQ is auto-detected from `$PD2_MPQ` or common install locations; `--mpq`
overrides. Example on Linux/Proton:
```sh
export PD2_MPQ="$HOME/.wine/drive_c/Program Files (x86)/Diablo II/ProjectD2/Live/pd2data.mpq"
python3 gui/app.py
```

### GUI features
- Open a character or stash; inventory shown as a real grid, equipped/stash as cards
- Hover an item for a full stat tooltip (quality-colored)
- Click a stat to edit it, or **Max Roll** an item
- **+ Add Item**: pick any base + quality + stat (browsed live from the game tables)
  and insert it — built from scratch, byte-identical to a real item
- **Validate**: predicts whether the game will load the save
- Every edit is checked by the validator and **rejected if it would not load**.
  Output is always written to a sibling `*.edited.d2s` — your source is never touched.

## CLI
```sh
python3 cli/d2edit.py --mpq <pd2data.mpq> info     <save.d2s>
python3 cli/d2edit.py --mpq <pd2data.mpq> items    <save.d2s>
python3 cli/d2edit.py --mpq <pd2data.mpq> validate <save.d2s>   # exit 0=loadable
python3 cli/d2edit.py --mpq <pd2data.mpq> verify-v2 <save.d2s>  # byte-exact round-trip
python3 cli/d2edit.py --mpq <pd2data.mpq> verify-stash <stash>
```

## Important gotcha
A `.d2s` file's **filename must match the character name stored inside it**, or
the game says "Unable to enter game / Bad character version". The `validate`
command warns when they differ.

## Layout
```
core/     codec + tables + validator (no UI)
  item_v2.py   item read/write (the cracked format)
  stash.py     stash containers (55BB / CSTM01 / SSS)
  tables.py    live schema from pd2data.mpq
  validate.py  the no-corruption gate
  mpq.py       pure-Python MPQ reader (PKWARE-DCL)
cli/      headless commands
gui/      stdlib web GUI (server.py)
testdata/ sample saves
```

## Safety
- Close the game before loading an edited save (PD2 rewrites saves on exit).
- Edits go to `*.edited.d2s`; rename to the character name to load in-game.
- The editor reads `pd2data.mpq` read-only; it never modifies game files.
