"""
LSB-first bit reader/writer for the D2 item bitstream.

D2 packs item data little-endian at the BIT level: within each byte, bit 0 is
read first. A reader and a writer that are exact inverses are the foundation of
the byte-exact round-trip gate.
"""
from __future__ import annotations


class BitReader:
    def __init__(self, data: bytes, start_bit: int = 0):
        self.data = data
        self.pos = start_bit  # absolute bit position

    def read_bit(self) -> int:
        byte = self.data[self.pos >> 3]
        bit = (byte >> (self.pos & 7)) & 1
        self.pos += 1
        return bit

    def read(self, nbits: int) -> int:
        """Read nbits as an unsigned int, LSB first."""
        val = 0
        for i in range(nbits):
            val |= self.read_bit() << i
        return val

    def read_signed(self, nbits: int) -> int:
        val = self.read(nbits)
        if val & (1 << (nbits - 1)):
            val -= (1 << nbits)
        return val

    def read_bool(self) -> bool:
        return self.read_bit() == 1

    def read_string(self, nchars: int, bits_per: int = 7) -> str:
        chars = []
        for _ in range(nchars):
            chars.append(self.read(bits_per))
        return bytes(chars).split(b"\x00")[0].decode("latin-1")

    def align_byte(self) -> None:
        if self.pos & 7:
            self.pos = (self.pos + 7) & ~7

    @property
    def bit_pos(self) -> int:
        return self.pos

    @property
    def byte_pos(self) -> int:
        return self.pos >> 3

    def remaining_bits(self) -> int:
        return len(self.data) * 8 - self.pos


class BitWriter:
    def __init__(self):
        self.bits: list[int] = []

    def write_bit(self, bit: int) -> None:
        self.bits.append(bit & 1)

    def write(self, val: int, nbits: int) -> None:
        for i in range(nbits):
            self.bits.append((val >> i) & 1)

    def write_signed(self, val: int, nbits: int) -> None:
        if val < 0:
            val += (1 << nbits)
        self.write(val, nbits)

    def write_bool(self, b: bool) -> None:
        self.write_bit(1 if b else 0)

    def align_byte(self, fill: int = 0) -> None:
        while len(self.bits) & 7:
            self.bits.append(fill & 1)

    def write_bits_list(self, bits: list[int]) -> None:
        self.bits.extend(bits)

    def to_bytes(self) -> bytes:
        out = bytearray((len(self.bits) + 7) // 8)
        for i, b in enumerate(self.bits):
            if b:
                out[i >> 3] |= 1 << (i & 7)
        return bytes(out)

    @property
    def bit_len(self) -> int:
        return len(self.bits)
