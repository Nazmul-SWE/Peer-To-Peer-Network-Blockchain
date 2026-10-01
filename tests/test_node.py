"""Integration tests: real TCP connections between PeerNode objects on localhost."""
import hashlib
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import protocol
from p2p_node import PeerNode


class Recorder:
    """Thread-safe event collector with blocking wait helpers."""

    def __init__(self):
        self.events = []
        self.cond = threading.Condition()

    def __call__(self, event, **data):
        with self.cond:
            self.events.append((event, data))
            self.cond.notify_all()

    def wait_for(self, event, timeout=10, **match):
        deadline = time.time() + timeout
        with self.cond:
            while True:
                for ev, data in self.events:
                    if ev == event and all(data.get(k) == v for k, v in match.items()):
                        return data
                left = deadline - time.time()
                if left <= 0:
                    raise AssertionError(f"timed out waiting for {event} {match}; got {[e for e, _ in self.events]}")
                self.cond.wait(left)

    def count(self, event):
        with self.cond:
            return sum(1 for ev, _ in self.events if ev == event)


class NodeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.nodes = []

    def tearDown(self):
        for n, _ in self.nodes:
            n.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, name):
        rec = Recorder()
        node = PeerNode(name, 0, os.path.join(self.tmp, name), on_event=rec, host="127.0.0.1")
        node.start()
        self.nodes.append((node, rec))
        return node, rec

    def link(self, a, b):
        """b dials a; waits until both sides see the connection."""
        (na, ra), (nb, rb) = a, b
        nb.connect_to_peer("127.0.0.1", na.port)
        rb.wait_for("peer_connected", peer_id=na.peer_id)
        ra.wait_for("peer_connected", peer_id=nb.peer_id)


