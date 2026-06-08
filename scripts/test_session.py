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
        assert not save_api.dirty_paths(), "session should be empty after discard"
        fresh = save_api._read_bytes(path)
        assert bytes(fresh) == open(path, "rb").read(), "discard must drop the buffer"
        print("PASS discard reloads from disk")
    finally:
        os.remove(path)
        save_api.reset_session()


def test_store_bytes_marks_dirty_and_bumps_revision():
    save_api.reset_session()
    path = _tmp_copy()
    try:
        buf = save_api._read_bytes(path)
        assert save_api.revision(path) == 0, "fresh load is revision 0"
        assert not save_api.dirty_paths(), "fresh load is not dirty"
        save_api._store_bytes(path, bytes(buf))
        assert len(save_api.dirty_paths()) == 1, "store must mark exactly one path dirty"
        assert save_api._session_key(path) in save_api.dirty_paths()
        assert save_api.revision(path) == 1, "first store bumps revision to 1"
        save_api._store_bytes(path, bytes(buf))
        assert save_api.revision(path) == 2, "second store bumps revision to 2"
        print("PASS store_bytes marks dirty and bumps revision")
    finally:
        os.remove(path)
        save_api.reset_session()


def path_in_dirty(path):
    return os.path.abspath(os.path.expanduser(path)) in save_api.dirty_paths()


def _have_mpq():
    mpq = os.environ.get("PD2_MPQ", "")
    if mpq and os.path.exists(mpq):
        save_api.set_mpq(mpq)
        return True
    return False


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
        backups = os.path.join(os.path.dirname(path), "backups")
        assert not os.path.isdir(backups), "edits must not create backups"
        print("PASS _gate_and_write buffers the edit without disk/backup")
    finally:
        os.remove(path)
        save_api.reset_session()


def test_commit_writes_disk_and_backup():
    if not _have_mpq():
        print("SKIP commit test (no PD2_MPQ)")
        return
    save_api.reset_session()
    path = _tmp_copy()
    try:
        buf = save_api._read_bytes(path)
        save_api._gate_and_write(bytearray(buf), path)   # dirty but identical+valid
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


def test_commit_rejects_invalid_buffer():
    if not _have_mpq():
        print("SKIP reject test (no PD2_MPQ)")
        return
    save_api.reset_session()
    path = _tmp_copy()
    try:
        disk = open(path, "rb").read()
        save_api._read_bytes(path)
        # Flip an in-range name byte: stays classified d2s, fails validation
        # (checksum mismatch) without raising.
        corrupted = bytearray(disk)
        corrupted[0x10] = 0x21
        save_api._store_bytes(path, corrupted)
        assert not save_api.validate_buffer(path)["ok"], \
            "chosen corruption must fail validation"
        res = save_api.commit_all()
        assert not res.get("ok"), f"commit must reject invalid buffer, got {res}"
        assert path_in_dirty(path), "rejection must leave the path dirty"
        assert open(path, "rb").read() == disk, "rejected commit must NOT write disk"
        backups = os.path.join(os.path.dirname(path), "backups")
        assert not os.path.isdir(backups), "rejected commit must not create backups"
        print("PASS commit_all rejects invalid buffer (no write, stays dirty)")
    finally:
        shutil.rmtree(os.path.join(os.path.dirname(path), "backups"), ignore_errors=True)
        os.remove(path)
        save_api.reset_session()


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


def main():
    test_read_bytes_loads_disk_then_buffer()
    test_discard_reloads_disk()
    test_store_bytes_marks_dirty_and_bumps_revision()
    test_gate_and_write_buffers_no_disk()
    test_commit_writes_disk_and_backup()
    test_commit_rejects_invalid_buffer()
    test_edit_then_commit_roundtrip()
    print("ALL SESSION TESTS PASSED")


if __name__ == "__main__":
    main()
