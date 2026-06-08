---
name: pd2-build-advisor
description: Use when the user wants help analyzing or improving a Project Diablo 2 character — reviewing gear/skills/stats, diagnosing why a character is squishy or weak, planning upgrades, or answering "what should I change" build questions. Reads the character with the Cain CLI and cross-references the PD2 wiki. Read-only; never edits saves.
---

# PD2 Build Advisor

Analyze a Project Diablo 2 character and give concrete, correct build advice.
You read the character with the **Cain** CLI (in this repo) and ground your
reasoning in the **Project Diablo 2 wiki**. You never modify a save — editing is
done by the user in the Cain desktop app.

## Workflow

### 1. Locate the MPQ and the save
- `pd2data.mpq` is usually at one of:
  - `$PD2_MPQ` (if set)
  - `~/Games/ProjectDiablo2/drive_c/Program Files (x86)/Diablo II/ProjectD2/pd2data.mpq`
  - `C:\Program Files (x86)\Diablo II\ProjectD2\pd2data.mpq`
- Character saves (`.d2s`) live in the install's `Save/` directory. Ask the user
  which character if it's ambiguous.

### 2. Read the character (JSON)
Run from the repo root:
```sh
python3 cli/cain.py --mpq "<pd2data.mpq>" character "<Save/Name.d2s>"
```
(If `$PD2_MPQ` is set or the install is auto-detected, `--mpq` can be omitted.)

The command prints JSON with:
- `identity` — name, class, level, hardcore flag, which difficulties were entered
- `attributes` — str/dex/vit/energy, life/mana/stamina, unspent points, gold
- `skills` — every allocated skill `{id, name, level}`
- `equipped` — worn gear by slot, each with resolved name + human-readable stat lines
- `inventory` — carried items (charms, cube, belt). **Charms here matter** — they
  contribute to resists, life, damage, etc.
- `notes` — the resist-math reminder (below)

Exit code is non-zero and the JSON is `{"error": ...}` if the save can't be read.

### 3. Compute what the file doesn't store
The save stores **mods**, not game-computed totals. You do the math:

- **Effective resistance** for each element =
  `sum(resist mods on equipped + charms)`, capped at the character's max (75 base,
  raised by items like Ancient's Pledge or skills), **minus the difficulty
  penalty**: Normal `0`, Nightmare `-40`, **Hell `-100`**.
  - A character that looks "resist-capped" in Nightmare can be deep in the
    negatives in Hell. This is the #1 cause of "why am I so squishy in Hell."
  - Some classes raise resists with a skill (e.g. Paladin Resist auras, certain
    passives) — account for those from the `skills` list.
- **Breakpoints** (FCR/FHR/IAS): sum the relevant mods and compare to the
  class/skill breakpoint tables on the wiki.
- Don't forget Damage Reduction (`damageresist`/"Damage Reduced by"), max-resist
  mods, and block.

### 4. Cross-reference the PD2 wiki (curl, NOT WebFetch)
The wiki is a MediaWiki instance, but two gotchas (verified 2026-06):
- **`WebFetch` is blocked (HTTP 403)** by the wiki's bot protection. Use `curl`
  with a browser User-Agent via Bash instead.
- **The API is at `/w/api.php`**, not `/api.php` (`/api.php` returns "File not
  found"). Article pages are at `/wiki/<Page>` and render fine with a browser UA.

Set a UA once and use the API:
```sh
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
# Search:
curl -sS -A "$UA" "https://wiki.projectdiablo2.com/w/api.php?action=query&list=search&srsearch=<TERM>&format=json&srlimit=5"
# Page wikitext (best for stat/skill TABLES — parse the |a||b||c| rows):
curl -sS -A "$UA" "https://wiki.projectdiablo2.com/w/api.php?action=parse&page=<PAGE>&format=json&prop=wikitext"
```
Notes: skills often **redirect** to their tree page with a section anchor (e.g.
`Fade` → `Shadow_Disciplines#Fade`) — follow the redirect and grep the section.
Per-level skill values live in wide wikitable rows (`! All Resistances +%` then
`|15||17||19||...`).

Use it to confirm: skill mechanics and per-level numbers, synergies, runeword
recipes/stats, unique/set item stats, and breakpoint tables. **PD2 differs
substantially from vanilla D2 / D2R — always prefer the PD2 wiki over recalled
vanilla knowledge** (e.g. Fade requires character level 18 and is +15% all-res /
0% phys-DR at level 1, +2% res & +1% phys-DR per level — not the vanilla values).

### 5. Give the advice
Be concrete and prioritized:
1. **Survivability first** if relevant — name the resist gaps with numbers
   ("Lightning res is −35 in Hell; you need +110 from gear/charms to cap"),
   then name specific PD2 items/runewords that fix them.
2. Then damage/breakpoints/QoL.
3. Tie each suggestion to a slot or charm the character actually has, and cite
   the wiki page for any item/skill you recommend.

Tell the user to make the changes in the **Cain desktop app** (`python3
native/app.py`) — this skill is read-only.

## Guardrails
- Never run edit/write commands. `cain` only has read/verify subcommands; keep it
  that way for build review.
- If a stat line shows a raw name (e.g. `damageresist 10`, `tohit 31`) the game's
  template was missing — interpret it from the stat name; don't treat it as an error.
- State your difficulty assumption explicitly (penalty applied) so the user can
  correct you if they're not actually in Hell yet.
