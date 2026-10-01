"""
p2p_node.py - the peer-to-peer networking layer
===============================================

One ``PeerNode`` is one peer.  It is simultaneously

    * a TCP **server**  - a background thread ``accept()``s incoming peers
    * a TCP **client**  - ``connect_to_peer()`` dials out to other peers

so there is no central server anywhere:  ``Every peer = TCP Server + TCP Client``.

Threading model
---------------
    accept thread ............ 1 per node          (server role)
    reader thread ............ 1 per connection    (receives text/files)
    writer thread ............ 1 per connection    (sends text/files)
    connect thread ........... short-lived          (client role, keeps GUI responsive)

Every connection owns an *outbox queue* drained by its single writer thread.
Consequences: sends never block the GUI, messages keep their order, and a
text message can never be interleaved into the middle of a file's raw bytes
(only the writer thread ever writes to that socket).

The node never touches the GUI.  Everything that happens is reported through
one callback ``on_event(event_name, **data)`` which may be called from any
thread.  Events:

    started, stopped
    peer_connected, peer_disconnected, connect_failed
    text_received, text_sent
    file_progress, file_received, file_sent
    info, error
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

import protocol
import utils

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0       # seconds to establish the TCP connection
HANDSHAKE_TIMEOUT = 10.0     # seconds a new peer has to complete the HELLO exchange
ACCEPT_POLL = 0.5           # accept() wakes up this often to notice stop()
PROGRESS_INTERVAL = 0.08    # throttle for file_progress events (seconds)


def describe_error(exc: BaseException) -> str:
    """Turn a low-level socket exception into a friendly sentence."""
    if isinstance(exc, ConnectionRefusedError):
        return "Connection refused (no peer is listening on that address and port)"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Connection timed out (peer unreachable or firewall blocking)"
    if isinstance(exc, socket.gaierror):
        return "Host not found"
    if isinstance(exc, protocol.ConnectionClosed):
        return "Connection closed by peer"
    return str(exc) or exc.__class__.__name__


class PeerConnection:
    """One live, handshaken TCP connection to a remote peer."""

    def __init__(self, sock: socket.socket, peer_id: str, name: str,
                 ip: str, port: int, direction: str) -> None:
        self.sock = sock
        self.peer_id = peer_id
        self.name = name
        self.ip = ip                    # remote IP address
        self.port = port                # remote *listening* port (from HELLO)
        self.direction = direction      # "incoming" or "outgoing"
        self.connected_at = time.time()
        self.outbox: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self.closed = False             # guarded by PeerNode._lock


class PeerNode:
    """A single peer: listens for peers, connects to peers, chats, sends files."""

    def __init__(
        self,
        name: str,
        port: int,
        downloads_dir: str,
        on_event: Optional[Callable[..., None]] = None,
        host: str = "0.0.0.0",
    ) -> None:
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        self.name = utils.validate_name(name)
        self.port = port
        self.host = host
        self.peer_id = protocol.generate_peer_id()
        self.downloads_dir = os.path.abspath(downloads_dir)
        self.on_event = on_event or (lambda event, **data: None)

        self._connections: Dict[str, PeerConnection] = {}
        self._lock = threading.RLock()
        self._server_socket: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    def get_connected_peers(self) -> List[dict]:
        """Snapshot of connected peers (safe to call from any thread)."""
        with self._lock:
            conns = sorted(self._connections.values(), key=lambda c: c.connected_at)
            return [{"peer_id": c.peer_id, "name": c.name, "ip": c.ip,
                     "port": c.port, "direction": c.direction} for c in conns]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """
        Server role, step by step (exactly the socket / bind / listen /
        accept recipe of the assignment).  Raises OSError if the port is
        busy or cannot be bound - the caller shows that to the user.
        """
        if self._running:
            raise RuntimeError("peer is already running")
        os.makedirs(self.downloads_dir, exist_ok=True)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)    # socket()
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):    # Windows: never share a port
                server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:                                         # POSIX: allow quick restart
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))                       # bind()
            server.listen(16)                                         # listen()
            server.settimeout(ACCEPT_POLL)
        except OSError:
            server.close()
            raise

        self.port = server.getsockname()[1]    # resolves port 0 -> real port
        self._server_socket = server
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, args=(server,),
                                               name="accept-loop", daemon=True)
        self._accept_thread.start()
        self._emit("started", peer_id=self.peer_id, name=self.name, port=self.port)

    def stop(self) -> None:
        """Stop listening and close every connection."""
        if not self._running:
            return
        self._running = False
        server, self._server_socket = self._server_socket, None
        if server is not None:
            # A thread blocked in accept() keeps the kernel socket (and the port)
            # alive even after close().  shutdown() wakes it immediately on
            # Linux/macOS; the join below covers Windows, so that Stop -> Start
            # on the same port always works.
            try:
                server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                server.close()
            except OSError:
                pass
        thread, self._accept_thread = self._accept_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=ACCEPT_POLL + 1.0)
        with self._lock:
            connections = list(self._connections.values())
        for conn in connections:
            self._drop(conn, notify=False)
        self._emit("stopped")

    # ------------------------------------------------------------------
    # Server role: accepting peers
    # ------------------------------------------------------------------
    def _accept_loop(self, server: socket.socket) -> None:
        while self._running:
            try:
                sock, addr = server.accept()                          # accept()
            except socket.timeout:
                continue
            except OSError:
                break                                                 # server closed
            threading.Thread(target=self._handle_incoming, args=(sock, addr),
                             name=f"incoming-{addr[0]}", daemon=True).start()

    def _handle_incoming(self, sock: socket.socket, addr) -> None:
        """Acceptor side of the HELLO handshake."""
        conn: Optional[PeerConnection] = None
        try:
            self._tune(sock)
            sock.settimeout(HANDSHAKE_TIMEOUT)
            hello = protocol.recv_message(sock)
            if hello.get("type") != protocol.MSG_HELLO:
                raise protocol.ProtocolError("expected a 'hello' message first")
            peer_id, name, port = protocol.parse_handshake(hello)

            if peer_id == self.peer_id:
                protocol.send_message(sock, protocol.make_error("A peer cannot connect to itself"))
                sock.close()
                return

            conn = PeerConnection(sock, peer_id, name, addr[0], port, "incoming")
            if not self._try_register(conn):
                protocol.send_message(sock, protocol.make_error("Already connected"))
                sock.close()
                return

            protocol.send_message(sock, protocol.make_hello_ack(self.peer_id, self.name, self.port))
            sock.settimeout(None)
            self._activate(conn)
        except (OSError, protocol.ProtocolError) as exc:
            if conn is not None:
                self._drop(conn, notify=False)
            else:
                try:
                    sock.close()
                except OSError:
                    pass
            self._emit("error", message=f"Rejected connection from {addr[0]}: {describe_error(exc)}")

    # ------------------------------------------------------------------
    # Client role: dialing peers
    # ------------------------------------------------------------------
    def connect_to_peer(self, ip: str, port: int) -> None:
        """Dial another peer.  Runs on its own thread so the GUI never freezes.
        Result is reported via ``peer_connected`` or ``connect_failed``."""
        if not self._running:
            self._emit("connect_failed", ip=ip, port=port,
                       message="Start your peer before connecting")
            return
        threading.Thread(target=self._connect_worker, args=(ip, port),
                         name=f"connect-{ip}:{port}", daemon=True).start()

    def _connect_worker(self, ip: str, port: int) -> None:
        sock: Optional[socket.socket] = None
        conn: Optional[PeerConnection] = None
        try:
            with self._lock:
                for existing in self._connections.values():
                    if existing.ip == ip and existing.port == port:
                        self._emit("connect_failed", ip=ip, port=port,
                                   message=f"Already connected to {existing.name}")
                        return
            self._emit("info", message=f"Connecting to {ip}:{port} ...")

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # socket()
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((ip, port))                                  # connect()
            self._tune(sock)

            protocol.send_message(sock, protocol.make_hello(self.peer_id, self.name, self.port))
            reply = protocol.recv_message(sock)
            if reply.get("type") == protocol.MSG_ERROR:
                raise protocol.ProtocolError(str(reply.get("reason", "peer refused the connection")))
            if reply.get("type") != protocol.MSG_HELLO_ACK:
                raise protocol.ProtocolError("expected 'hello_ack' from peer")
            peer_id, name, remote_port = protocol.parse_handshake(reply)
            if peer_id == self.peer_id:
                raise protocol.ProtocolError("A peer cannot connect to itself")

            conn = PeerConnection(sock, peer_id, name, ip, port, "outgoing")
            if not self._running or not self._try_register(conn):
                raise protocol.ProtocolError(f"Already connected to {name}")
            sock.settimeout(None)
            self._activate(conn)
        except (OSError, protocol.ProtocolError) as exc:
            if conn is not None:
                self._drop(conn, notify=False)
            elif sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self._emit("connect_failed", ip=ip, port=port,
                       message=f"Connection failed: {describe_error(exc)}")

    # ------------------------------------------------------------------
    # Connection bookkeeping
    # ------------------------------------------------------------------
    @staticmethod
    def _tune(sock: socket.socket) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def _try_register(self, conn: PeerConnection) -> bool:
        with self._lock:
            if not self._running or conn.peer_id in self._connections:
                return False
            self._connections[conn.peer_id] = conn
            return True

    def _activate(self, conn: PeerConnection) -> None:
        """Handshake done: start this connection's reader and writer threads."""
        threading.Thread(target=self._reader_loop, args=(conn,),
                         name=f"reader-{conn.name}", daemon=True).start()
        threading.Thread(target=self._writer_loop, args=(conn,),
                         name=f"writer-{conn.name}", daemon=True).start()
        self._emit("peer_connected", peer_id=conn.peer_id, name=conn.name,
                   ip=conn.ip, port=conn.port, direction=conn.direction)

    def _drop(self, conn: PeerConnection, reason: str = "", notify: bool = True) -> None:
        """Idempotently tear down a connection (safe from any thread)."""
        with self._lock:
            if conn.closed:
                return
            conn.closed = True
            if self._connections.get(conn.peer_id) is conn:
                del self._connections[conn.peer_id]
        conn.outbox.put(None)                       # wake + stop the writer
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)    # unblocks the reader's recv()
        except OSError:
            pass
        try:
            conn.sock.close()
        except OSError:
            pass
        if notify:
            self._emit("peer_disconnected", peer_id=conn.peer_id, name=conn.name,
                       reason=reason or "connection closed")

    def disconnect_peer(self, peer_id: str) -> None:
        """Close the connection to one peer (user-initiated)."""
        with self._lock:
            conn = self._connections.get(peer_id)
        if conn is not None:
            self._drop(conn, reason="you disconnected")

    def _emit(self, event: str, **data) -> None:
        try:
            self.on_event(event, **data)
        except Exception:                           # a GUI bug must never kill a network thread
            log.exception("on_event callback failed for %r", event)

    # ------------------------------------------------------------------
    # Receiving (one reader thread per connection)
    # ------------------------------------------------------------------
    def _reader_loop(self, conn: PeerConnection) -> None:
        reason = "connection closed by peer"
        try:
            while not conn.closed:
                message = protocol.recv_message(conn.sock)
                kind = message["type"]
                if kind == protocol.MSG_TEXT:
                    self._on_text(conn, message)
                elif kind == protocol.MSG_FILE:
                    self._receive_file(conn, message)
                else:
                    self._emit("error", peer_id=conn.peer_id,
                               message=f"Ignored unknown message type '{kind}' from {conn.name}")
        except protocol.ConnectionClosed:
            pass
        except protocol.ProtocolError as exc:
            reason = f"protocol error: {exc}"
            if not conn.closed:
                self._emit("error", peer_id=conn.peer_id,
                           message=f"Protocol error from {conn.name}: {exc}")
        except OSError as exc:
            reason = describe_error(exc)
        self._drop(conn, reason=reason)

    def _on_text(self, conn: PeerConnection, message: dict) -> None:
        text = message.get("message")
        if not isinstance(text, str):
            self._emit("error", peer_id=conn.peer_id,
                       message=f"Ignored malformed text message from {conn.name}")
            return
        self._emit("text_received", peer_id=conn.peer_id, name=conn.name,
                   text=text[: protocol.MAX_TEXT_LENGTH * 4], timestamp=time.time())

    def _receive_file(self, conn: PeerConnection, meta: dict) -> None:
        """
        Receive one file: the metadata is already parsed; the next
        ``filesize`` bytes on the socket ARE the file.  Written to
        ``<name>.part`` first, renamed when complete, so a half-received
        file is never mistaken for a finished one.
        """
        size = meta.get("filesize")
        raw_name = meta.get("filename")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise protocol.ProtocolError(f"invalid file size: {size!r}")
        if not isinstance(raw_name, str):
            raise protocol.ProtocolError("invalid file name")

        filename = utils.sanitize_filename(raw_name)
        destination = utils.unique_path(self.downloads_dir, filename)
        partial = destination + ".part"
        error: Optional[OSError] = None
        handle = None
        try:
            os.makedirs(self.downloads_dir, exist_ok=True)
            handle = open(partial, "wb")
        except OSError as exc:
            error = exc                      # we still must drain the bytes from the socket

        progress = self._progress_reporter("in", conn, filename, size)
        try:
            write_error = protocol.receive_stream(
                conn.sock, size, handle.write if handle else None, progress)
        except BaseException:
            if handle:
                handle.close()
                self._remove_quietly(partial)
            raise
        error = error or write_error

        if handle:
            try:
                handle.close()
            except OSError as exc:
                error = error or exc
        if error is None:
            try:
                os.replace(partial, destination)
            except OSError as exc:
                error = exc
        if error is not None:
            self._remove_quietly(partial)
            self._emit("error", peer_id=conn.peer_id,
                       message=f"Could not save '{filename}' from {conn.name}: {error}")
            return
        self._emit("file_received", peer_id=conn.peer_id, name=conn.name,
                   filename=os.path.basename(destination), path=destination,
                   filesize=size, timestamp=time.time())

    @staticmethod
    def _remove_quietly(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Sending (queued, executed by each connection's writer thread)
    # ------------------------------------------------------------------
    def send_text(self, peer_id: str, text: str) -> bool:
        """Queue a text message for one peer.  Returns False (and emits an
        error event) if it cannot be sent."""
        if not isinstance(text, str) or not text.strip():
            self._emit("error", message="Cannot send an empty message")
            return False
        if len(text) > protocol.MAX_TEXT_LENGTH:
            self._emit("error", message=f"Message too long (max {protocol.MAX_TEXT_LENGTH} characters)")
            return False
        conn = self._get_connection(peer_id)
        if conn is None:
            return False
        conn.outbox.put(("text", text))
        return True

    def send_file(self, peer_id: str, path: str) -> bool:
        """Queue a file for one peer.  Validates the path first."""
        if not path or not os.path.isfile(path):
            self._emit("error", message=f"File does not exist: {path}")
            return False
        if not os.access(path, os.R_OK):
            self._emit("error", message=f"File is not readable: {path}")
            return False
        conn = self._get_connection(peer_id)
        if conn is None:
            return False
        conn.outbox.put(("file", path))
        return True

    def _get_connection(self, peer_id: Optional[str]) -> Optional[PeerConnection]:
        with self._lock:
            conn = self._connections.get(peer_id) if peer_id else None
        if conn is None:
            self._emit("error", message="Cannot send: that peer is not connected")
        return conn

    def _writer_loop(self, conn: PeerConnection) -> None:
        while True:
            job = conn.outbox.get()
            if job is None:
                return
            try:
                if job[0] == "text":
                    protocol.send_message(conn.sock, protocol.make_text(self.peer_id, self.name, job[1]))
                    self._emit("text_sent", peer_id=conn.peer_id, name=conn.name,
                               text=job[1], timestamp=time.time())
                else:
                    self._transmit_file(conn, job[1])
            except (OSError, protocol.ProtocolError) as exc:
                if not conn.closed:
                    self._emit("error", peer_id=conn.peer_id,
                               message=f"Failed to send to {conn.name}: {describe_error(exc)}")
                self._drop(conn, reason=describe_error(exc))
                return

    def _transmit_file(self, conn: PeerConnection, path: str) -> None:
        """metadata first, then the raw bytes in 64 KiB chunks (never the whole file in RAM)."""
        try:
            handle = open(path, "rb")
        except OSError as exc:                       # connection is still healthy
            self._emit("error", peer_id=conn.peer_id, message=f"Cannot open '{path}': {exc}")
            return
        with handle:
            size = os.fstat(handle.fileno()).st_size
            filename = os.path.basename(path)
            protocol.send_message(conn.sock, protocol.make_file(self.peer_id, self.name, filename, size))

            progress = self._progress_reporter("out", conn, filename, size)
            sent = 0
            while sent < size:
                chunk = handle.read(min(protocol.FILE_CHUNK_SIZE, size - sent))
                if not chunk:                        # file shrank: stream is now unrecoverable
                    raise ConnectionError(f"'{filename}' changed while it was being sent")
                conn.sock.sendall(chunk)
                sent += len(chunk)
                progress(sent)
        self._emit("file_sent", peer_id=conn.peer_id, name=conn.name, filename=filename,
                   path=path, filesize=size, timestamp=time.time())

    def _progress_reporter(self, direction: str, conn: PeerConnection,
                           filename: str, total: int) -> Callable[[int], None]:
        """Return a function that emits throttled ``file_progress`` events."""
        state = {"last": 0.0}

        def report(done: int) -> None:
            now = time.monotonic()
            if done >= total or now - state["last"] >= PROGRESS_INTERVAL:
                state["last"] = now
                self._emit("file_progress", direction=direction, peer_id=conn.peer_id,
                           name=conn.name, filename=filename, done=done, total=total)
        if total == 0:
            report(0)
        return report
