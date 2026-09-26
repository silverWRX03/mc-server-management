"""Just enough of Minecraft's NBT format to read and write ``servers.dat`` (the
multiplayer server list), which is uncompressed NBT."""

from __future__ import annotations

import struct
from typing import Any

END, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE, BYTE_ARRAY, STRING, LIST, COMPOUND, INT_ARRAY, LONG_ARRAY = range(13)


class NBTError(ValueError):
    pass


class Tagged:
    """A value with an explicit NBT type (used for numbers, whose Python type is ambiguous)."""

    def __init__(self, kind: int, value: Any):
        self.kind, self.value = kind, value

    def __eq__(self, other):
        return isinstance(other, Tagged) and (self.kind, self.value) == (other.kind, other.value)

    def __repr__(self):
        return f"Tagged({self.kind}, {self.value!r})"


class ListTag(list):
    def __init__(self, kind: int, items=()):
        super().__init__(items)
        self.kind = kind


_FMT = {BYTE: ">b", SHORT: ">h", INT: ">i", LONG: ">q", FLOAT: ">f", DOUBLE: ">d"}


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise NBTError("truncated NBT")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def num(self, fmt: str):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self) -> str:
        return self.take(self.num(">H")).decode("utf-8", errors="replace")

    def payload(self, kind: int, depth: int = 0):
        if depth > 64:
            raise NBTError("NBT nested too deeply")
        if kind in _FMT:
            return Tagged(kind, self.num(_FMT[kind]))
        if kind == BYTE_ARRAY:
            return Tagged(kind, self.take(self.num(">i")))
        if kind == STRING:
            return self.string()
        if kind == LIST:
            item_kind, n = self.num(">b"), self.num(">i")
            return ListTag(item_kind, [self.payload(item_kind, depth + 1) for _ in range(max(n, 0))])
        if kind == COMPOUND:
            out = {}
            while (child := self.num(">b")) != END:
                name = self.string()
                out[name] = self.payload(child, depth + 1)
            return out
        if kind == INT_ARRAY:
            return Tagged(kind, [self.num(">i") for _ in range(self.num(">i"))])
        if kind == LONG_ARRAY:
            return Tagged(kind, [self.num(">q") for _ in range(self.num(">i"))])
        raise NBTError(f"unknown NBT tag {kind}")


def loads(data: bytes) -> dict:
    """Read an uncompressed NBT file whose root is a compound."""
    r = _Reader(data)
    if r.num(">b") != COMPOUND:
        raise NBTError("not an NBT compound")
    r.string()
    return r.payload(COMPOUND)


def _kind(value) -> int:
    if isinstance(value, Tagged):
        return value.kind
    if isinstance(value, ListTag):
        return LIST
    if isinstance(value, str):
        return STRING
    if isinstance(value, dict):
        return COMPOUND
    if isinstance(value, bool):
        return BYTE
    if isinstance(value, int):
        return INT
    raise NBTError(f"can't store {type(value).__name__} in NBT")


def _payload(kind: int, value) -> bytes:
    if isinstance(value, Tagged):
        value = value.value
    if kind in _FMT:
        return struct.pack(_FMT[kind], value)
    if kind == BYTE_ARRAY:
        return struct.pack(">i", len(value)) + bytes(value)
    if kind == STRING:
        raw = value.encode("utf-8")
        return struct.pack(">H", len(raw)) + raw
    if kind == LIST:
        item_kind = value.kind if isinstance(value, ListTag) else (_kind(value[0]) if value else END)
        return struct.pack(">bi", item_kind, len(value)) + b"".join(_payload(item_kind, v) for v in value)
    if kind == COMPOUND:
        out = b""
        for name, child in value.items():
            k = _kind(child)
            out += struct.pack(">b", k) + _payload(STRING, name) + _payload(k, child)
        return out + b"\x00"
    if kind == INT_ARRAY:
        return struct.pack(">i", len(value)) + b"".join(struct.pack(">i", v) for v in value)
    if kind == LONG_ARRAY:
        return struct.pack(">i", len(value)) + b"".join(struct.pack(">q", v) for v in value)
    raise NBTError(f"unknown NBT tag {kind}")


def dumps(root: dict) -> bytes:
    return struct.pack(">b", COMPOUND) + _payload(STRING, "") + _payload(COMPOUND, root)


def add_server(data: bytes | None, name: str, address: str) -> bytes:
    """``servers.dat`` with the server first in the list (updating its entry if it's there)."""
    root = loads(data) if data else {}
    servers = root.get("servers")
    if not isinstance(servers, list):
        servers = ListTag(COMPOUND)
    servers = ListTag(COMPOUND, [s for s in servers if not (isinstance(s, dict) and s.get("ip") == address)])
    servers.insert(0, {"name": name, "ip": address, "acceptTextures": Tagged(BYTE, 1)})
    root["servers"] = servers
    return dumps(root)
