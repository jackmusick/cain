# In-Memory Edit Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Edits live in an in-memory buffer; disk is touched only on an explicit, async, validated Save — and the MPQ table cache warms off the UI thread so the Item Editor opens instantly.

**Architecture:** Centralize all save-file reads behind `_read_bytes(path)` and all writes behind the two existing chokepoints (`_gate_and_write`, `_gate_and_write_stash`) in `gui/server.py`, backed by a module-level `_SESSION` buffer keyed by path. Edits mutate the buffer (no disk, no validate, no backup); a new `commit_all()` validates + backs up + writes on Save. In `native/app.py`, warm tables on a `QThread`, run Save on a worker with a progress dialog, and a single 250 ms poll timer drives the dirty-title indicator and a debounced background validate that feeds a banner.

**Tech Stack:** Python 3.10+ stdlib (core/server), PySide6 (Qt) for the desktop app. Tests are stdlib scripts under `scripts/` run with `.venv/bin/python` (no pytest), matching `scripts/test_d2r.py`.

---

## File structure

- `gui/server.py` — **modify.** Add `_SESSION` store and the session API (`_read_bytes`, `discard`, `dirty_paths`, `revision`, `commit_save`, `commit_all`, `validate_buffer`, `warm`, `is_warm`). Rewrite `_gate_and_write` / `_gate_and_write_stash` to target the buffer. Retarget all save-file `open(path,"rb").read()` sites to `_read_bytes(path)`.
- `native/app.py` — **modify.** Background table warm + readiness gating; async Save action (toolbar + `Ctrl+S`) with `QProgressDialog`; dirty-aware `closeEvent` and file-switch prompts; a poll `QTimer` driving the title asterisk and the debounced validation banner.
- `scripts/test_session.py` — **create.** Stdlib regression script for the session buffer + commit semantics.

Conventions to match: test scripts use `sys.path.insert(0, ".")`, `def test_*()` with `assert`, `print("PASS …")`, and a `main()` that runs them; run via `.venv/bin/python scripts/test_session.py`. Real save fixtures live in `testdata/` (`berserk.d2s`, `blessed-hammer.d2s`, `Ancksunamum.d2s`).

---

## Task 1: Session buffer + read helper in `gui/server.py`

**Files:**
- Modify: `gui/server.py` (add near the other module globals / before `parse_save`)
- Test: `scripts/test_session.py` (create)

- [ ] **Step 1: Write the failing test**

Create `scripts/test_session.py`:

```python
#!/usr/bin/env python3
"""Session-buffer + commit regression checks. Run:
    .venv/bin/python scripts/test_session.py

Buffer-mechanics tests need no MPQ. Commit/validate tests need a real
pd2data.mpq; set PD2_MPQ to its path or they SKIP.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from gui import server as save_api

FIXTURE = "testdata/berserk.d2s"


def _tmp_copy():
    fd, path = tempfile.mkstemp(suffix=".d2s")
    os.close(fd)
    shutil.copy2(FIXTURE, path)
    return path


def test_read_bytes_loads_disk_then_buffer():
    save_api.reset_session()
    path = _tmp_copy()
    try:
        disk = open(path, "rb").read()
        first = save_api._read_bytes(path)
        assert bytes(first) == disk, "first read must equal disk bytes"
        assert path in save_api.dirty_paths() is False or path not in save_api.dirty_paths()
        assert not save_api.dirty_paths(), "fresh load is not dirty"
        # mutate the buffer in place; a second read returns the SAME mutated buffer
        first[0] ^= 0xFF
        second = save_api._read_bytes(path)
        assert second is first, "second read must return the live buffer, not re-read disk"
        assert bytes(second)[:1] != disk[:1], "buffer edit must persist across reads"
        # disk is untouched
        assert open(path, "rb").read() == disk, "disk must not change on read/edit"
        print("PASS read_bytes loads disk once then serves the live buffer")
    finally:
        os.remove(path)
        save_api.reset_session()


def test_discard_reloads_disk():
    save_api.reset_session()
    path = _tmp_copy()
    try:
        buf = save_api._read_bytes(path)
        buf[0] ^= 0xFF
        save_api.discard(path)
        assert path not in save_api.dirty_paths()
        fresh = save_api._read_bytes(path)
        assert bytes(fresh) == open(path, "rb").read(), "discard must drop the buffer"
        print("PASS discard reloads from disk")
    finally:
        os.remove(path)
        save_api.reset_session()


def main():
    test_read_bytes_loads_disk_then_buffer()
    test_discard_reloads_disk()
    print("ALL SESSION TESTS PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python scripts/test_session.py`
