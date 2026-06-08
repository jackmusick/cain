"""
Minimal, dependency-free MPQ archive reader.

Scope: just enough to extract the data tables PD2 needs (.txt files in
data\\global\\excel\\). Reads the classic hash-table/block-table MPQ layout,
handles the standard file decryption and (un)compression used by D2/PD2 data
MPQs (zlib + PKWARE implode). No write support — we never modify the MPQ.

This is the RE/prototype implementation in Python. The validated logic is the
spec for the eventual Rust port (see PD2_SAVE_EDITOR_PLAN.md).
"""
from __future__ import annotations

import struct
import zlib
import io


# --- MPQ crypt table (generated once, as Blizzard's algorithm specifies) ---
def _build_crypt_table() -> list[int]:
    table = [0] * 0x500
    seed = 0x00100001
    for index1 in range(0x100):
        index2 = index1
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            temp1 = (seed & 0xFFFF) << 0x10
            seed = (seed * 125 + 3) % 0x2AAAAB
            temp2 = seed & 0xFFFF
            table[index2] = temp1 | temp2
            index2 += 0x100
    return table


_CRYPT = _build_crypt_table()

MPQ_HASH_TABLE_OFFSET = 0
MPQ_HASH_NAME_A = 1
MPQ_HASH_NAME_B = 2
MPQ_HASH_FILE_KEY = 3


