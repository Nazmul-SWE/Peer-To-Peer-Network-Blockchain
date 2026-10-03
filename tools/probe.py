"""
probe.py - PeerLink network diagnostic (standard library only)

Use it when two computers will not connect.  It talks to the other PC *without*
the GUI and prints exactly what happened, so you can tell a firewall problem
from a wrong IP from a different/incompatible program.

    python tools/probe.py                          show this PC's addresses
    python tools/probe.py 192.168.1.20 5000        test a running PeerLink peer
    python tools/probe.py --listen 5000            plain listener: prints whatever connects

Typical use when the GUI says "Connection failed":
  1. On PC-A:  python tools/probe.py --listen 5000      (do NOT start PeerLink on that port)
  2. On PC-B:  python tools/probe.py <PC-A address> 5000
     -> "TCP connected" on both screens means the network/firewall is fine.
  3. Close the listener, start PeerLink on PC-A, run step 2 again.
     The probe prints the peer's raw reply to the HELLO message.
"""
import json
import socket
import struct
import sys
import time
import uuid


def local_addresses():
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in found:
                found.append(info[4][0])
    except OSError:
        pass
    return found


def recv_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError(f"peer closed the connection after {len(data)} of {count} bytes")
        data += chunk
    return data


def show_addresses():
    print("This computer's IPv4 addresses (give the one on your Wi-Fi/LAN to the other PC):")
    for address in local_addresses():
        note = ""
        if address.startswith("192.168.56."):
            note = "   <- VirtualBox host-only adapter, NOT your Wi-Fi"
        elif address.startswith("169.254."):
            note = "   <- link-local (no DHCP), not usable"
        elif address.startswith("127."):
            note = "   <- loopback, only works on this same PC"
        print("   ", address, note)


def listen(port):
    show_addresses()
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen()
    print(f"\nListening on 0.0.0.0:{port}  (Ctrl+C to stop)")
    while True:
        conn, addr = server.accept()
        print(f"[{time.strftime('%H:%M:%S')}] TCP connection from {addr[0]}:{addr[1]}")
        conn.settimeout(5)
        try:
            data = conn.recv(4096)
            print("    first bytes:", data[:200])
            conn.sendall(b"PROBE-OK\n")
        except OSError as exc:
            print("    ", exc)
        conn.close()


def probe(host, port):
    print(f"1) TCP connect to {host}:{port} ...")
    started = time.time()
    sock = socket.socket()
    sock.settimeout(6)
    try:
        sock.connect((host, port))
    except socket.timeout:
        print("   FAILED: timed out -> wrong IP, different Wi-Fi, router 'client isolation', or firewall dropping packets.")
        return 1
    except ConnectionRefusedError:
        print("   FAILED: connection refused -> the PC is reachable but nothing listens on that port "
              "(peer not started, or a different port).")
        return 1
    except OSError as exc:
        print("   FAILED:", exc)
        return 1
    print(f"   OK in {(time.time() - started) * 1000:.0f} ms  (local {sock.getsockname()} -> remote {sock.getpeername()})")

    print("2) sending a PeerLink 'hello' ...")
    hello = {"type": "hello", "version": 1, "peer_id": "probe" + uuid.uuid4().hex[:4],
             "peer_name": "Probe", "port": 59999}
    body = json.dumps(hello).encode()
    sock.sendall(struct.pack(">I", len(body)) + body)

    print("3) waiting for the reply ...")
    try:
        header = recv_exact(sock, 4)
        (length,) = struct.unpack(">I", header)
        print(f"   4-byte length header: {header.hex()}  -> {length} bytes")
        if length > 1_000_000:
            print("   That is not a PeerLink frame. First bytes as text:", header)
            print("   -> the port belongs to a DIFFERENT program (or a different protocol).")
            return 1
        payload = recv_exact(sock, length)
        print("   payload:", payload.decode("utf-8", "replace"))
        try:
            reply = json.loads(payload)
        except ValueError:
            print("   -> not JSON: different program/protocol.")
            return 1
        kind = str(reply.get("type"))
        if kind.strip().lower().replace("-", "_").replace(" ", "_") == "hello_ack":
            print("\nRESULT: the peer speaks PeerLink correctly. If the GUI still fails, send me this whole output.")
        elif kind == "error":
            print("\nRESULT: the peer refused:", reply.get("reason"))
        else:
            print(f"\nRESULT: the peer replied with type {kind!r}, not 'hello_ack'.")
            print("        -> that is exactly the GUI's \"expected 'hello_ack'\" error: the other PC is running a")
            print("           different version/program. Compare its protocol.py with this project's.")
        return 0
    except (OSError, ConnectionError) as exc:
        print("   FAILED:", exc)
        print("   -> the peer accepted the TCP connection but closed it without answering: wrong program on that port.")
        return 1
    finally:
        sock.close()


def main(argv):
    if len(argv) == 1:
        show_addresses()
        print(__doc__)
        return 0
    if argv[1] == "--listen":
        listen(int(argv[2]) if len(argv) > 2 else 5000)
        return 0
    if len(argv) < 3:
        print("usage: python tools/probe.py <ip> <port>")
        return 2
    return probe(argv[1], int(argv[2]))


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print()
