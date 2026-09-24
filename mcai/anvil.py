"""Чтение мира Minecraft Java прямо из файлов: регионы .mca (формат Anvil) и NBT.

Регион r.X.Z.mca хранит 32x32 чанка. Чанк — сжатый NBT, внутри секции по 16 блоков высотой,
у каждой — палитра блоков и упакованный массив индексов. Этого хватает, чтобы узнать любой блок.
"""
import gzip
import io
import math
import os
import struct
import zlib

try:
    import lz4.block  # необязательно: Paper может сжимать чанки LZ4
except ImportError:
    lz4 = None


# --- NBT -------------------------------------------------------------------------

def read_nbt(data):
    f = io.BytesIO(data)
    tag = f.read(1)[0]
    _read_string(f)          # имя корня
    return _read_payload(f, tag)


def _read_string(f):
    (n,) = struct.unpack(">H", f.read(2))
    return f.read(n).decode("utf-8", errors="replace")


def _read_payload(f, tag):
    if tag == 1:
        return struct.unpack(">b", f.read(1))[0]
    if tag == 2:
        return struct.unpack(">h", f.read(2))[0]
    if tag == 3:
        return struct.unpack(">i", f.read(4))[0]
    if tag == 4:
        return struct.unpack(">q", f.read(8))[0]
    if tag == 5:
        return struct.unpack(">f", f.read(4))[0]
    if tag == 6:
        return struct.unpack(">d", f.read(8))[0]
    if tag == 7:
        (n,) = struct.unpack(">i", f.read(4))
        return f.read(n)
    if tag == 8:
        return _read_string(f)
    if tag == 9:
        item, n = struct.unpack(">bi", f.read(5))
        return [_read_payload(f, item) for _ in range(n)]
    if tag == 10:
        out = {}
        while True:
            t = f.read(1)[0]
            if t == 0:
                return out
            name = _read_string(f)
            out[name] = _read_payload(f, t)
    if tag == 11:
        (n,) = struct.unpack(">i", f.read(4))
        return list(struct.unpack(f">{n}i", f.read(4 * n)))
    if tag == 12:
        (n,) = struct.unpack(">i", f.read(4))
        return list(struct.unpack(f">{n}q", f.read(8 * n)))
    raise ValueError(f"unknown NBT tag {tag}")


# --- регионы и чанки -------------------------------------------------------------

def read_chunk(region_dir, cx, cz):
    """NBT чанка (cx, cz) или None, если он ещё не сгенерирован."""
    path = os.path.join(region_dir, f"r.{cx >> 5}.{cz >> 5}.mca")
    try:
        with open(path, "rb") as f:
            index = 4 * ((cx & 31) + (cz & 31) * 32)
            f.seek(index)
            loc = struct.unpack(">I", f.read(4))[0]
            offset, sectors = loc >> 8, loc & 0xFF
            if not offset or not sectors:
                return None
            f.seek(offset * 4096)
            length, compression = struct.unpack(">IB", f.read(5))
            data = f.read(length - 1)
    except FileNotFoundError:
        return None
    if compression == 2:
        raw = zlib.decompress(data)
    elif compression == 1:
        raw = gzip.decompress(data)
    elif compression == 3:
        raw = data
    elif compression == 4 and lz4:
        raw = _lz4_java(data)
    else:
        raise ValueError(f"chunk compression {compression} is not supported")
    return read_nbt(raw)


def _lz4_java(data):
    """LZ4 в формате Java LZ4BlockOutputStream: блоки с заголовком 'LZ4Block'."""
    out, pos = bytearray(), 0
    while pos < len(data):
        token = data[pos + 8]
        clen, dlen = struct.unpack("<ii", data[pos + 9:pos + 17])
        body = data[pos + 21:pos + 21 + clen]
        method = token & 0xF0
        out += body if method == 0x10 else lz4.block.decompress(body, uncompressed_size=dlen)
        pos += 21 + clen
        if dlen == 0:
            break
    return bytes(out)


class Section:
    """16x16x16 блоков: палитра и индексы."""

    def __init__(self, nbt):
        states = nbt.get("block_states", {})
        self.palette = [p.get("Name", "minecraft:air") for p in states.get("palette", [])] or ["minecraft:air"]
        data = states.get("data")
        if data is None or len(self.palette) == 1:
            self.indices = None
            return
        bits = max(4, math.ceil(math.log2(len(self.palette))))
        per_long, mask = 64 // bits, (1 << bits) - 1
        idx = []
        for value in data:
            value &= (1 << 64) - 1
            for i in range(per_long):
                idx.append((value >> (i * bits)) & mask)
        self.indices = idx[:4096]

    def block(self, x, y, z):
        if self.indices is None:
            return self.palette[0]
        return self.palette[self.indices[(y << 8) | (z << 4) | x]]


class World:
    """Блоки мира по координатам; чанки читаются с диска и кэшируются."""

    def __init__(self, region_dir):
        self.region_dir = region_dir
        self.chunks = {}

    def _sections(self, cx, cz):
        if (cx, cz) not in self.chunks:
            nbt = read_chunk(self.region_dir, cx, cz)
            sections = {}
            for s in (nbt or {}).get("sections", []):
                if "block_states" in s:
                    sections[s["Y"]] = Section(s)
            self.chunks[(cx, cz)] = sections if nbt else None
        return self.chunks[(cx, cz)]

    def block(self, x, y, z):
        """'minecraft:stone', 'minecraft:air' или None, если чанк не сгенерирован."""
        sections = self._sections(x >> 4, z >> 4)
        if sections is None:
            return None
        s = sections.get(y >> 4)
        return s.block(x & 15, y & 15, z & 15) if s else "minecraft:air"
