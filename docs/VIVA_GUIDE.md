# Viva Guide - questions and answers

Short, accurate answers for the 20 questions in section 20 of the assignment, with the place in the code to point at.

1. **Client vs server?** A server waits (`listen`/`accept`) for connections; a client starts them (`connect`). Role depends on who initiates, not on the machine.
2. **Why can one peer be both?** It owns a listening socket (server role, `PeerNode.start` + `_accept_loop`) *and* creates outgoing sockets (client role, `connect_to_peer`). `Every peer = TCP Server + TCP Client`.
3. **Purpose of an IP address?** Identifies the computer/network interface (e.g. `192.168.1.10`) so packets reach the right machine.
4. **Purpose of a port?** Identifies the application on that machine (`192.168.1.10:5000`); many programs share one IP.
5. **Why different ports on one computer?** Only one socket may listen on an IP:port. Two peers on `127.0.0.1` need e.g. 5000 and 5001. (The app reports "Port already in use".)
6. **What does `connect()` do?** The client OS performs the TCP three-way handshake (SYN, SYN-ACK, ACK) with the server's IP:port; it returns once established, or raises (refused / timeout).
7. **What does `accept()` do?** Blocks until a completed connection is waiting in the listen queue, then returns a *new* socket for that peer plus its address; the listening socket keeps listening.
8. **Why TCP, not UDP?** TCP is reliable, ordered and detects disconnects - essential for files (a missing/reordered byte corrupts them). UDP would need us to rebuild all of that.
9. **Why threads?** `accept()` and `recv()` block. One thread per connection (plus the accept thread) lets a peer wait for many peers at once while the GUI stays responsive.
10. **Purpose of HELLO?** The handshake that tells each side the other's id, name and listening port before normal traffic - and lets us reject self/duplicate connections.
11. **What is message framing?** Marking where one message ends and the next begins in a byte stream. Here: `[4-byte length][JSON]`.
12. **Why is one `recv()` not one message?** TCP is a stream with no boundaries; `recv(n)` returns *up to* n bytes - messages can be merged or split. `recv_exact` loops until the exact count is read (`protocol.recv_exact`).
13. **Why metadata before the file?** The receiver must know the filename and exactly how many raw bytes follow, so it knows where the file ends and the next frame begins.
14. **Why chunks?** A 2 GB video must not be loaded into RAM. We read/send 64 KiB at a time (`FILE_CHUNK_SIZE`) - constant memory, and progress can be shown.
15. **How does the receiver know the file is complete?** It counts bytes: stops after exactly `filesize` bytes (`protocol.receive_stream`). It never relies on one `recv()`.
16. **What if the other peer disconnects?** `recv` returns 0 bytes -> `ConnectionClosed`; the reader thread cleans up (`_drop`), a half-written `.part` file is deleted, the GUI shows "X disconnected", other peers keep working.
17. **Where is the received file stored?** In the `downloads/` folder next to `main.py` (safe name, `photo (1).jpg` if it exists).
18. **Is there a central server?** No. Peers connect directly; each is its own server.
19. **What if a central server went offline (client-server)?** All clients lose the ability to communicate - a single point of failure. In P2P, remaining peers keep talking (shown by the "peer disconnects" test).
20. **Challenges of a large Internet-scale P2P system?** Peer discovery (DHT/bootstrap nodes), NAT/firewall traversal (STUN/TURN, hole punching), routing in partial meshes, churn (peers joining/leaving), security (authentication, encryption, malicious peers), file integrity (hashes) and resume/replication, fairness and scalability.

## Code map (what to open when asked "show me")

| Question topic | File / function |
|----------------|-----------------|
| socket / bind / listen / accept | `p2p_node.py` -> `PeerNode.start`, `_accept_loop` |
| socket / connect | `p2p_node.py` -> `_connect_worker` |
| handshake | `_handle_incoming` (acceptor), `_connect_worker` (initiator) |
| framing | `protocol.py` -> `encode_message`, `recv_message`, `recv_exact` |
| file send / receive | `_transmit_file`, `_receive_file`, `protocol.receive_stream` |
| threads | `_accept_loop`, `_reader_loop`, `_writer_loop` |
| error handling | `describe_error`, `_drop`, `utils.validate_*` |
| GUI <-> network bridge | `gui.py` -> `_poll_events`, `_handle_event` |

## Demo script (matches the checklist in section 19)

1. Start Peer A (Alice, 5000). 2. Start Peer B (Bob, 5001). 3. Bob connects to `127.0.0.1:5000`.
4. Show the connected peer list (name + id). 5-6. Text A->B and B->A. 7. Image A->B.
8. Audio B->A. 9. Video A->B. 10-12. Start Peer C (Charlie, 5002), connect to Alice and Bob,
and exchange text + a file between every pair. Finally close Bob and show that Alice and Charlie
keep working.
