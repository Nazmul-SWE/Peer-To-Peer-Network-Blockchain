"""
protocol.py - application-level protocol for PeerLink
=====================================================

TCP only gives us a reliable, ordered *stream of bytes*.  It knows nothing
about "messages": two ``send()`` calls on one side may arrive as one
``recv()`` on the other, or one message may be split across several.  This
module solves that "message framing" problem and defines the vocabulary
(message types) that peers speak.

Wire format of every control message::

    +------------------+---------------------------+
    | 4-byte length N  |  N bytes of UTF-8 JSON    |
    | (big-endian)     |  (one JSON object)        |
    +------------------+---------------------------+

A file transfer is a control message followed by raw bytes::

    [framed JSON {"type": "file", "filename": ..., "filesize": S}]
    [S raw bytes of the file ........................................]

The receiver uses ``filesize`` (never "one recv() call") to know where the
file ends and the next framed message begins.

Message types
-------------
hello      initiator  -> acceptor   "this is who I am"
hello_ack  acceptor   -> initiator  "and this is who I am"
text       either way               a chat message
file       either way               metadata; raw bytes follow immediately
error      acceptor   -> initiator  handshake refused (extra, optional)

Only the standard library is used.
"""

from __future__ import annotations

import json
import socket
import struct
import uuid
from typing import Callable, Optional, Tuple

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
PROTOCOL_VERSION = 1

LENGTH_PREFIX_SIZE = 4            # bytes used for the length header
LENGTH_FORMAT = ">I"              # unsigned 32-bit, big-endian ("network order")
MAX_MESSAGE_SIZE = 1024 * 1024    # control messages are tiny; refuse > 1 MiB
FILE_CHUNK_SIZE = 64 * 1024       # files are streamed in 64 KiB pieces

MAX_NAME_LENGTH = 24
MAX_TEXT_LENGTH = 4000

MSG_HELLO = "hello"
MSG_HELLO_ACK = "hello_ack"
MSG_TEXT = "text"
MSG_FILE = "file"
MSG_ERROR = "error"


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------
class ProtocolError(Exception):
    """The remote side violated the protocol (bad JSON, bad size, ...)."""


class ConnectionClosed(ConnectionError):
    """The remote side closed the TCP connection (EOF while reading)."""


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
def generate_peer_id() -> str:
    """Return a short random id such as ``'a83f21c4'`` (8 hex characters)."""
    return uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------
# Message builders (one place defines every field name)
# --------------------------------------------------------------------------
def make_hello(peer_id: str, name: str, port: int) -> dict:
    return {"type": MSG_HELLO, "version": PROTOCOL_VERSION,
            "peer_id": peer_id, "peer_name": name, "port": port}


def make_hello_ack(peer_id: str, name: str, port: int) -> dict:
    return {"type": MSG_HELLO_ACK, "version": PROTOCOL_VERSION,
            "peer_id": peer_id, "peer_name": name, "port": port}


def make_text(peer_id: str, name: str, text: str) -> dict:
    return {"type": MSG_TEXT, "sender_id": peer_id,
            "sender_name": name, "message": text}


def make_file(peer_id: str, name: str, filename: str, filesize: int) -> dict:
    return {"type": MSG_FILE, "sender_id": peer_id, "sender_name": name,
            "filename": filename, "filesize": filesize}


def make_error(reason: str) -> dict:
    return {"type": MSG_ERROR, "reason": reason}


def parse_handshake(message: dict) -> Tuple[str, str, int]:
    """Validate a hello / hello_ack and return ``(peer_id, name, port)``."""
    peer_id = message.get("peer_id")
    name = message.get("peer_name")
    port = message.get("port")

    if not isinstance(peer_id, str) or not 1 <= len(peer_id) <= 64:
        raise ProtocolError("handshake has an invalid peer_id")
    if not isinstance(name, str) or not name.strip():
        raise ProtocolError("handshake has an invalid peer_name")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ProtocolError("handshake has an invalid port")
    return peer_id, name.strip()[:MAX_NAME_LENGTH], port


# --------------------------------------------------------------------------
# Framing: sending
# --------------------------------------------------------------------------
def encode_message(message: dict) -> bytes:
    """dict -> ``[4-byte length][UTF-8 JSON]``."""
    payload = json.dumps(message).encode("utf-8")
    if len(payload) > MAX_MESSAGE_SIZE:
        raise ProtocolError("message is too large to send")
    return struct.pack(LENGTH_FORMAT, len(payload)) + payload


def send_message(sock: socket.socket, message: dict) -> None:
    """Frame and fully send one control message (``sendall`` loops for us)."""
    sock.sendall(encode_message(message))


# --------------------------------------------------------------------------
# Framing: receiving
# --------------------------------------------------------------------------
def recv_exact(sock: socket.socket, count: int) -> bytes:
    """
    Read *exactly* ``count`` bytes.

    ``recv(n)`` may legally return fewer than ``n`` bytes, so we loop until
    we have them all.  If the peer closes first, ConnectionClosed is raised.
    """
    buffer = bytearray(count)
    view = memoryview(buffer)
    received = 0
    while received < count:
        got = sock.recv_into(view[received:], count - received)
        if got == 0:
            raise ConnectionClosed("connection closed by peer")
        received += got
    return bytes(buffer)


def recv_message(sock: socket.socket) -> dict:
    """Read one framed JSON control message and return it as a dict."""
    header = recv_exact(sock, LENGTH_PREFIX_SIZE)
    (length,) = struct.unpack(LENGTH_FORMAT, header)
    if length == 0 or length > MAX_MESSAGE_SIZE:
        raise ProtocolError(f"invalid message length: {length}")

    payload = recv_exact(sock, length)
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"malformed message: {exc}") from exc

    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ProtocolError("message must be a JSON object with a 'type'")
    return message


def receive_stream(
    sock: socket.socket,
    size: int,
    write: Optional[Callable[[memoryview], object]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
) -> Optional[OSError]:
    """
    Read exactly ``size`` raw bytes (a file body) in chunks of at most
    FILE_CHUNK_SIZE and hand each chunk to ``write``.

    * The loop ends when ``size`` bytes have arrived - *not* when one
      ``recv`` returns - which is how the receiver knows the file is done.
    * If ``write`` fails (e.g. disk full) the remaining bytes are still read
      and discarded so the TCP stream stays in sync; the OSError is returned.
    * ConnectionClosed is raised if the peer vanishes mid-transfer.
    """
    write_error: Optional[OSError] = None
    received = 0
    buffer = bytearray(min(FILE_CHUNK_SIZE, size)) if size > 0 else bytearray()
    view = memoryview(buffer)

    while received < size:
        want = min(len(buffer), size - received)
        got = sock.recv_into(view[:want], want)
        if got == 0:
            raise ConnectionClosed("connection closed during file transfer")
        if write is not None and write_error is None:
            try:
                write(view[:got])
            except OSError as exc:
                write_error = exc
        received += got
        if on_progress is not None:
            on_progress(received)
    return write_error
