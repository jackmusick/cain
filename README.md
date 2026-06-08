# Cain — a modern, multi-game Diablo II save editor

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

## Install

### Linux (one-liner, latest main)
```sh
curl -fsSL https://raw.githubusercontent.com/jackmusick/cain/main/install.sh | sh
```
User-local, no sudo: a private venv under `~/.local/share/cain`, the `cain`
(desktop app) and `cain-cli` (read/verify CLI) commands in `~/.local/bin`, and a
**Cain** app-menu launcher with a Diablo-like icon. Re-run the same command to
update. On first run, choose `pd2data.mpq` and a character/stash save; the app
remembers both.

To uninstall: `rm -rf ~/.local/share/cain ~/.local/bin/cain ~/.local/bin/cain-cli ~/.local/share/applications/cain.desktop`.

### From source (developers)
```sh
git clone https://github.com/jackmusick/cain
cd cain
./install.sh                 # same venv install, from your checkout
# or, for an editable dev install:
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/cain               # desktop app   (.venv/bin/cain-cli for the CLI)
```

### Single double-clickable executable (PyInstaller, optional)
```sh
pip install PySide6 pyinstaller
python3 build.py             # -> dist/Cain(.exe), one file
```
Run `build.py` on each OS you want a binary for (PyInstaller doesn't cross-compile:
Windows .exe, Linux ELF, macOS .app). `scripts/install-linux-desktop.sh` installs
that built binary into the app menu instead of the venv launcher.

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
python3 cli/cain.py --mpq <pd2data.mpq> character <save.d2s>  # full character as JSON
python3 cli/cain.py --mpq <pd2data.mpq> info      <save.d2s>
python3 cli/cain.py --mpq <pd2data.mpq> items     <save.d2s>
python3 cli/cain.py --mpq <pd2data.mpq> validate  <save.d2s>  # exit 0=loadable
python3 cli/cain.py --mpq <pd2data.mpq> verify-v2 <save.d2s>  # byte-exact round-trip
python3 cli/cain.py --mpq <pd2data.mpq> verify-stash <stash>
```

`--mpq` can be omitted when `$PD2_MPQ` is set or the install is auto-detected.

### `character` — read-only JSON for LLM build review
`character` emits a flat, JSON view of a `.d2s`: `identity` (name/class/level/
hardcore/difficulty progress), `attributes`, allocated `skills`, `equipped` gear
(resolved names + human-readable stat lines), and `inventory` (charms, cube, belt
— charms count toward resists). It's read-only and reuses the same decode the
desktop app uses. Resist/breakpoint totals are left to the caller: the save
stores mods, not game-computed totals (see the `notes` field).

## Claude skill: PD2 build advisor
`skills/pd2-build-advisor/SKILL.md` is a bundled, read-only Claude skill. It runs
`cain character`, computes effective resists (gear + charms minus the difficulty
penalty: Normal 0 / Nightmare −40 / Hell −100), cross-references the Project
Diablo 2 wiki, and gives concrete gear/skill advice. Install it for Claude Code by
symlinking it into your skills directory, e.g.:
```sh
ln -s "$PWD/skills/pd2-build-advisor" ~/.claude/skills/pd2-build-advisor
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
cli/      headless commands (cain.py)
gui/      stdlib web GUI + headless backend (server.py)
native/   native Qt desktop app (the primary editor)
skills/   bundled Claude skill (pd2-build-advisor)
testdata/ sample saves
```

## Safety
- Close the game before loading an edited save (PD2 rewrites saves on exit).
- Edits go to `*.edited.d2s`; rename to the character name to load in-game.
- The editor reads `pd2data.mpq` read-only; it never modifies game files.