Expected: FAIL — `AttributeError: module 'gui.server' has no attribute 'reset_session'` (or `_read_bytes`).

- [ ] **Step 3: Add the session store and helpers**

In `gui/server.py`, add near the top-level module globals (after the existing `_gt`/`_mpq` globals):

```python
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
        data = bytearray(open(path, "rb").read())
        entry = {"data": data, "dirty": False,
                 "kind": "stash" if _is_stash(path, bytes(data)) else "d2s",
                 "rev": 0}
        _SESSION[key] = entry
    return entry["data"]


def _store_bytes(path: str, data) -> None:
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
    return [k for k, e in _SESSION.items() if e["dirty"]]


def revision(path: str) -> int:
    entry = _SESSION.get(_session_key(path))
    return entry["rev"] if entry else 0


def reset_session() -> None:
    """Test/teardown hook: forget all buffers."""
    _SESSION.clear()
```

(`_is_stash` already exists in this module and is used by `parse_save`.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python scripts/test_session.py`
Expected: PASS for both tests, ending `ALL SESSION TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add gui/server.py scripts/test_session.py
git commit -m "Add in-memory session buffer + read helper"
```

---

## Task 2: Defer disk I/O — buffer-writing chokepoints + commit API

**Files:**
- Modify: `gui/server.py:1754-1776` (`_gate_and_write`, `_gate_and_write_stash`)
- Modify: `gui/server.py` (add `commit_save`, `commit_all`, `validate_buffer` after the two functions)
- Test: `scripts/test_session.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/test_session.py` (and add the two calls to `main()` before the final print):

```python
def test_gate_and_write_buffers_no_disk():
    save_api.reset_session()
    path = _tmp_copy()
    try:
        disk = open(path, "rb").read()
        save_api._read_bytes(path)                 # register buffer
        edited = bytearray(disk)
        edited[16] ^= 0xFF                          # arbitrary byte change
        res = save_api._gate_and_write(edited, path)
        assert res.get("ok"), res
        assert path_in_dirty(path), "edit must mark the path dirty"
        assert open(path, "rb").read() == disk, "edit must NOT write disk"
        assert bytes(save_api._read_bytes(path)) == bytes(edited), "buffer must hold the edit"
        # no backup folder created by an edit
        backups = os.path.join(os.path.dirname(path), "backups")
        assert not os.path.isdir(backups), "edits must not create backups"
        print("PASS _gate_and_write buffers the edit without disk/backup")
    finally:
        os.remove(path)
        save_api.reset_session()


def path_in_dirty(path):
    return os.path.abspath(os.path.expanduser(path)) in save_api.dirty_paths()


def test_commit_writes_disk_and_backup():
    if not _have_mpq():
        print("SKIP commit test (no PD2_MPQ)")
        return
    save_api.reset_session()
    path = _tmp_copy()
    try:
        save_api._read_bytes(path)
        buf = save_api._read_bytes(path)
        # a no-op edit: store identical bytes so the buffer is 'dirty' but valid
        save_api._gate_and_write(bytearray(buf), path)
        res = save_api.commit_all()
        assert res.get("ok"), res
        assert not save_api.dirty_paths(), "commit must clear dirty"
        backups = os.path.join(os.path.dirname(path), "backups")
        baks = os.listdir(backups) if os.path.isdir(backups) else []
        assert len(baks) == 1, f"commit must make exactly one backup, got {baks}"
        assert bytes(save_api._read_bytes(path)) == open(path, "rb").read(), \
            "disk must equal the committed buffer"
        print("PASS commit_all writes disk + one backup, clears dirty")
    finally:
        shutil.rmtree(os.path.join(os.path.dirname(path), "backups"), ignore_errors=True)
        os.remove(path)
        save_api.reset_session()


def _have_mpq():
    mpq = os.environ.get("PD2_MPQ", "")
    if mpq and os.path.exists(mpq):
        save_api.set_mpq(mpq)
        return True
    return False
```

- [ ] **Step 2: Run to verify the buffer test fails**

Run: `.venv/bin/python scripts/test_session.py`
Expected: FAIL — `test_gate_and_write_buffers_no_disk` fails because the current `_gate_and_write` validates and writes to disk (the `backups/` assertion or the "must NOT write disk" assertion trips).

- [ ] **Step 3: Rewrite the chokepoints + add the commit API**

Replace `_gate_and_write` and `_gate_and_write_stash` (currently at `gui/server.py:1754-1776`) with buffer-only versions, and add the commit/validate functions:

```python
def _gate_and_write(data, path: str):
    """Apply an edit to the in-memory buffer. No disk write, no backup, no
    validation here — validation is debounced in the UI and run authoritatively
    by commit_save()."""
    _store_bytes(path, data)
    return {"ok": True, "out": path, "pending": True}


def _gate_and_write_stash(data, path: str):
    _store_bytes(path, data)
    return {"ok": True, "out": path, "pending": True}


def validate_buffer(path: str):
    """Validate the in-memory buffer for `path` without writing. Returns
    {"ok": True} or {"ok": False, "errors": [...]}. Requires tables (MPQ)."""
    st = tables().stat_table()
    data = bytes(_read_bytes(path))
    entry = _SESSION.get(_session_key(path))
    kind = entry["kind"] if entry else ("stash" if _is_stash(path, data) else "d2s")
    res = (validate_mod.validate_stash(data, st) if kind == "stash"
           else validate_mod.validate_d2s(data, st))
    return {"ok": res.ok, "errors": list(res.errors)}


def commit_save(path: str):
    """Validate the buffer, back up the original, write to disk, clear dirty."""
    key = _session_key(path)
    entry = _SESSION.get(key)
    if entry is None or not entry["dirty"]:
        return {"ok": True, "out": path, "nothing_to_do": True}
    v = validate_buffer(path)
    if not v["ok"]:
        return {"error": "edit rejected by validator (would not load)",
                "details": v["errors"], "path": path}
    backup = _backup_original(path)
    with open(path, "wb") as f:
        f.write(bytes(entry["data"]))
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
```

Note: `validate_mod` is already imported in `gui/server.py` (used by the old chokepoints); `_backup_original` and `_finalize` are unchanged and still called by the `do_*` functions before they hand bytes to `_gate_and_write`.

- [ ] **Step 4: Run to verify tests pass**

Run: `.venv/bin/python scripts/test_session.py`
Expected: PASS for `test_gate_and_write_buffers_no_disk`; `test_commit_writes_disk_and_backup` PASS if `PD2_MPQ` is set, else prints `SKIP commit test (no PD2_MPQ)`. Ends `ALL SESSION TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add gui/server.py scripts/test_session.py
git commit -m "Defer save-file disk I/O to explicit commit_all()"
```

---

## Task 3: Retarget all save-file reads to the buffer

**Files:**
- Modify: `gui/server.py` — the ~23 `open(path,"rb").read()` save-read sites (lines listed below)
- Test: `scripts/test_session.py`

The read sites (each reads a save/stash file that an edit may have mutated in memory): `parse_save` (1356), the resists reader (1441), and inside the `do_*` functions at 2131, 2152, 2280, 2303, 2335, 2421, 2441, 2470, 2515, 2544, 2578, 2680, 2744, 2828 (`raw`), 2861 (`char_raw`), 2866 (`stash_raw`), 2918 (`stash_raw`), 2927 (`char_path`), 2960 (`char_path`), 3002, 3044. Line numbers are pre-edit; match on the code, not the number.

- [ ] **Step 1: Write the failing test**

Append to `scripts/test_session.py` (add the call in `main()`):

```python
def test_edit_then_commit_roundtrip():
    if not _have_mpq():
        print("SKIP roundtrip test (no PD2_MPQ)")
        return
    save_api.reset_session()
    path = _tmp_copy()
    try:
        save = save_api.parse_save(path)
        assert save.get("kind") == "character", save.get("kind")
        items = save.get("items", [])
        dup_idx = next(i for i, it in enumerate(items) if it.get("clean"))
        before = len(items)
        res = save_api.do_duplicateitem({"path": path, "item": dup_idx})
        assert res.get("ok"), res
        # the duplicate is visible WITHOUT any disk write
        assert path_in_dirty(path), "duplicate must dirty the buffer"
        reparsed = save_api.parse_save(path)              # reads the BUFFER
        assert len(reparsed["items"]) == before + 1, "duplicate must be visible in-memory"
        assert open(path, "rb").read() == open(FIXTURE, "rb").read(), "disk untouched pre-commit"
        # commit, then the new on-disk file re-parses with the extra item and validates
        assert save_api.commit_all().get("ok")
        save_api.reset_session()
        final = save_api.parse_save(path)
        assert len(final["items"]) == before + 1, "committed file must hold the duplicate"
        assert save_api.validate_buffer(path)["ok"], "committed file must validate"
        print("PASS edit reads/writes the buffer; commit persists a valid file")
    finally:
        shutil.rmtree(os.path.join(os.path.dirname(path), "backups"), ignore_errors=True)
        os.remove(path)
        save_api.reset_session()
```

- [ ] **Step 2: Run to verify it fails**

Run: `PD2_MPQ=/path/to/pd2data.mpq .venv/bin/python scripts/test_session.py`
Expected: FAIL — `do_duplicateitem` still does `open(path,"rb").read()`, so the in-memory duplicate is invisible to the next `parse_save` (or the "disk untouched pre-commit" assert trips because a stale path still validates against disk). (If `PD2_MPQ` is unset this SKIPs; you must set it to drive this task.)

- [ ] **Step 3: Replace every save-file read with `_read_bytes`**

For each site listed above, change the read to use the buffer. Patterns:

- `data = bytearray(open(path, "rb").read())` → `data = bytearray(_read_bytes(path))`
- `data = open(path, "rb").read()` → `data = bytes(_read_bytes(path))`
- `raw = open(path, "rb").read()` → `raw = bytes(_read_bytes(path))`
- `char_raw = open(char_path, "rb").read()` → `char_raw = bytes(_read_bytes(char_path))`
- `stash_raw = open(stash_path, "rb").read()` → `stash_raw = bytes(_read_bytes(stash_path))`
- `data = bytearray(open(char_path, "rb").read())` → `data = bytearray(_read_bytes(char_path))`

Use `bytearray(_read_bytes(path))` wherever the original wrapped the read in `bytearray(...)` (the code mutates a copy and rebuilds), and `bytes(_read_bytes(path))` where it took the bytes read-only. Do **not** change `_read_bytes`'s own `open(...)`, `_backup_original`'s `shutil.copy2`, or `commit_save`'s `open(path,"wb")`.

Verify none are missed:

Run: `grep -n 'open(.*rb)' gui/server.py`
Expected: the only remaining `rb` open is inside `_read_bytes`.

- [ ] **Step 4: Run to verify it passes**

Run: `PD2_MPQ=/path/to/pd2data.mpq .venv/bin/python scripts/test_session.py`
Expected: PASS for all tests including `test_edit_then_commit_roundtrip`, ending `ALL SESSION TESTS PASSED`.

- [ ] **Step 5: Commit**

```bash
git add gui/server.py scripts/test_session.py
git commit -m "Route all save-file reads through the in-memory buffer"
```

---

## Task 4: Warm the MPQ/table cache on a background thread

**Files:**
- Modify: `gui/server.py` (add `warm()` + `is_warm()`)
- Modify: `native/app.py` — `MainWindow.__init__` / `load_current` (around 2166-2377)

- [ ] **Step 1: Add `warm()`/`is_warm()` to `gui/server.py`**

Add after `set_mpq`:

```python
_warm_done = False

# the table-derived sets the desktop app reads when opening the Item Editor
_WARM_BROWSE = ("bases", "stats", "uniques", "sets", "magic_prefixes",
                "magic_suffixes", "rare_prefixes", "rare_suffixes",
                "runewords", "socket_fillers")


def warm():
    """Build every table/derived structure the app uses, so later table access
    is a cache hit. Safe to call on a worker thread; finishes before the UI
    touches tables()."""
    global _warm_done
    gt = tables()
    gt.build_schema()
    gt.build_affix_max()
    gt.build_stat_encoding()
    gt.stat_table()
    for kind in _WARM_BROWSE:
        browse(kind)
    _warm_done = True
    return True


def is_warm() -> bool:
    return _warm_done
```

(If `set_mpq` is later called with a new path, reset warmth: in `set_mpq`, where it sets `_gt = None`, also add `global _warm_done` and `_warm_done = False`.)

- [ ] **Step 2: Add `set_mpq` warmth reset**

In `gui/server.py` `set_mpq`, change the globals line and add the reset:

```python
def set_mpq(path: str):
    global _gt, _item_meta, _mpq, _warm_done
    ...
    _gt = None
    _item_meta = None
    _warm_done = False
    return _mpq_status()
```

- [ ] **Step 3: Warm on a `QThread` at startup; defer the first render until ready**

In `native/app.py`, add a small worker class near the other top-level widget classes:

```python
class _WarmWorker(QThread):
    done = Signal(bool)

    def run(self):
        try:
            save_api.warm()
            self.done.emit(True)
        except Exception:  # noqa: BLE001
            self.done.emit(False)
```

Ensure `QThread` and `Signal` are imported (PySide6: `from PySide6.QtCore import QThread, Signal` — add to the existing QtCore import group).

Rework `MainWindow.load_current` (currently 2364-2377) so set_mpq happens, then warm runs in the background and the actual parse/render waits for warmth:

```python
def load_current(self):
    global ASSETS
    mpq = self.settings.value("paths/mpq", "")
    try:
        save_api.set_mpq(mpq)
        ASSETS = DiabloAssetLoader(mpq)
    except Exception as e:  # noqa: BLE001
        QMessageBox.critical(self, "Could not load MPQ", str(e))
        return
    if save_api.is_warm():
        self._load_save_now()
        return
    self.statusBar().showMessage("Loading game data…")
    self._warm = _WarmWorker(self)
    self._warm.done.connect(self._on_warm_done)
    self._warm.start()

def _on_warm_done(self, ok: bool):
    if not ok:
        QMessageBox.critical(self, "Could not load game data",
                             "Failed to read tables from the MPQ.")
        return
    self.statusBar().clearMessage()
    self._load_save_now()

def _load_save_now(self):
    path = self.settings.value("paths/save", "")
    try:
        data = save_api.parse_save(path)
    except Exception as e:  # noqa: BLE001
        QMessageBox.critical(self, "Could not load save", str(e))
        return
    self.loaded = LoadedSave(path=path, data=data)
    self.render_save()
```

- [ ] **Step 4: Verify the window opens immediately and the editor is warm**

Run: `.venv/bin/cain`
Expected: the main window appears immediately with "Loading game data…" in the status bar; within a couple of seconds the save renders. Open **Item Builder / Item Editor** — it opens without a multi-second stall (tables already cached). Re-opening it is instant.

- [ ] **Step 5: Commit**

```bash
git add gui/server.py native/app.py
git commit -m "Warm MPQ table cache on a background thread"
```

---

## Task 5: Single async Save action with a progress dialog

**Files:**
- Modify: `native/app.py` — `_build_menu` (2186-2201), add `save_now` + a save worker, add `Ctrl+S`, add title-asterisk helper
- Modify: `native/app.py` — replace misleading "and wrote {res['out']}" status messages

- [ ] **Step 1: Add a Save worker and the Save action**

In `native/app.py`, add a worker near `_WarmWorker`:

```python
class _SaveWorker(QThread):
    done = Signal(dict)

    def run(self):
        try:
            self.done.emit(save_api.commit_all())
        except Exception as e:  # noqa: BLE001
            self.done.emit({"ok": False, "results": [{"error": str(e)}]})
```

In `_build_menu`, add Save as the first toolbar action and keep a handle so it can be enabled/disabled:

```python
def _build_menu(self):
    toolbar = QToolBar("Main")
    toolbar.setMovable(False)
    toolbar.setIconSize(QSize(20, 20))
    self.addToolBar(toolbar)
    self.save_action = QAction("Save", self)
    self.save_action.setShortcut(QKeySequence.StandardKey.Save)  # Ctrl+S
    self.save_action.triggered.connect(self.save_now)
    self.save_action.setEnabled(False)
    toolbar.addAction(self.save_action)
    for text, fn in [
        ("Open Save", self.pick_save),
        ("Open MPQ", self.pick_mpq),
        ("Settings", self.open_settings),
        ("Validate", self.validate_save),
        ("Item Builder", self.open_builder),
    ]:
        act = QAction(text, self)
        act.triggered.connect(fn)
        toolbar.addAction(act)
```

Ensure `QKeySequence` is imported (`from PySide6.QtGui import QKeySequence` — add to the existing QtGui import group).

- [ ] **Step 2: Implement `save_now` + completion handler + title helper**

Add to `MainWindow`:

```python
def _update_title(self):
    dirty = bool(save_api.dirty_paths())
    name = os.path.basename(self.loaded.path) if self.loaded else "Cain"
    self.setWindowTitle(f"{'*' if dirty else ''}{name} — Cain" if self.loaded else "Cain")
    self.save_action.setEnabled(dirty)

def save_now(self):
    if not save_api.dirty_paths():
        return
    self._save_progress = QProgressDialog("Saving…", "", 0, 0, self)
    self._save_progress.setWindowTitle("Cain")
    self._save_progress.setCancelButton(None)
    self._save_progress.setWindowModality(Qt.WindowModal)
    self._save_progress.setMinimumDuration(0)
    self.setEnabled(False)            # block edits during the write
    self._save_progress.show()
    self._saver = _SaveWorker(self)
    self._saver.done.connect(self._on_save_done)
    self._saver.start()

def _on_save_done(self, res: dict):
    self.setEnabled(True)
    self._save_progress.close()
    if not res.get("ok"):
        errs = []
        for r in res.get("results", []):
            if r.get("error"):
                errs.append(r["error"])
                errs.extend(r.get("details", []) or [])
        QMessageBox.warning(self, "Save failed",
                            "The edit did not validate and was not written:\n\n"
                            + "\n".join(errs[:20]))
    else:
        self.statusBar().showMessage("Saved", 5000)
    self._update_title()
```

Ensure `QProgressDialog` is imported (`from PySide6.QtWidgets import QProgressDialog` — add to the existing QtWidgets import group). `Qt` is already imported.

- [ ] **Step 3: Replace the misleading per-edit status messages**

The edit handlers currently end their status message with `and wrote {res['out']}` (e.g. lines 2602, 2645, 2667, 2731, 2773, 2803, 2911, 2931, 3073, 3089, and similar). The file is no longer written per edit. For each, replace the trailing `and wrote {res['out']}` with `— unsaved (Ctrl+S to save)`. Example:

```python
# before
self.statusBar().showMessage(f"Duplicated {item.get('name', 'item')} and wrote {res['out']}", 7000)
# after
self.statusBar().showMessage(f"Duplicated {item.get('name', 'item')} — unsaved (Ctrl+S to save)", 7000)
```

Find them all:

Run: `grep -n "and wrote {res" native/app.py`
Expected after edits: no matches.

- [ ] **Step 4: Verify Save works end to end**

Run: `.venv/bin/cain`
Expected: open a save, duplicate an item — the title gains a leading `*` and **Save** enables (within ~0.25 s, via the Task 6 timer; until then it enables on the next render). Press `Ctrl+S` — a brief "Saving…" dialog appears, then "Saved", the `*` clears, Save disables, and a single timestamped file appears under `<save folder>/backups/`. Re-open the save in a fresh launch and confirm the duplicate persisted.

- [ ] **Step 5: Commit**

```bash
git add native/app.py
git commit -m "Add single async Save action with progress dialog"
```

---

## Task 6: Dirty-aware close + file-switch prompts

**Files:**
- Modify: `native/app.py` — add `closeEvent`; guard `pick_save` (2347-2363) and any other file-open entry points that call `load_current`

- [ ] **Step 1: Add a reusable confirm-discard helper**

Add to `MainWindow`:

```python
def _confirm_lose_changes(self) -> bool:
    """Return True if it's safe to proceed (no dirt, or user saved/discarded).
    False means cancel the pending action."""
    if not save_api.dirty_paths():
        return True
    choice = QMessageBox.question(
        self, "Unsaved changes",
        "You have unsaved changes. Save them before continuing?",
        QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
        QMessageBox.Save)
    if choice == QMessageBox.Cancel:
        return False
    if choice == QMessageBox.Discard:
        for p in list(save_api.dirty_paths()):
            save_api.discard(p)
        self._update_title()
        return True
    # Save: commit synchronously here (we're about to close/switch anyway)
    res = save_api.commit_all()
    if not res.get("ok"):
        QMessageBox.warning(self, "Save failed",
                            "The edit did not validate and was not written. "
                            "Resolve the issue or discard to continue.")
        return False
    self._update_title()
    return True
```

- [ ] **Step 2: Guard close**

Add to `MainWindow`:

```python
def closeEvent(self, event):
    if self._confirm_lose_changes():
        event.accept()
    else:
        event.ignore()
```

- [ ] **Step 3: Guard file switching**

In `pick_save` (2347-2363), gate on the helper before loading a new file:

```python
def pick_save(self):
    if not self._confirm_lose_changes():
        return
    start = self.settings.value("paths/save", "") or os.path.expanduser("~")
    path, _ = QFileDialog.getOpenFileName(
        self, "Open Diablo II save", start,
        "Diablo II saves (*.d2s *.d2x *.sss *.stash);;All files (*)")
    if path:
        self.settings.setValue("paths/save", path)
        self.load_current()
```

- [ ] **Step 4: Verify the prompts**

Run: `.venv/bin/cain`
Expected: with an unsaved edit, choosing **Open Save** or closing the window prompts Save / Discard / Cancel. Cancel aborts; Discard drops the edit (title `*` clears, the underlying file is unchanged); Save writes then proceeds.

- [ ] **Step 5: Commit**

```bash
git add native/app.py
git commit -m "Prompt to save/discard on close and file switch"
```

---

## Task 7: Debounced realtime validation banner

**Files:**
- Modify: `native/app.py` — add a banner widget into the layout, a poll `QTimer`, and a validate worker

- [ ] **Step 1: Add the validation worker**

In `native/app.py`, near the other workers:

```python
class _ValidateWorker(QThread):
    done = Signal(int, dict)   # (revision-validated, result)

    def __init__(self, path: str, rev: int, parent=None):
        super().__init__(parent)
        self._path = path
        self._rev = rev

    def run(self):
        try:
            res = save_api.validate_buffer(self._path)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "errors": [str(e)]}
        self.done.emit(self._rev, res)