def _hash(string: str, hash_type: int) -> int:
    seed1 = 0x7FED7FED
    seed2 = 0xEEEEEEEE
    for ch in string.upper():
        c = ord(ch)
        seed1 = _CRYPT[(hash_type << 8) + c] ^ ((seed1 + seed2) & 0xFFFFFFFF)
        seed1 &= 0xFFFFFFFF
        seed2 = (c + seed1 + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return seed1


def _decrypt(data: bytes, key: int) -> bytes:
    out = bytearray()
    seed2 = 0xEEEEEEEE
    key &= 0xFFFFFFFF
    n = len(data) // 4
    ints = struct.unpack(f"<{n}I", data[: n * 4])
    result = []
    for value in ints:
        seed2 = (seed2 + _CRYPT[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        ch = (value ^ (key + seed2)) & 0xFFFFFFFF
        key = (((~key & 0xFFFFFFFF) << 0x15) + 0x11111111) | (key >> 0x0B)
        key &= 0xFFFFFFFF
        seed2 = (ch + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
        result.append(ch)
    out += struct.pack(f"<{n}I", *result)
    out += data[n * 4:]  # trailing bytes left as-is
    return bytes(out)


# --- compression handlers ---------------------------------------------------
_COMP_ZLIB = 0x02
_COMP_PKWARE = 0x08
_COMP_BZIP2 = 0x10


def _decompress(data: bytes, expected_size: int) -> bytes:
    """Sector decompression. First byte = compression mask (multi-compression)."""
    if not data:
        return data
    if len(data) >= expected_size:
        # stored uncompressed (no mask byte)
        return data[:expected_size]
    mask = data[0]
    payload = data[1:]
    if mask & _COMP_ZLIB:
        return zlib.decompress(payload)
    if mask & _COMP_BZIP2:
        import bz2
        return bz2.decompress(payload)
    if mask & _COMP_PKWARE:
        return _pkware_explode(payload, expected_size)
    # unknown / single-mask we don't handle yet
    raise NotImplementedError(f"Unsupported MPQ compression mask: 0x{mask:02x}")


# --- PKWARE DCL "implode" decompressor (explode) ----------------------------
# D2 data MPQs commonly use this. Implemented as a faithful port of zlib's
# blast.c (canonical DCL decoder). In the Rust port, StormLib handles DCL.
def _pkware_explode(data: bytes, expected_size: int) -> bytes:
    return _pkware_explode_native(data, expected_size)


# Faithful port of zlib's blast.c (Mark Adler) — the canonical PKWARE DCL
# decompressor. Decodes via single-bit-prepend Huffman matching, which is the
# known-correct approach (my earlier attempt mishandled bit order).

# --- blast.c verbatim data (validated against zlib's "AIAIAIAIAIAIA" vector) ---
_MAXBITS = 13
# base[]/extra[] for length codes.
_LEN_BASE = [3, 2, 4, 5, 6, 7, 8, 9, 10, 12, 16, 24, 40, 72, 136, 264]
_LEN_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8]
# Rep-encoded code lengths, transcribed verbatim from zlib/contrib/blast/blast.c.
# Each byte: low nibble = bit length, high nibble = (repeat count - 1).
_LENLEN = [2, 35, 36, 53, 38, 23]
_DISTLEN = [2, 20, 53, 230, 247, 151, 248]


class _Huffman:
    """Port of blast.c construct(): build count[] and the (length,symbol)-sorted
    symbol[] table from a rep-encoded code-length list."""

    def __init__(self, rep: list[int]):
        length = []
        for sym in rep:
            n = (sym >> 4) + 1
            l = sym & 15
            length += [l] * n
        n = len(length)
        self.count = [0] * (_MAXBITS + 1)
        for l in length:
            self.count[l] += 1
        offs = [0, 0]
        for l in range(1, _MAXBITS):
            offs.append(offs[l] + self.count[l])
        self.symbol = [0] * n
        for s in range(n):
            if length[s]:
                self.symbol[offs[length[s]]] = s
                offs[length[s]] += 1

    def decode(self, br: "_BitStream") -> int:
        # Port of blast.c decode(): invert each bit into the code, compare to the
        # first canonical code of each length.
        code = first = index = 0
        for length in range(1, _MAXBITS + 1):
            code |= br.read_bit() ^ 1
            count = self.count[length]
            if code < first + count:
                return self.symbol[index + (code - first)]
            index += count
            first = (first + count) << 1
            code <<= 1
        raise ValueError("PKWARE Huffman decode ran out of bits")


class _BitStream:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.bitbuf = 0
        self.bitcnt = 0

    def read_bit(self) -> int:
        if self.bitcnt == 0:
            if self.pos >= len(self.data):
                self.bitbuf = 0
            else:
                self.bitbuf = self.data[self.pos]
                self.pos += 1
            self.bitcnt = 8
        bit = self.bitbuf & 1
        self.bitbuf >>= 1
        self.bitcnt -= 1
        return bit

    def read_bits(self, n: int) -> int:
        val = 0
        for i in range(n):
            val |= self.read_bit() << i
        return val


_LENCODE = _Huffman(_LENLEN)
_DISTCODE = _Huffman(_DISTLEN)


def _pkware_explode_native(data: bytes, expected_size: int) -> bytes:
    br = _BitStream(data)
    lit_mode = br.read_bits(8)   # 0 = uncoded literals, 1 = coded (we handle 0; D2 uses 0)
    dict_bits = br.read_bits(8)  # 4, 5, or 6
    if dict_bits not in (4, 5, 6):
        raise ValueError(f"bad PKWARE dict bits {dict_bits}")
    if lit_mode not in (0, 1):
        raise ValueError(f"bad PKWARE literal mode {lit_mode}")
    out = bytearray()
    while len(out) < expected_size:
        if br.read_bit():  # length/distance pair
            sym = _LENCODE.decode(br)
            length = _LEN_BASE[sym] + br.read_bits(_LEN_EXTRA[sym])
            if length == 519:  # end-of-stream marker
                break
            dsym = _DISTCODE.decode(br)
            if length == 2:
                dist = (dsym << 2) + br.read_bits(2) + 1
            else:
                dist = (dsym << dict_bits) + br.read_bits(dict_bits) + 1
            for _ in range(length):
                out.append(out[-dist])
        else:  # literal byte
            out.append(br.read_bits(8))
    return bytes(out[:expected_size])


# --- main archive class -----------------------------------------------------
class MPQArchive:
    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.raw = f.read()
        self._parse_header()
        self._read_tables()

    def _parse_header(self):
        d = self.raw
        # locate 'MPQ\x1a' (user data header 'MPQ\x1b' can precede)
        off = d.find(b"MPQ\x1a")
        if off < 0:
            raise ValueError("not an MPQ archive")
        self.archive_offset = off
        (magic, header_size, archive_size, fmt_version,
         block_size_shift, hash_table_pos, block_table_pos,
         hash_table_count, block_table_count) = struct.unpack_from(
            "<4sIIHHIIII", d, off)
        self.sector_size = 512 << block_size_shift
        self.hash_table_pos = off + hash_table_pos
        self.block_table_pos = off + block_table_pos
        self.hash_table_count = hash_table_count
        self.block_table_count = block_table_count

    def _read_tables(self):
        d = self.raw
        # hash table
        ht_size = self.hash_table_count * 16
        ht = _decrypt(d[self.hash_table_pos:self.hash_table_pos + ht_size],
                      _hash("(hash table)", MPQ_HASH_FILE_KEY))
        self.hash_table = [struct.unpack_from("<IIHHI", ht, i * 16)
                           for i in range(self.hash_table_count)]
        # block table
        bt_size = self.block_table_count * 16
        bt = _decrypt(d[self.block_table_pos:self.block_table_pos + bt_size],
                      _hash("(block table)", MPQ_HASH_FILE_KEY))
        self.block_table = [struct.unpack_from("<IIII", bt, i * 16)
                            for i in range(self.block_table_count)]

    def _find(self, filename: str):
        idx = _hash(filename, MPQ_HASH_TABLE_OFFSET) % self.hash_table_count
        name_a = _hash(filename, MPQ_HASH_NAME_A)
        name_b = _hash(filename, MPQ_HASH_NAME_B)
        for _ in range(self.hash_table_count):
            entry = self.hash_table[idx]
            ha, hb, locale, platform, block_index = entry
            if block_index == 0xFFFFFFFF:
                return None
            if ha == name_a and hb == name_b:
                return block_index
            idx = (idx + 1) % self.hash_table_count
        return None

    def read_file(self, filename: str) -> bytes | None:
        block_index = self._find(filename)
        if block_index is None:
            return None
        file_pos, comp_size, uncomp_size, flags = self.block_table[block_index]
        file_pos += self.archive_offset
        FLAG_IMPLODE = 0x00000100     # single-method PKWARE implode (NO mask byte)
        FLAG_COMPRESSED = 0x00000200  # multi-compression (leading mask byte)
        FLAG_ENCRYPTED = 0x00010000
        FLAG_SINGLE_UNIT = 0x01000000

        if flags & FLAG_ENCRYPTED:
            # Encrypted files need a per-file key derived from the basename;
            # PD2 data tables are typically not encrypted. Defer if hit.
            raise NotImplementedError(f"encrypted file not yet supported: {filename}")

        data = self.raw[file_pos:file_pos + comp_size]

        def _inflate_sector(sector: bytes, out_size: int) -> bytes:
            if len(sector) >= out_size:
                return sector[:out_size]  # stored
            if flags & FLAG_IMPLODE:
                return _pkware_explode(sector, out_size)  # no mask byte
            if flags & FLAG_COMPRESSED:
                return _decompress(sector, out_size)      # mask byte inside
            return sector[:out_size]

        if flags & FLAG_SINGLE_UNIT:
            return _inflate_sector(data, uncomp_size)

        # multi-sector
        num_sectors = (uncomp_size + self.sector_size - 1) // self.sector_size
        offsets = struct.unpack_from(f"<{num_sectors + 1}I", data, 0)
        out = bytearray()
        for s in range(num_sectors):
            start, end = offsets[s], offsets[s + 1]
            sector = data[start:end]
            this_size = min(self.sector_size, uncomp_size - len(out))
            out += _inflate_sector(sector, this_size)
        return bytes(out[:uncomp_size])

    def list_known(self, names: list[str]) -> dict[str, bool]:
        return {n: self._find(n) is not None for n in names}


if __name__ == "__main__":
    import sys
    arc = MPQArchive(sys.argv[1])
    print(f"sector_size={arc.sector_size} "
          f"hash_entries={arc.hash_table_count} "
          f"block_entries={arc.block_table_count}")
    target = sys.argv[2] if len(sys.argv) > 2 else r"data\global\excel\ItemStatCost.txt"
    blob = arc.read_file(target)
    if blob is None:
        print(f"NOT FOUND: {target}")
    else:
        print(f"READ {target}: {len(blob)} bytes")
        sys.stdout.buffer.write(blob[:300])
