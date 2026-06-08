# In-memory edit model, warm MPQ cache, async Save — design

**Date:** 2026-06-08
**Status:** Approved (pending spec review)

## Problem

The native Qt app (`native/app.py`) reuses the browser prototype's handler module
(`gui/server.py`) as its backend (`import gui.server as save_api`). Every edit
routes through a `do_*` function that:

1. re-reads the entire save from disk (`open(path, "rb").read()`),
2. re-parses it, mutates, rebuilds the byte stream,
3. runs the full validator,
4. copies a timestamped backup, then
5. writes the whole file back to disk.

So disk *is* the document — read/validate/backup/write happens on **every click**.
That is the lag felt on duplicate/move/edit. Separately, opening the Item Editor
cold-loads the MPQ: the editor's `__init__` calls `browse(...)` ten times, each
forcing a table out of the MPQ through a pure-Python PKWARE exploder, synchronously
on the UI thread — the "several seconds" on first open.

This is an architecture problem, not a language one. The fix is an in-memory
document with an explicit Save, plus warming the table cache off the UI thread.

## Goals

- Load a save file into memory once; apply edits in memory; write to disk only on
  an explicit, user-initiated **Save**.
- Save is asynchronous with a progress dialog, validates, backs up the original,
  then replaces the file from memory.
- Warm the MPQ/table cache at startup on a background thread; the main window is
  usable immediately.
- Real-time, debounced validation against the in-memory buffer, surfaced as a
  non-blocking banner.

## Non-goals

- No rewrite of the bit-level parsing/format logic (`core/`).
- No change to the on-disk formats, backup layout, or validator behavior.
- No per-edit disk persistence or autosave.

## Decisions (from brainstorming)

- **Unsaved changes:** dirty indicator + prompt **Save / Discard / Cancel** on close
  or when opening/switching files.
- **Save scope:** per-file dirty tracking (session keyed by path), but a **single
  Save** action that commits *all* dirty buffers. Usually one file; if a
  copy-to-stash dirtied the stash too, the one action flushes both. No "Save All"
  wording, no separate per-file buttons.
- **MPQ warm:** background thread, window usable immediately; table-dependent
  actions wait on a readiness event behind a small progress indicator.
- **Per-edit validation:** not a blocking gate. Edits apply instantly; a
  **debounced** background validate runs against the in-memory buffer and surfaces
  issues in a banner. Save performs the final authoritative validate before writing.

## Architecture

### Session buffer (`gui/server.py`)

Module-level working set, keyed by absolute path:

```python
_SESSION = {}  # path -> {"data": bytearray, "dirty": bool, "kind": "d2s" | "stash"}
```

- `_read_bytes(path) -> bytearray` — returns the session buffer if loaded, else
  reads disk once and registers it (`dirty=False`). All ~23 `open(path, "rb").read()`
  read sites become `_read_bytes(path)`.
- `_gate_and_write(data, path)` / `_gate_and_write_stash(data, path)` — stop
  touching disk. They store the rebuilt bytes into `_SESSION[path]["data"]` and set
  `dirty=True`. No backup, no disk write, no validation here. They keep their
  current return shape (`{"ok": True, ...}`) so the 20 `do_*` callers are untouched.
- `commit_save(path) -> dict` — the only function that writes the save file:
  validate the buffer → `_backup_original(path)` → write bytes to disk → clear
  `dirty`. Returns `{"ok": True, "backup": ...}` or `{"error", "details"}` on
  validation failure (buffer left dirty, nothing written).
- `commit_all() -> dict` — calls `commit_save` for every dirty path; aggregates
  results. Backs the single Save action.
- `validate_buffer(path) -> dict` — runs the appropriate validator
  (`validate_d2s` / `validate_stash`) against the in-memory buffer without writing.
  Used by the debounced realtime validator.
- `discard(path)` — drops the buffer so the next `_read_bytes` reloads from disk.
- `dirty_paths() -> list[str]` — for UI state.

