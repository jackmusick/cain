# Cain — read-only character CLI + bundled build-advisor skill

**Date:** 2026-06-07
**Status:** Approved (design), pending implementation

## Summary

Rename the "Save Editor" project to **Cain** and relocate it to `~/GitHub/Cain`
as a fresh git repository. Add a read-only `cain character <save.d2s>` command
that emits a structured JSON view of a character (identity, attributes, skills,
equipped gear with resolved names and human-readable stats), suitable for
consumption by an LLM. Bundle a Claude skill in the repo that drives this command
and the Project Diablo 2 wiki to give build/gear advice.

This is a **read-only** feature set. No new editing capability is introduced.
Existing edit paths (GUI "Max Roll", "Add Item") are untouched.

## Motivation

The editor already reads `.d2s` characters live from `pd2data.mpq`. All the rich
presentation logic — item display names, human-readable stat formatting, the
character stat/skill blocks, equip-slot mapping — currently lives only in
`gui/server.py` (a 2000+ line web-server module). The headless CLI
(`cli/d2edit.py`) talks to `core/` directly and therefore cannot produce a
human/LLM-readable character summary.

Goal: a clean CLI surface an LLM can call to read a character, plus a bundled
skill that pairs that data with the PD2 wiki for build advice (e.g. "why am I so
squishy in Hell Act 2" → sum gear resists, apply the −100 Hell penalty, suggest
fixes).

## Scope

### In scope
1. **Relocation + rename** to `~/GitHub/Cain`, fresh `git init`.
2. **`cain character <save.d2s>`** command → JSON.
3. **`core/summary.py`** — extract the read-only presentation helpers out of
   `gui/server.py` so both the GUI and CLI share them.
4. **`skills/pd2-build-advisor/SKILL.md`** bundled in the repo + install docs.

### Out of scope (YAGNI)
- Any new edit/write command in the CLI.
- Computing *totalled* resists/breakpoints inside the codec. The save stores base
  attributes + item mods, not game-computed totals; the skill does the summing.
- Stash/shared-stash JSON dumps (the `character` command targets `.d2s` only for
  v1; stash can follow the same pattern later if wanted).
- Renaming the old `~/Sync/save-editor` directory — it stays as a backup.

## Relocation details

- Copy tree `~/Sync/save-editor` → `~/GitHub/Cain`, excluding `.git/`, `build/`,
  `dist/`, `__pycache__/`, `.superpowers/`. **(done during this session)**
- `git init` fresh; add `.gitignore` covering Python artifacts, `build/`, `dist/`,
  `*.edited.d2s`/`*.edited.d2x`, `.superpowers/`. **(done)**
- The old `.git` at the source was empty (no history/remote), so nothing is lost.
- After implementation, update the user's memory file paths to `~/GitHub/Cain`.

## Rename surface

New product name **Cain**; CLI command **`cain`**.

| Old | New |
|-----|-----|
| `cli/d2edit.py` | `cli/cain.py` (argparse `prog="cain"`) |
| `SaveEditor.spec` | `Cain.spec` (PyInstaller `name="Cain"`) |
| "Save Editor" / `SaveEditor` strings (~19) in `build.py`, `README.md`, `core/tables.py`, `core/mpq.py`, `gui/app.py`, `gui/server.py`, `native/app.py`, `native/__init__.py`, `scripts/install-linux-desktop.sh` | "Cain" |
| Window titles / desktop launcher name | "Cain" |

Behavior of every renamed entry point is unchanged; only names/strings move.

## `cain character` — output contract

`cain --mpq <pd2data.mpq> character <save.d2s>` prints a single JSON object to
stdout. Exit 0 on success; non-zero with a JSON `{"error": "..."}` on failure
(unreadable file, missing marker, etc.).

```jsonc
{
  "identity": {
    "name": "Jeclipse",
    "class": "Paladin",
    "level": 44,
    "version": "0x60",
    "hardcore": false,
    "difficulty_progress": { "normal": true, "nightmare": false, "hell": false }
  },
  "attributes": {
    "strength": 90, "dexterity": 60, "vitality": 200, "energy": 35,
    "life": 850, "max_life": 850, "mana": 210, "max_mana": 210,
    "stamina": 320, "max_stamina": 320,
    "unspent_stat_points": 0, "unspent_skill_points": 2,
    "gold": 12345, "gold_stash": 678
  },
  "skills": [
    { "id": 106, "name": "Holy Shock", "level": 20 },
    { "id": 112, "name": "Resist Lightning", "level": 1 }
  ],
  "equipped": [
    {
      "slot": "Armor",
      "name": "Smoke",                 // runeword/unique/rare/magic resolved name
      "base": "Mage Plate",
      "quality": "runeword",
      "ethereal": false,
      "sockets": 2,
      "stats": [
        "+50% Enhanced Defense",
        "+50 to All Resistances",
        "+10 to Vitality"
      ]
    }
    // ... one entry per occupied EQUIP_SLOTS slot
  ],
  "notes": [
    "Resist values above are per-item gear mods only.",
    "Effective resists = sum of gear/charm mods, capped at 75 (base), minus the",
    "difficulty penalty: Normal 0, Nightmare -40, Hell -100.",
    "Charms in inventory also contribute and are NOT in 'equipped'."
  ]
}
```