```

- [ ] **Step 2: Add a banner widget**

In `_build_ui`, create a banner and insert it at the top of the main layout (above `self.tabs`/`self.header`). Use a clickable label styled to stand out; store the last errors for the detail popup:

```python
self.validation_banner = QLabel("")
self.validation_banner.setObjectName("validationBanner")
self.validation_banner.setStyleSheet(
    "#validationBanner { background:#5a1e1e; color:#ffd9d9; padding:6px 10px; }")
self.validation_banner.setVisible(False)
self.validation_banner.setCursor(Qt.PointingHandCursor)
self.validation_banner.mousePressEvent = lambda _e: self._show_validation_details()
self._validation_errors: list[str] = []
```

Insert `self.validation_banner` into the top-level layout before the existing first widget (follow the existing `_build_ui` layout code — it is added to the same vertical layout that holds `self.header`).

- [ ] **Step 3: Add the poll timer + handlers**

In `__init__` after `self._build_ui()`, start a single 250 ms timer that syncs the title and triggers a debounced validate when the buffer has changed and then settled:

```python
self._last_rev = 0
self._validated_rev = 0
self._validating = False
self._poll = QTimer(self)
self._poll.setInterval(250)
self._poll.timeout.connect(self._poll_tick)
self._poll.start()
```

Add the methods:

```python
def _poll_tick(self):
    self._update_title()
    if not self.loaded or not save_api.is_warm():
        return
    path = self.loaded.path
    rev = save_api.revision(path)
    if rev == self._last_rev:
        # buffer settled; validate once per new revision
        if rev != self._validated_rev and not self._validating:
            self._validating = True
            self._validator = _ValidateWorker(path, rev, self)
            self._validator.done.connect(self._on_validated)
            self._validator.start()
    self._last_rev = rev