This centralizes the entire behavioral change at the read helper and the two write
chokepoints; the `do_*` functions and `parse_save` are mechanically retargeted, not
rewritten.

### Warm MPQ cache (`native/app.py`)

After `save_api.set_mpq(path)`, the main window opens immediately and starts a
`QThread` that touches every table/derived structure the app uses — the ten
`browse(...)` sets, `build_schema`, `build_affix_max`, `build_stat_encoding`,
`stat_table` — populating `GameTables._cache` once, then sets a `tables_ready`
event.

- Only the warm thread builds the cache; the main thread waits on `tables_ready`
  before any `tables()`-dependent action.
- Opening a save file (`parse_save` → `tables().stat_table()`) and opening the Item
  Editor both wait on the event behind a small progress indicator if the user beats
  the warm. After the event fires, the cache is fully populated and read-only, so
  concurrent reads are safe. Pre-warming the exact set the app uses avoids later
  lazy `load_table` mutations from the main thread.

### Async Save (`native/app.py`)

Save runs on a worker (`QThread`/`QRunnable`) calling `commit_all()`:
validate → backup → write. A modal indeterminate `QProgressDialog` ("Saving…") is
shown and editing UI is disabled for the duration to prevent buffer mutation mid
save. On completion a signal marshals back to the main thread to clear dirty state,
update the title, and report success or a validation failure (failure keeps the
file dirty and shows the errors).

### Realtime validation banner (`native/app.py`)

A `QTimer`-based debounce (~300–500 ms after the last edit) calls
`validate_buffer(active_path)` on a worker thread. If invalid, a non-intrusive
banner appears above the editor summarizing the issue count; clicking it expands the
detailed validator errors. The banner clears when the buffer validates clean.

### UI / dirty-state affordances

- **Save** action: toolbar button + `Ctrl+S`, enabled only when any buffer is dirty.
- Dirty indicator: asterisk in the window title.
- On **close** or **open/switch file** with unsaved changes: prompt
  **Save / Discard / Cancel**. Discard calls `discard(path)` and reloads from disk.

## Data flow

```
edit click → do_*(body) → _read_bytes(path) [from buffer]
           → rebuild → _gate_and_write [buffer, dirty=True]   (no disk, no validate)
           → debounce timer → validate_buffer (worker) → banner

Save (Ctrl+S) → worker: commit_all() → per dirty path:
                validate → backup → write disk → dirty=False
              → signal → clear title asterisk / report result
```

## Error handling

- **Save validation failure:** file not written, buffer stays dirty, validator
  errors shown; Save remains available.
- **Disk write failure:** surface the OS error; buffer stays dirty.
- **Edit during save:** prevented by disabling the editing UI while the save worker
  runs.
- **Action before warm complete:** blocks on `tables_ready` behind a progress
  indicator rather than racing a half-built cache.

## Testing

- `_read_bytes`: returns buffer when loaded, reads disk and registers when not;
  buffer edits are visible to subsequent `do_*` without disk writes.
- `_gate_and_write[_stash]`: mutate buffer + set dirty; do **not** write disk or back
  up.
- `commit_save`: writes disk + backup on valid buffer; on invalid buffer writes
  nothing, returns error, leaves dirty.
- `commit_all`: flushes multiple dirty paths (char + stash); aggregates results.
- `validate_buffer`: matches `commit_save`'s validation verdict without writing.
- Round-trip: load → several edits in memory → Save → on-disk bytes equal the final
  buffer and pass the validator; exactly one backup created per Save.
- `discard`: next read reloads original disk bytes.

## Risks

- Hidden disk read/write paths outside the enumerated sites — mitigated by grepping
  `open(` in `gui/server.py` and routing every save-file read/write through the
  helpers.
- `GameTables._cache` thread-safety — mitigated by the single-builder + readiness
  event discipline above.