Field sourcing:
- **identity** — from `core/d2s.py` header + difficulty/quest progress bytes.
- **attributes** — from the `gf` character-stat block (existing
  `_char_stat_block` logic).
- **skills** — from the `if` skill block, filtered to the class tree (existing
  `_skill_block` logic); only non-zero levels emitted.
- **equipped** — items with `location_id == 1` (equipped), mapped through
  `EQUIP_SLOTS`; name via existing `_display_name`, stats via existing
  `_format_stat`. Alt-weapon-swap slots (11/12) included, labelled "Alt".

> v1 reports per-item resist mods and documents the totalling rule in `notes`
> rather than computing effective resists in code. The skill (or the LLM) sums
> gear + charms and applies the difficulty penalty. This keeps the codec free of
> game-balance assumptions.

## `core/summary.py` — extraction

Move these read-only helpers from `gui/server.py` into a new, UI-free
`core/summary.py`; `gui/server.py` re-imports them so its behavior is unchanged:

- `EQUIP_SLOTS`, `CLASS_CODES`, difficulty constants
- `_display_name`, `_display_invfile`, `_clean_base_name`, affix/quality name
  helpers, `_skill_name`, `_class_name`
- `_format_stat` and its per-stat formatting helpers (`_format_per_level`, etc.)
- `_char_stat_block` / `character_stats`, `_skill_block` / `character_skills`
- A new top-level `character_summary(data, tables) -> dict` that assembles the
  output contract above.

The CLI's `character` command and the GUI both call `character_summary`.
`gui/server.py` keeps all its write/edit functions; only the read-only
presentation layer moves. This shrinks the oversized server module and gives the
summary a single, testable home.

### Module boundaries
- `core/summary.py` depends on `core/tables.py` (live schema), `core/d2s.py`,
  `core/item_v2.py`. It does **not** import any GUI/Qt/server code.
- Input: raw save `bytes` + a `Tables` instance. Output: plain dict / JSON-safe.
- Pure and side-effect-free → unit-testable against `testdata/` saves.

## Bundled skill: `skills/pd2-build-advisor/SKILL.md`

A repo-committed Claude skill (read-only). It instructs Claude to:
1. Locate `pd2data.mpq` (env `PD2_MPQ` or documented install paths) and the save.
2. Run `python3 cli/cain.py --mpq <mpq> character <save>` and parse the JSON.
3. Consult the **Project Diablo 2 wiki** via WebFetch — MediaWiki API at
   `https://wiki.projectdiablo2.com/api.php` (e.g.
   `?action=query&list=search&srsearch=<term>&format=json`) and article URLs
   (`https://wiki.projectdiablo2.com/<Page>`) for skills, runewords, uniques,
   mechanics.
4. Reason about the build: sum gear/charm resists, apply the difficulty penalty,
   identify gaps (resists, breakpoints, missing damage/defense), and suggest
   concrete gear/skill changes.

The skill is explicitly **read-only**: it never invokes edit commands and never
writes saves. README documents installing it (symlink/copy into the user's Claude
skills directory).

## Testing

- **Unit:** `core/summary.py` against `testdata/` saves — assert identity,
  attribute keys, skill list non-empty for a leveled char, equipped slots map
  correctly, stats render as strings. Round-trip parity check: the summary's
  attribute/skill values match the existing GUI `character_stats`/
  `character_skills` output (guards the extraction).
- **CLI smoke:** `cain character` on a `testdata` save emits valid JSON (parse +
  schema-key check), exit 0; bad path → JSON error + non-zero exit.
- **Regression:** existing `verify-v2` / `verify-stash` byte-exact round-trips
  and the GUI still pass after the extraction (no behavior change).
- **Manual:** run `cain character` on the live `Jeclipse.d2s` and eyeball the
  Paladin's resists/skills.

## Error handling

- Missing/unreadable MPQ or save → JSON `{"error": ...}`, non-zero exit.
- Missing `gf`/`if` markers → surfaced as a clear error (reuse existing
  `ValueError` messages).
- Unknown stat/skill ids → degrade gracefully to `"Skill <id>"` / raw stat, never
  crash (existing behavior in the helpers).

## Risks

- **Extraction regressions** — moving code out of `gui/server.py` could change
  GUI behavior. Mitigation: `server.py` re-imports the moved names; the
  round-trip parity test compares old vs new output on `testdata`.
- **Wiki API drift** — endpoints documented in the skill, not hardcoded in
  shipped code, so drift is a doc fix, not a code change.