class HandshakeAndText(NodeTestCase):
    def test_handshake_exchanges_identity(self):
        a, b = self.make("Alice"), self.make("Bob")
        b[0].connect_to_peer("127.0.0.1", a[0].port)
        got_b = b[1].wait_for("peer_connected")
        got_a = a[1].wait_for("peer_connected")
        self.assertEqual((got_b["name"], got_b["peer_id"], got_b["port"]), ("Alice", a[0].peer_id, a[0].port))
        self.assertEqual((got_a["name"], got_a["peer_id"], got_a["port"]), ("Bob", b[0].peer_id, b[0].port))
        self.assertEqual(len(a[0].get_connected_peers()), 1)

    def test_text_both_directions_in_order(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        for i in range(50):
            self.assertTrue(b[0].send_text(a[0].peer_id, f"msg {i}"))
        self.assertTrue(a[0].send_text(b[0].peer_id, "Hello Bob! বাংলা"))
        a[1].wait_for("text_received", text="msg 49")
        texts = [d["text"] for e, d in a[1].events if e == "text_received"]
        self.assertEqual(texts, [f"msg {i}" for i in range(50)])
        got = b[1].wait_for("text_received")
        self.assertEqual((got["text"], got["name"]), ("Hello Bob! বাংলা", "Alice"))

    def test_empty_and_oversized_text_rejected(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        self.assertFalse(b[0].send_text(a[0].peer_id, "   "))
        self.assertFalse(b[0].send_text(a[0].peer_id, "x" * (protocol.MAX_TEXT_LENGTH + 1)))

    def test_send_to_unknown_peer(self):
        a, _ = self.make("Alice"), None
        self.assertFalse(a[0].send_text("nope", "hi"))
        self.assertFalse(a[0].send_text(None, "hi"))


class ConnectionErrors(NodeTestCase):
    def test_connection_refused(self):
        a = self.make("Alice")
        s = socket.socket(); s.bind(("127.0.0.1", 0)); free = s.getsockname()[1]; s.close()
        a[0].connect_to_peer("127.0.0.1", free)
        data = a[1].wait_for("connect_failed")
        self.assertIn("refused", data["message"].lower())

    def test_cannot_connect_to_self(self):
        a = self.make("Alice")
        a[0].connect_to_peer("127.0.0.1", a[0].port)
        self.assertIn("itself", a[1].wait_for("connect_failed")["message"])
        self.assertEqual(a[0].get_connected_peers(), [])

    def test_duplicate_connection_rejected(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        b[0].connect_to_peer("127.0.0.1", a[0].port)
        b[1].wait_for("connect_failed")
        self.assertEqual(len(a[0].get_connected_peers()), 1)
        self.assertEqual(len(b[0].get_connected_peers()), 1)

    def test_connect_before_start(self):
        rec = Recorder()
        node = PeerNode("X", 0, os.path.join(self.tmp, "x"), on_event=rec)
        node.connect_to_peer("127.0.0.1", 5000)
        rec.wait_for("connect_failed")

    def test_port_already_in_use(self):
        a = self.make("Alice")
        clash = PeerNode("Clash", a[0].port, os.path.join(self.tmp, "c"), host="127.0.0.1")
        with self.assertRaises(OSError):
            clash.start()

    def test_disconnect_is_reported_and_other_peer_survives(self):
        a, b, c = self.make("A"), self.make("B"), self.make("C")
        self.link(a, b); self.link(a, c)
        b[0].stop()
        a[1].wait_for("peer_disconnected", peer_id=b[0].peer_id)
        names = [p["name"] for p in a[0].get_connected_peers()]
        self.assertEqual(names, ["C"])
        self.assertTrue(a[0].send_text(c[0].peer_id, "still alive"))
        c[1].wait_for("text_received", text="still alive")
        self.assertFalse(a[0].send_text(b[0].peer_id, "gone"))

    def test_user_disconnect(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        a[0].disconnect_peer(b[0].peer_id)
        b[1].wait_for("peer_disconnected", peer_id=a[0].peer_id)
        self.assertEqual(a[0].get_connected_peers(), [])

    def test_restart_same_port_immediately_many_times(self):
        """Stop -> Start on the same port must work instantly (regression: accept()
        thread used to keep the old listening socket alive for up to 0.5 s)."""
        a = self.make("Alice")
        port = a[0].port
        a[0].stop()
        for i in range(10):
            again = PeerNode("Alice", port, os.path.join(self.tmp, f"again{i}"), host="127.0.0.1")
            again.start()
            self.assertEqual(again.port, port)
            again.stop()

    def test_restart_with_live_connections_same_port(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        port = a[0].port
        a[0].stop()
        again = PeerNode("Alice2", port, os.path.join(self.tmp, "again"), host="127.0.0.1")
        again.start()
        again.stop()

    def test_garbage_client_does_not_break_server(self):
        a, b = self.make("Alice"), self.make("Bob")
        junk = socket.create_connection(("127.0.0.1", a[0].port))
        junk.sendall(b"GET / HTTP/1.1\r\n\r\n")
        a[1].wait_for("error")
        junk.close()
        self.link(a, b)          # server still healthy

    def test_silent_client_does_not_block_server(self):
        a, b = self.make("Alice"), self.make("Bob")
        silent = socket.create_connection(("127.0.0.1", a[0].port))   # connects, says nothing
        self.link(a, b)
        silent.close()


class FileTransfer(NodeTestCase):
    def _send_and_check(self, sender, receiver, path, expected_name=None):
        sender[0].send_file(receiver[0].peer_id, path)
        got = receiver[1].wait_for("file_received", filename=expected_name or os.path.basename(path), timeout=30)
        sender[1].wait_for("file_sent", timeout=30)
        with open(path, "rb") as f1, open(got["path"], "rb") as f2:
            self.assertEqual(hashlib.sha256(f1.read()).hexdigest(), hashlib.sha256(f2.read()).hexdigest())
        return got

    def test_binary_files_identical_after_transfer(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        sizes = {"empty.txt": 0, "one.bin": 1, "note.txt": 2_000, "exact_chunk.bin": protocol.FILE_CHUNK_SIZE,
                 "chunk_plus1.bin": protocol.FILE_CHUNK_SIZE + 1, "video.mp4": 7 * 1024 * 1024 + 123}
        for name, size in sizes.items():
            path = os.path.join(self.tmp, name)
            with open(path, "wb") as f:
                f.write(os.urandom(size))
            got = self._send_and_check(a, b, path)
            self.assertEqual(got["filesize"], size)
        self.assertFalse([f for f in os.listdir(b[0].downloads_dir) if f.endswith(".part")])

    def test_text_and_files_interleaved_stay_in_sync(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        big = os.path.join(self.tmp, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(5 * 1024 * 1024))
        a[0].send_text(b[0].peer_id, "before")
        a[0].send_file(b[0].peer_id, big)
        a[0].send_text(b[0].peer_id, "middle")
        a[0].send_file(b[0].peer_id, big)
        a[0].send_text(b[0].peer_id, "after")
        b[1].wait_for("text_received", text="after", timeout=30)
        self.assertEqual([d["text"] for e, d in b[1].events if e == "text_received"], ["before", "middle", "after"])
        self.assertEqual(b[1].count("file_received"), 2)
        names = sorted(os.listdir(b[0].downloads_dir))
        self.assertEqual(names, ["big (1).bin", "big.bin"])        # no overwrite

    def test_progress_events(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        path = os.path.join(self.tmp, "p.bin")
        with open(path, "wb") as f:
            f.write(os.urandom(3 * 1024 * 1024))
        a[0].send_file(b[0].peer_id, path)
        b[1].wait_for("file_received", timeout=30)
        sent = [d for e, d in a[1].events if e == "file_progress"]
        recv = [d for e, d in b[1].events if e == "file_progress"]
        self.assertEqual(sent[-1]["done"], sent[-1]["total"])
        self.assertEqual(recv[-1]["done"], recv[-1]["total"])
        self.assertEqual(recv[0]["direction"], "in")
        self.assertEqual(sent[0]["direction"], "out")

    def test_missing_file_and_directory(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        self.assertFalse(a[0].send_file(b[0].peer_id, os.path.join(self.tmp, "nope.bin")))
        self.assertFalse(a[0].send_file(b[0].peer_id, self.tmp))
        self.assertFalse(a[0].send_file(b[0].peer_id, ""))
        self.assertTrue(a[0].send_text(b[0].peer_id, "connection still fine"))
        b[1].wait_for("text_received")

    def test_malicious_filename_cannot_escape_downloads(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        conn = a[0]._connections[b[0].peer_id]
        evil = b"data"
        protocol.send_message(conn.sock, protocol.make_file(a[0].peer_id, "Alice", "../../evil.txt", len(evil)))
        conn.sock.sendall(evil)
        got = b[1].wait_for("file_received")
        self.assertEqual(os.path.dirname(got["path"]), b[0].downloads_dir)
        self.assertEqual(got["filename"], "evil.txt")

    def test_invalid_file_size_drops_connection_only(self):
        a, b, c = self.make("Alice"), self.make("Bob"), self.make("Carol")
        self.link(a, b); self.link(a, c)
        conn = b[0]._connections[a[0].peer_id]
        protocol.send_message(conn.sock, {"type": "file", "filename": "x", "filesize": -5})
        a[1].wait_for("peer_disconnected", peer_id=b[0].peer_id)
        self.assertTrue(a[0].send_text(c[0].peer_id, "carol ok"))
        c[1].wait_for("text_received", text="carol ok")

    def test_disconnect_mid_transfer_cleans_partial_file(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        conn = a[0]._connections[b[0].peer_id]
        protocol.send_message(conn.sock, protocol.make_file(a[0].peer_id, "Alice", "half.bin", 1_000_000))
        conn.sock.sendall(b"x" * 1000)
        time.sleep(0.2)
        a[0].disconnect_peer(b[0].peer_id)
        b[1].wait_for("peer_disconnected")
        time.sleep(0.2)
        self.assertEqual(os.listdir(b[0].downloads_dir), [])        # no half-written leftovers

    def test_stop_during_big_transfer_does_not_crash(self):
        a, b = self.make("Alice"), self.make("Bob")
        self.link(a, b)
        path = os.path.join(self.tmp, "huge.bin")
        with open(path, "wb") as f:
            f.write(os.urandom(40 * 1024 * 1024))
        a[0].send_file(b[0].peer_id, path)
        b[1].wait_for("file_progress")
        b[0].stop()
        a[1].wait_for("peer_disconnected", timeout=15)


class MultiPeer(NodeTestCase):
    def test_three_peer_mesh_with_files_between_all_pairs(self):
        a, b, c = self.make("Alice"), self.make("Bob"), self.make("Charlie")
        self.link(a, b); self.link(b, c); self.link(c, a)
        for n, _ in (a, b, c):
            self.assertEqual(len(n.get_connected_peers()), 2)

        pairs = [(a, b), (b, c), (c, a), (b, a), (c, b), (a, c)]
        for i, (src, dst) in enumerate(pairs):
            path = os.path.join(self.tmp, f"f{i}.bin")
            with open(path, "wb") as f:
                f.write(os.urandom(200_000 + i))
            src[0].send_text(dst[0].peer_id, f"hi from {src[0].name}")
            src[0].send_file(dst[0].peer_id, path)
        for i, (src, dst) in enumerate(pairs):
            dst[1].wait_for("file_received", filename=f"f{i}.bin", timeout=20)
            dst[1].wait_for("text_received", name=src[0].name)

    def test_many_peers_connect_to_one_hub_concurrently(self):
        hub = self.make("Hub")
        spokes = [self.make(f"P{i}") for i in range(8)]
        for s in spokes:
            s[0].connect_to_peer("127.0.0.1", hub[0].port)
        for s in spokes:
            s[1].wait_for("peer_connected")
        self.assertEqual(len(hub[0].get_connected_peers()), 8)
        for s in spokes:
            s[0].send_text(hub[0].peer_id, "ping")
        deadline = time.time() + 10
        while hub[1].count("text_received") < 8 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(hub[1].count("text_received"), 8)


if __name__ == "__main__":
    unittest.main()
