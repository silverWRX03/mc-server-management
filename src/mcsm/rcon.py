"""Source RCON client, for sending commands to a running server from another shell."""

from __future__ import annotations

import socket
import struct
from pathlib import Path

from .properties import read_properties

LOGIN, COMMAND, RESPONSE = 3, 2, 0


class RconError(Exception):
    pass


def encode(request_id: int, kind: int, body: str) -> bytes:
    payload = struct.pack("<ii", request_id, kind) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def decode(data: bytes) -> tuple[int, int, str]:
    request_id, kind = struct.unpack("<ii", data[:8])
    return request_id, kind, data[8:-2].decode("utf-8", errors="replace")


class Rcon:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock: socket.socket | None = None
        self._next_id = 1

    @classmethod
    def from_server_dir(cls, server_dir: Path) -> Rcon:
        props = read_properties(server_dir / "server.properties")
        if props.get("enable-rcon") != "true" or not props.get("rcon.password"):
            raise RconError("RCON is disabled: set enable-rcon=true and rcon.password in server.properties")
        return cls("127.0.0.1", int(props.get("rcon.port", "25575")), props["rcon.password"])

    def __enter__(self) -> Rcon:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        _, kind, _ = self._request(LOGIN, self.password, expect_id=False)
        return self

    def __exit__(self, *exc) -> None:
        if self.sock:
            self.sock.close()

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf

    def _request(self, kind: int, body: str, expect_id: bool = True) -> tuple[int, int, str]:
        request_id = self._next_id
        self._next_id += 1
        self.sock.sendall(encode(request_id, kind, body))
        (length,) = struct.unpack("<i", self._recv_exact(4))
        rid, rkind, text = decode(self._recv_exact(length))
        if rid == -1:
            raise RconError("RCON authentication failed")
        return rid, rkind, text

    def command(self, command: str) -> str:
        return self._request(COMMAND, command)[2]
