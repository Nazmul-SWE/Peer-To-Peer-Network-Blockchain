"""Unit tests for protocol.py and utils.py  (run: python -m unittest discover -s tests)"""
import os
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import protocol
import utils


class FramingTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.a.settimeout(5)
        self.b.settimeout(5)

    def tearDown(self):
        self.a.close()
        self.b.close()

    def test_roundtrip(self):
        msg = protocol.make_text("id1", "Alice", "Hello Bob! বাংলা ✓")
        protocol.send_message(self.a, msg)
        self.assertEqual(protocol.recv_message(self.b), msg)

    def test_two_messages_sent_back_to_back_stay_separate(self):
        self.a.sendall(protocol.encode_message({"type": "text", "n": 1}) +
                       protocol.encode_message({"type": "text", "n": 2}))
        self.assertEqual(protocol.recv_message(self.b)["n"], 1)
        self.assertEqual(protocol.recv_message(self.b)["n"], 2)

    def test_message_split_into_single_bytes(self):
        data = protocol.encode_message({"type": "text", "message": "x" * 300})

        def dribble():
            for i in range(len(data)):
                self.a.sendall(data[i:i + 1])
        threading.Thread(target=dribble, daemon=True).start()
        self.assertEqual(protocol.recv_message(self.b)["message"], "x" * 300)

    def test_length_prefix_is_4_bytes_big_endian(self):
        raw = protocol.encode_message({"type": "x"})
        (length,) = struct.unpack(">I", raw[:4])
        self.assertEqual(length, len(raw) - 4)

    def test_oversized_length_rejected(self):
        self.a.sendall(struct.pack(">I", protocol.MAX_MESSAGE_SIZE + 1))
        with self.assertRaises(protocol.ProtocolError):
            protocol.recv_message(self.b)

    def test_zero_length_rejected(self):
        self.a.sendall(struct.pack(">I", 0))
        with self.assertRaises(protocol.ProtocolError):
            protocol.recv_message(self.b)

    def test_garbage_json_rejected(self):
        self.a.sendall(struct.pack(">I", 5) + b"{oops")
        with self.assertRaises(protocol.ProtocolError):
            protocol.recv_message(self.b)

    def test_json_without_type_rejected(self):
        self.a.sendall(protocol.encode_message({"hello": 1}))
        with self.assertRaises(protocol.ProtocolError):
            protocol.recv_message(self.b)

    def test_close_mid_message_raises_connection_closed(self):
        self.a.sendall(struct.pack(">I", 100) + b"abc")
        self.a.close()
        with self.assertRaises(protocol.ConnectionClosed):
            protocol.recv_message(self.b)

    def test_receive_stream_reads_exact_size_and_leaves_next_message(self):
        body = os.urandom(200_000)
        self.a.sendall(body + protocol.encode_message({"type": "text", "message": "after"}))
        out = bytearray()
        err = protocol.receive_stream(self.b, len(body), out.extend)
        self.assertIsNone(err)
        self.assertEqual(bytes(out), body)
        self.assertEqual(protocol.recv_message(self.b)["message"], "after")

    def test_receive_stream_drains_when_writer_fails(self):
        body = os.urandom(100_000)
        self.a.sendall(body + protocol.encode_message({"type": "text", "message": "ok"}))

        def bad_write(_):
            raise OSError("disk full")
        err = protocol.receive_stream(self.b, len(body), bad_write)
        self.assertIsInstance(err, OSError)
        self.assertEqual(protocol.recv_message(self.b)["message"], "ok")

    def test_receive_stream_truncated(self):
        self.a.sendall(b"12345")
        self.a.close()
        with self.assertRaises(protocol.ConnectionClosed):
            protocol.receive_stream(self.b, 100, lambda _: None)

    def test_parse_handshake(self):
        pid, name, port = protocol.parse_handshake(protocol.make_hello("abc", " Bob ", 5001))
        self.assertEqual((pid, name, port), ("abc", "Bob", 5001))
        for bad in ({"peer_id": "", "peer_name": "x", "port": 1},
                    {"peer_id": "a", "peer_name": "", "port": 1},
                    {"peer_id": "a", "peer_name": "x", "port": 0},
                    {"peer_id": "a", "peer_name": "x", "port": "5000"},
                    {"peer_id": "a", "peer_name": "x", "port": True}):
            with self.assertRaises(protocol.ProtocolError):
                protocol.parse_handshake(bad)


class UtilsTests(unittest.TestCase):
    def test_validate_port(self):
        self.assertEqual(utils.validate_port("5000"), 5000)
        for bad in ("", "abc", "0", "65536", "-1", "50 00", "5000.5", "²"):
            with self.assertRaises(utils.ValidationError):
                utils.validate_port(bad)
        with self.assertRaises(utils.ValidationError):
            utils.validate_port("80", minimum=1024)

    def test_validate_host(self):
        for ok in ("127.0.0.1", "192.168.1.20", "localhost", "my-pc.local"):
            self.assertEqual(utils.validate_host(ok), ok)
        for bad in ("", "999.1.1.1", "1.2.3", "1.2.3.4.5", "0.0.0.0", "bad host", "a..b", "-x.com", "::1"):
            with self.assertRaises(utils.ValidationError):
                utils.validate_host(bad)

    def test_validate_name(self):
        self.assertEqual(utils.validate_name("  Alice "), "Alice")
        for bad in ("", "   ", "x" * 25, "a\nb"):
            with self.assertRaises(utils.ValidationError):
                utils.validate_name(bad)

    def test_sanitize_filename(self):
        self.assertEqual(utils.sanitize_filename("photo.jpg"), "photo.jpg")
        self.assertEqual(utils.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(utils.sanitize_filename("..\\..\\win\\evil.exe"), "evil.exe")
        self.assertEqual(utils.sanitize_filename("a:b*c?.txt"), "a_b_c_.txt")
        self.assertEqual(utils.sanitize_filename(".."), "received_file")
        self.assertEqual(utils.sanitize_filename("CON.txt"), "_CON.txt")
        self.assertLessEqual(len(utils.sanitize_filename("x" * 400 + ".mp4")), 150)

    def test_unique_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            first = utils.unique_path(d, "a.txt")
            open(first, "w").close()
            second = utils.unique_path(d, "a.txt")
            self.assertTrue(second.endswith("a (1).txt"))

    def test_format_size(self):
        self.assertEqual(utils.format_size(0), "0 B")
        self.assertEqual(utils.format_size(1536), "1.5 KB")
        self.assertEqual(utils.format_size(5 * 1024 * 1024), "5.0 MB")


if __name__ == "__main__":
    unittest.main()
