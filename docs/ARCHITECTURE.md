# PeerLink - Architecture

## 1. Layers

```
+---------------------------------------------------------+
|  gui.py        User interface (Tkinter, main thread)    |
+------------------------+--------------------------------+
        method calls     |     events via queue.Queue
+------------------------v--------------------------------+
|  p2p_node.py   PeerNode: server + client + threads      |
+------------------------+--------------------------------+
                         |  uses
+------------------------v--------------------------------+
|  protocol.py   framing, message types, stream receive   |
+------------------------+--------------------------------+
                         |
                   TCP sockets  ---->  other peers
```

* `gui.py` never imports `socket`. `p2p_node.py` never imports `tkinter`.
  That makes the network layer testable without a screen (see `tests/test_node.py`).
* The node reports everything through one callback `on_event(name, **data)`.
  The GUI's callback only does `queue.put(...)`; the Tk thread drains the queue every 40 ms,
  because Tkinter widgets must only be touched from the main thread.

## 2. Threading model

| Thread | Count | Job |
|--------|-------|-----|
| main (Tk) | 1 | draws the UI, drains the event queue |
| accept loop | 1 per node | `accept()` new peers (wakes every 0.5 s to notice `stop()`) |
| incoming handshake | short-lived, 1 per new connection | acceptor side of HELLO, 10 s timeout |
| connect worker | short-lived | dial out + initiator side of HELLO (GUI never freezes) |
| reader | 1 per connection | `recv_message()` loop: text, file |
| writer | 1 per connection | drains the connection's **outbox queue**, sends text / files |

**Why a writer thread + outbox queue?** Only one thread ever writes to a socket, so a text
message can never be interleaved inside the raw bytes of a file, messages keep their order, and a
slow peer cannot freeze the GUI.

## 3. Wire protocol

Every control message is a *frame*:

```
 0        4                      4+N
 +--------+----------------------+
 | length | UTF-8 JSON  (N bytes)|      length = big-endian unsigned 32-bit
 +--------+----------------------+
```

`MAX_MESSAGE_SIZE` is 1 MiB; a larger or zero length is a protocol error (prevents a hostile
peer from making us allocate gigabytes).

| type | direction | fields |
|------|-----------|--------|
| `hello` | initiator -> acceptor | `peer_id`, `peer_name`, `port`, `version` |
| `hello_ack` | acceptor -> initiator | `peer_id`, `peer_name`, `port`, `version` |
| `text` | both | `sender_id`, `sender_name`, `message` |
| `file` | both | `sender_id`, `sender_name`, `filename`, `filesize` - **then `filesize` raw bytes** |
| `error` | acceptor -> initiator | `reason` (handshake refused: self-connect / duplicate) |

### Handshake

```
 B: socket(); connect(A)            A: accept()  -> new thread
 B -> A  hello     {id_B, "Bob",   5001}
 A -> B  hello_ack {id_A, "Alice", 5000}
 both register the peer; both start a reader + writer thread
```

The `port` in the HELLO is the sender's **listening** port (not the ephemeral source port of the
TCP connection), which is why the peer list can show a meaningful `ip:port` on both sides.

### File transfer

```
 Alice                                                   Bob
   | frame {"type":"file","filename":"photo.jpg","filesize":2456789}
   |------------------------------------------------------>|  parse metadata
   | 65536 bytes                                           |
   | 65536 bytes ... (loop: read(64 KiB) / sendall)        |  recv_into loop until
   | last 2257 bytes                                       |  2 456 789 bytes received
   |------------------------------------------------------>|  rename photo.jpg.part -> photo.jpg
```

The receiver finishes when it has counted `filesize` bytes - never because one `recv()` returned.
It writes to `name.part` and renames on success, so a half-received file can never look complete.

## 4. Robustness decisions

| Decision | Reason |
|----------|--------|
| `recv_exact` / `receive_stream` loops | `recv(n)` may return fewer than `n` bytes |
| `sendall` instead of `send` | `send` may transmit only part of the data |
| `.part` file + atomic `os.replace` | no corrupt "finished" files after a crash/disconnect |
| `sanitize_filename` | blocks `../../x` path traversal, illegal characters, Windows reserved names |
| `unique_path` | `photo (1).jpg` instead of overwriting |
| Disk-full while receiving | the stream is still drained so the connection stays in sync; error reported |
| Invalid size / malformed JSON | only that connection is dropped; others unaffected |
| Handshake timeout (10 s) | a silent or garbage client cannot block or crash the peer |
| `SO_EXCLUSIVEADDRUSE` on Windows, `SO_REUSEADDR` elsewhere | Windows would otherwise let two peers share a port silently; POSIX gets fast restart |
| `TCP_NODELAY`, `SO_KEEPALIVE` | low latency chat; dead peers are eventually detected |
| Generation counter in the GUI | events from a stopped node can never leak into a new session |
| Duplicate / self connection rejected by `peer_id` | avoids ghost double entries |
| Simultaneous connect tie-break | if both peers click Connect at once there are briefly two TCP connections; both sides keep the one dialled by the smaller `peer_id`, the loser closes silently |
| 0.6 s disconnect grace for brand-new connections | the losing duplicate must not flash "peer disconnected" |
| Liberal handshake parsing (`Hello-Ack`, `hello ack`, port as string, missing port) | interoperates with other students' implementations; unexpected replies produce an error naming the received type |

## 5. Not implemented (explicitly out of scope per section 15 of the assignment)

Blockchain, consensus, DHT, NAT traversal, encryption, authentication, routing between peers
(messages go only to the selected, directly connected peer), chunk recovery / resume.
