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
