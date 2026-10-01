"""
GUI smoke test + screenshot generator.

Opens three real PeerLink windows (Alice, Bob, Charlie) in one process, drives
them exactly like a user would (Start, Connect, Send, Send File) and verifies
the results.  Needs a display (on a headless Linux box:  xvfb-run -a -s '-screen 0 3400x760x24' python tests/gui_smoke.py).

    python tests/gui_smoke.py                 # run checks only
    python tests/gui_smoke.py --screenshots   # also write ./screenshots/*.png (needs Pillow)
"""
import hashlib
import math
import os
import struct
import sys
import tempfile
import time
import tkinter as tk
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gui import App  # noqa: E402

SHOTS = "--screenshots" in sys.argv


def pump(roots, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        for r in roots:
            r.update()
        time.sleep(0.01)


def wait(roots, cond, what, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        pump(roots, 0.05)
        if cond():
            return
    raise AssertionError("timed out: " + what)


def make_assets(folder):
    paths = {}
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (640, 360), "#7c5cff")
        d = ImageDraw.Draw(img)
        for i in range(0, 640, 40):
            d.ellipse((i, 120 + 60 * math.sin(i / 60), i + 90, 210 + 60 * math.sin(i / 60)), fill="#2ee6c5")
        paths["photo.png"] = os.path.join(folder, "photo.png")
        img.save(paths["photo.png"])
    except ImportError:
        paths["photo.bin"] = os.path.join(folder, "photo.bin")
        open(paths["photo.bin"], "wb").write(os.urandom(150_000))
    wav = os.path.join(folder, "song.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * t / 22050))) for t in range(22050 * 2)))
    paths["song.wav"] = wav
    video = os.path.join(folder, "lecture.mp4")
    open(video, "wb").write(os.urandom(6 * 1024 * 1024))
    paths["lecture.mp4"] = video
    notes = os.path.join(folder, "notes.txt")
    open(notes, "w", encoding="utf-8").write("CSE 433 - P2P lab notes\n" * 50)
    paths["notes.txt"] = notes
    return paths


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def shot(roots_by_name, name, filename):
    if not SHOTS:
        return
    from PIL import ImageGrab
    win = roots_by_name[name]
    pump(list(roots_by_name.values()), 0.4)
    x, y, w, h = win.winfo_rootx(), win.winfo_rooty(), win.winfo_width(), win.winfo_height()
    out = os.path.join(ROOT, "screenshots", filename)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    ImageGrab.grab(bbox=(x, y, x + w, y + h), xdisplay=os.environ.get("DISPLAY")).save(out)
    print("  screenshot ->", os.path.relpath(out, ROOT))


def main():
    tmp = tempfile.mkdtemp()
    assets = make_assets(tmp)
    names = ["Alice", "Bob", "Charlie"]
    ports = [5000, 5001, 5002]
    roots, apps = {}, {}
    for index, (name, port) in enumerate(zip(names, ports)):
        r = tk.Tk()
        r.geometry(f"1120x720+{index * 1130}+0")      # side by side: no overlap, no window manager needed
        apps[name] = App(r, downloads_dir=os.path.join(tmp, name + "_downloads"), default_name=name, default_port=port)
        roots[name] = r
    every = list(roots.values())
    A, B, C = apps["Alice"], apps["Bob"], apps["Charlie"]

    print("1. validation errors do not crash")
    A.name_field.entry.delete(0, "end"); A.start_peer()
    A.name_field.entry.insert(0, "Alice")
    A.port_field.entry.delete(0, "end"); A.port_field.entry.insert(0, "99999"); A.start_peer()
    assert A.node is None
    A.port_field.entry.delete(0, "end"); A.port_field.entry.insert(0, "5000")
    A.connect_clicked()                                      # connect before start
    A.send_text_clicked()                                    # send before start
    shot(roots, "Alice", "00_validation_error.png")

    print("2. start peers")
    for app in (A, B, C):
        app.start_peer()
        assert app.node and app.node.is_running
    pump(every)
    shot(roots, "Alice", "01_peer_started.png")

    print("3. port already in use")
    dup_root = tk.Tk()
    dup = App(dup_root, downloads_dir=os.path.join(tmp, "dup"), default_name="Dup", default_port=5000)
    dup.start_peer()
    assert dup.node is None and "already in use" in dup.status_label.cget("text")
    dup.on_close()

    print("4. Bob connects to Alice, connection errors are shown")
    B.ip_field.entry.delete(0, "end"); B.ip_field.entry.insert(0, "999.1.1.1"); B.connect_clicked()
    assert "valid IPv4" in B.status_label.cget("text")
    B.ip_field.entry.delete(0, "end"); B.ip_field.entry.insert(0, "127.0.0.1")
    B.rport_field.entry.delete(0, "end"); B.rport_field.entry.insert(0, "5999"); B.connect_clicked()
    wait(every, lambda: "refused" in B.status_label.cget("text").lower(), "connection refused message")
    B.rport_field.entry.delete(0, "end"); B.rport_field.entry.insert(0, "5000"); B.connect_clicked()
    wait(every, lambda: len(B.peers) == 1 and len(A.peers) == 1, "Alice<->Bob")

    print("5. text both ways; sending with no peer selected is an error")
    B.text_entry.insert(0, "Hello Alice!"); B.send_text_clicked()
    wait(every, lambda: any(e["kind"] == "text" and not e["mine"] for e in A.history[B.node.peer_id]), "Alice got text")
    A.text_entry.insert(0, "Hi Bob, বাংলা works too ✓"); A.send_text_clicked()
    wait(every, lambda: any(e["kind"] == "text" and not e["mine"] and "বাংলা" in e["text"] for e in B.history[A.node.peer_id]), "Bob got text")
    C.selected = None
    C.text_entry.insert(0, "x"); C.send_text_clicked()
    assert "Select a connected peer" in C.status_label.cget("text")
    C.text_entry.delete(0, "end")

    print("6. files: image, audio, video, text")
    send = lambda app, peer, path: (app.select_peer(peer.node.peer_id), app.send_file_path(path))
    send(A, B, assets.get("photo.png") or assets["photo.bin"])
    fname = "photo.png" if "photo.png" in assets else "photo.bin"
    wait(every, lambda: os.path.exists(os.path.join(B.downloads_dir, fname)), "image arrived", 20)
    send(B, A, assets["song.wav"])
    wait(every, lambda: os.path.exists(os.path.join(A.downloads_dir, "song.wav")), "audio arrived", 20)
    send(A, B, assets["lecture.mp4"])
    wait(every, lambda: os.path.exists(os.path.join(B.downloads_dir, "lecture.mp4")) and
         not any(f.endswith(".part") for f in os.listdir(B.downloads_dir)), "video arrived", 30)
    send(B, A, assets["notes.txt"])
    wait(every, lambda: os.path.exists(os.path.join(A.downloads_dir, "notes.txt")), "text file arrived", 20)
    assert sha(assets["lecture.mp4"]) == sha(os.path.join(B.downloads_dir, "lecture.mp4"))
    assert sha(assets["song.wav"]) == sha(os.path.join(A.downloads_dir, "song.wav"))
    A.select_peer(B.node.peer_id); B.select_peer(A.node.peer_id)
    pump(every, 0.3)

    print("7. Charlie joins the network (3 peers)")
    C.ip_field.entry.delete(0, "end"); C.ip_field.entry.insert(0, "127.0.0.1")
    C.rport_field.entry.delete(0, "end"); C.rport_field.entry.insert(0, "5000"); C.connect_clicked()
    wait(every, lambda: len(C.peers) == 1, "Charlie->Alice")
    C.rport_field.entry.delete(0, "end"); C.rport_field.entry.insert(0, "5001"); C.connect_clicked()
    wait(every, lambda: len(C.peers) == 2 and len(B.peers) == 2 and len(A.peers) == 2, "Charlie->Bob")
    C.select_peer(A.node.peer_id); C.text_entry.insert(0, "Charlie here - hi Alice!"); C.send_text_clicked()
    C.select_peer(B.node.peer_id); C.text_entry.insert(0, "Hello Bob, it's Charlie"); C.send_text_clicked()
    B.select_peer(C.node.peer_id); B.text_entry.insert(0, "Welcome Charlie!"); B.send_text_clicked()
    send(C, A, assets["notes.txt"])
    wait(every, lambda: os.path.exists(os.path.join(A.downloads_dir, "notes (1).txt")), "Charlie->Alice file (renamed, no overwrite)", 20)
    send(A, C, assets["song.wav"])
    wait(every, lambda: os.path.exists(os.path.join(C.downloads_dir, "song.wav")), "Alice->Charlie file", 20)
    A.select_peer(B.node.peer_id)
    C.select_peer(A.node.peer_id); B.select_peer(C.node.peer_id)
    pump(every, 0.5)
    shot(roots, "Alice", "02_alice.png")
    shot(roots, "Bob", "03_bob.png")
    shot(roots, "Charlie", "04_charlie.png")
    for app in (A, B, C):
        app._show_view("log")
    pump(every, 0.3)
    shot(roots, "Alice", "05_event_log.png")
    for app in (A, B, C):
        app._show_view("chat")

    print("8. a peer disconnects unexpectedly -> others keep working")
    B.stop_peer()
    wait(every, lambda: len(A.peers) == 1 and len(C.peers) == 1, "Bob gone")
    A.select_peer(C.node.peer_id); A.text_entry.insert(0, "still here"); A.send_text_clicked()
    wait(every, lambda: any(e.get("text") == "still here" for e in C.history[A.node.peer_id]), "A->C after B left")
    shot(roots, "Alice", "06_peer_disconnected.png")

    print("9. restart the stopped peer on the same port, then close everything")
    B.start_peer(); assert B.node.is_running
    for r in every:
        r.nametowidget(".").update()
    for app in (A, B, C):
        app.on_close()
    print("\nGUI SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
