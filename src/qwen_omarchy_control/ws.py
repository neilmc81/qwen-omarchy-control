"""Minimal WebSocket client (RFC 6455), stdlib only.

Why this exists
---------------
The direct-CDP route (see `cdp.py`) needs to speak the WebSocket protocol to
Chrome's DevTools endpoint, and this project deliberately has no third-party
Python dependencies - the MCP server is stdlib-only, and adding a package would
be one more thing to install and keep working on every machine.

This is not a general WebSocket implementation. It does exactly what a CDP client
needs: a client handshake against loopback, and text frames in both directions
with masking on the way out. Deliberately unsupported, and refused rather than
half-done: TLS, extensions, subprotocols, fragmentation on send, and compression.

The framing is small enough to be testable, which is the point: `encode_frame`
and `decode_frame` are pure functions and are covered directly by tests, so the
part that is easy to get subtly wrong is not buried inside socket handling.
"""

from __future__ import annotations

import base64
import os
import socket
import struct
from urllib.parse import urlparse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes we care about.
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(RuntimeError):
    """The socket could not be opened, or the peer spoke something unexpected."""


# --- framing (pure functions: tested directly) -----------------------------


def encode_frame(payload: bytes, opcode: int = OP_TEXT, mask: bool = True) -> bytes:
    """One client frame. Client frames MUST be masked (RFC 6455 §5.3).

    Payloads are sent unmasked only in tests; Chrome closes the connection if a
    client frame arrives unmasked.
    """
    header = bytearray()
    header.append(0x80 | opcode)  # FIN + opcode
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)

    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + key + masked


def decode_frame(data: bytes) -> tuple[int, bytes, int]:
    """Decode one frame -> (opcode, payload, bytes_consumed).

    Server frames are never masked, so an incoming masked frame is a protocol
    error. Raises WebSocketError rather than guessing at a malformed length.
    """
    if len(data) < 2:
        raise WebSocketError("frame too short")
    b0, b1 = data[0], data[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        if len(data) < 4:
            raise WebSocketError("truncated 16-bit length")
        length = struct.unpack("!H", data[2:4])[0]
        offset = 4
    elif length == 127:
        if len(data) < 10:
            raise WebSocketError("truncated 64-bit length")
        length = struct.unpack("!Q", data[2:10])[0]
        offset = 10
    if masked:
        raise WebSocketError("server frame must not be masked")
    if len(data) < offset + length:
        raise WebSocketError("truncated payload")
    payload = data[offset:offset + length]
    if not fin:
        raise WebSocketError("fragmented frames are not supported")
    return opcode, payload, offset + length


# --- connection ------------------------------------------------------------


class WebSocket:
    """A blocking client connection. Use as a context manager."""

    def __init__(self, url: str, timeout: float = 20.0):
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise WebSocketError(f"only ws:// is supported, got {parsed.scheme!r}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        self.timeout = timeout
        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._buffer = b""
        try:
            self._handshake(host, port, path)
        except Exception:
            self._socket.close()
            raise

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._socket.sendall(request.encode())

        # Read until the end of the response headers.
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            raw += chunk
        head, _, rest = raw.partition(b"\r\n\r\n")
        self._buffer = rest
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise WebSocketError(f"handshake failed: {status}")
        expected = base64.b64encode(
            __import__("hashlib").sha1((key + _GUID).encode()).digest()
        ).decode()
        if expected.lower().encode() not in head.lower():
            raise WebSocketError("handshake did not echo the expected key")

    # -- io --
    def send(self, text: str) -> None:
        self._socket.sendall(encode_frame(text.encode()))

    def _read_frame(self) -> tuple[int, bytes]:
        while True:
            # Try to decode from what we already have.
            if len(self._buffer) >= 2:
                try:
                    opcode, payload, used = decode_frame(self._buffer)
                except WebSocketError as exc:
                    # Only "truncated" is recoverable by reading more.
                    if "truncated" not in str(exc):
                        raise
                else:
                    self._buffer = self._buffer[used:]
                    if opcode == OP_PING:
                        self._socket.sendall(encode_frame(payload, OP_PONG))
                        continue
                    if opcode == OP_CLOSE:
                        raise WebSocketError("peer closed the connection")
                    return opcode, payload
            chunk = self._socket.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buffer += chunk

    def recv(self, idle_timeout: float | None = None) -> str:
        """The next text message. Ignores control frames, answers pings."""
        if idle_timeout is not None:
            self._socket.settimeout(idle_timeout)
        try:
            opcode, payload = self._read_frame()
        finally:
            if idle_timeout is not None:
                self._socket.settimeout(self.timeout)
        if opcode != OP_TEXT:
            raise WebSocketError(f"expected a text frame, got opcode {opcode}")
        return payload.decode(errors="replace")

    def close(self) -> None:
        try:
            self._socket.sendall(encode_frame(b"", OP_CLOSE))
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass

    def __enter__(self) -> "WebSocket":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["WebSocket", "WebSocketError", "encode_frame", "decode_frame",
           "OP_TEXT", "OP_CLOSE", "OP_PING", "OP_PONG"]