def _on_validated(self, rev: int, res: dict):
    self._validating = False
    self._validated_rev = rev
    if res.get("ok"):
        self._validation_errors = []
        self.validation_banner.setVisible(False)
    else:
        self._validation_errors = list(res.get("errors", []))
        n = len(self._validation_errors)
        self.validation_banner.setText(
            f"⚠ {n} validation issue{'s' if n != 1 else ''} — click for details")
        self.validation_banner.setVisible(True)

def _show_validation_details(self):
    if not self._validation_errors:
        return
    QMessageBox.warning(self, "Validation issues",
                        "\n".join(self._validation_errors[:40]))
```

Ensure `QTimer` is imported (`from PySide6.QtCore import QTimer` — add to the QtCore import group).

- [ ] **Step 4: Verify the banner**

Run: `.venv/bin/cain`
Expected: normal edits keep the banner hidden. To force a failure, make an edit the validator rejects (e.g. via the Item Builder / edit dialog set an out-of-range value if the UI allows, or temporarily corrupt a value) — within ~0.5 s a red banner appears reading "⚠ N validation issues — click for details"; clicking shows the validator messages. Undo/fix and the banner clears on the next debounce. Attempting `Ctrl+S` while invalid shows the "Save failed" dialog and does not write.

- [ ] **Step 5: Commit**

```bash
git add native/app.py
git commit -m "Add debounced in-memory validation banner"
```

---

## Self-review notes

- **Spec coverage:** in-memory buffer (T1–T3), explicit Save writing from memory with backup (T2/T5), Save validates + async + progress bar (T5), backup folder reused (T2 via existing `_backup_original`), warm MPQ on background thread with window usable (T4), per-file dirty + single Save (T2 `commit_all`, T5), Save/Discard/Cancel on close+switch (T6), debounced realtime validation surfaced as a click-for-details banner (T7). All spec sections map to tasks.
- **Type/signature consistency:** `_read_bytes`/`_store_bytes`/`discard`/`dirty_paths`/`revision`/`reset_session`/`commit_save`/`commit_all`/`validate_buffer`/`warm`/`is_warm` are defined in T1/T2/T4 and used unchanged in later tasks; `_SaveWorker.done`/`_ValidateWorker.done`/`_WarmWorker.done` signal shapes match their handlers.
- **Known limitation (acceptable):** the dirty-title indicator and Save-enable update on the 250 ms poll tick rather than synchronously on each edit — chosen to avoid touching all ~15 edit handlers; worst-case lag is one tick.
