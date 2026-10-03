"""
gui.py - PeerLink user interface (standard light theme, responsive)
===================================================================

This file contains ONLY presentation code.  It never touches a socket: it
calls methods on a ``PeerNode`` (start / stop / connect_to_peer / send_text /
send_file) and renders the events that come back.

Design
------
A conventional, familiar chat layout: white top bar, a sidebar with the
peer controls and the peer list, and a conversation area with the message
box at the bottom.  Neutral greys plus ONE accent (standard blue); green
means connected / start, red means disconnect / error.

Responsive behaviour
--------------------
The window can be resized freely (minimum 600 x 600).

* Wide window (>= 860 px): sidebar and conversation sit side by side.  The
  sidebar width follows the window (270 - 360 px).
* Narrow window (< 860 px): "single panel" mode, like a phone app.  Either
  the sidebar (peer list) or the conversation is shown.  Choosing a peer
  opens its conversation and a "Back" button returns to the peer list.
* Chat bubbles re-wrap to about 2/3 of the conversation width.
* Secondary text (tagline, hint line, downloads path) hides when space is
  short, and long button labels shorten.

Thread safety
-------------
PeerNode fires events from background threads, but Tkinter widgets may only
be touched from the main thread.  The node's callback therefore just puts
``(generation, event, data)`` on a ``queue.Queue``; ``_poll_events`` drains
that queue every 40 ms on the Tk thread.  The ``generation`` number makes the
GUI ignore stale events from a node that has already been stopped.

Layout (wide)
-------------
    +--------------------------------------------------------------+
    |  top bar: logo . title . status pill                         |
    +-------------------+------------------------------------------+
    |  My peer          |  selected peer header      [Disconnect]  |
    |  Connect          |  Conversation | Event log                |
    |  Connected peers  |  ...bubbles / log...                     |
    |                   |  [ message ........ ] [Send] [Send file] |
    +-------------------+------------------------------------------+
    |  status bar                                                  |
    +--------------------------------------------------------------+
"""

from __future__ import annotations

import errno
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, ttk
from typing import Callable, Dict, List, Optional

import protocol
import utils
from p2p_node import PeerNode

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
POLL_MS = 40
MAX_CHAT_RENDER = 300          # newest messages drawn per conversation
MAX_LOG_LINES = 2000
BUBBLE_WRAP = 430              # default bubble width in pixels (adapts on resize)

COMPACT_W = 860                # below this width: single-panel mode
WIDE_TEXT_W = 960              # tagline visible from this width
PATH_TEXT_W = 900              # downloads path in status bar visible from this width


# --------------------------------------------------------------------------
# Palette - standard light UI: neutral greys + one blue accent
# --------------------------------------------------------------------------
class C:
    BG = "#f0f2f5"             # window background
    SURFACE = "#ffffff"        # cards, bars
    SURFACE_2 = "#f6f8fa"      # inputs, subtle fills
    BORDER = "#d8dee4"
    TEXT = "#1f2328"
    MUTED = "#59636e"
    DIM = "#8c959f"
    PRIMARY = "#0b6bcb"
    PRIMARY_HOVER = "#0958a8"
    PRIMARY_SOFT = "#e6f1fc"
    LOGO_2 = "#58a6ff"
    SUCCESS = "#1a7f37"
    SUCCESS_HOVER = "#156d2e"
    SUCCESS_DOT = "#2da44e"
    WARN = "#9a6700"
    WARN_DOT = "#d4a72c"
    DANGER = "#cf222e"
    DANGER_BG = "#ffebe9"
    DANGER_HOVER = "#ffd8d3"
    DISABLED_BG = "#e6e9ed"
    DISABLED_FG = "#9aa4b1"
    CHAT_BG = "#f5f7fa"
    BUBBLE_ME = "#0b6bcb"
    BUBBLE_THEM = "#e8ebef"
    BADGE = "#cf222e"
    AVATARS = ["#0b6bcb", "#1a7f37", "#bf5f00", "#a5399f", "#0e7c86", "#6f42c1"]


# file-type tiles: (extensions, background, foreground)
_FILE_KINDS = (
    ({"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"}, "#ddf4ff", "#0550ae"),
    ({"mp3", "wav", "flac", "ogg", "m4a", "aac"}, "#fbefff", "#6f2fa8"),
    ({"mp4", "mkv", "avi", "mov", "webm"}, "#ffebe9", "#a0111f"),
    ({"pdf", "doc", "docx", "txt", "md", "xls", "xlsx", "ppt", "pptx", "csv"}, "#dafbe1", "#116329"),
    ({"zip", "rar", "7z", "tar", "gz"}, "#fff8c5", "#7d4e00"),
)


def file_badge(filename: str):
    """Return (label, bg, fg) for the file-type tile."""
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    label = (ext.upper()[:4]) or "FILE"
    for group, bg, fg in _FILE_KINDS:
        if ext in group:
            return label, bg, fg
    return label, "#eaeef2", "#424a53"


def pick_font(root: tk.Misc, candidates, fallback: str) -> str:
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return fallback


def set_visible(widget: tk.Misc, visible: bool, **pack_opts) -> None:
    """Show/hide a packed widget without re-packing it every time."""
    shown = bool(widget.winfo_manager())
    if visible and not shown:
        widget.pack(**pack_opts)
    elif not visible and shown:
        widget.pack_forget()


# --------------------------------------------------------------------------
# Small custom widgets (plain tk so colours look identical on every OS)
# --------------------------------------------------------------------------
class FlatButton(tk.Label):
    STYLES = {
        "primary": (C.PRIMARY, C.PRIMARY_HOVER, "#ffffff"),
        "success": (C.SUCCESS, C.SUCCESS_HOVER, "#ffffff"),
        "danger": (C.DANGER_BG, C.DANGER_HOVER, C.DANGER),
        "ghost": (C.SURFACE_2, C.BORDER, C.TEXT),
    }

    def __init__(self, parent, text: str, command: Callable[[], None], kind: str = "primary",
                 font=None, padx: int = 16, pady: int = 8) -> None:
        self._bg, self._hover, self._fg = self.STYLES[kind]
        super().__init__(parent, text=text, bg=self._bg, fg=self._fg, font=font,
                         padx=padx, pady=pady, cursor="hand2")
        self._command = command
        self._enabled = True
        self.bind("<Enter>", lambda _e: self._enabled and self.configure(bg=self._hover))
        self.bind("<Leave>", lambda _e: self._enabled and self.configure(bg=self._bg))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_release(self, event) -> None:
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        if self._enabled and inside:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if enabled:
            self.configure(bg=self._bg, fg=self._fg, cursor="hand2")
        else:
            self.configure(bg=C.DISABLED_BG, fg=C.DISABLED_FG, cursor="arrow")

    def set_text(self, text: str) -> None:
        self.configure(text=text)


class Field(tk.Frame):
    """Caption above a standard text input with a blue focus ring."""

    def __init__(self, parent, caption: str, value: str, font, small_font, width: int = 10) -> None:
        super().__init__(parent, bg=C.SURFACE)
        tk.Label(self, text=caption, bg=C.SURFACE, fg=C.MUTED, font=small_font).pack(anchor="w")
        self.entry = tk.Entry(
            self, font=font, width=width, bg=C.SURFACE, fg=C.TEXT, insertbackground=C.TEXT,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=C.BORDER,
            highlightcolor=C.PRIMARY, disabledbackground=C.SURFACE_2, disabledforeground=C.DIM)
        self.entry.pack(fill="x", ipady=6, pady=(3, 0))
        self.entry.insert(0, value)

    def get(self) -> str:
        return self.entry.get()

    def set_enabled(self, enabled: bool) -> None:
        self.entry.configure(state="normal" if enabled else "disabled")


class Card(tk.Frame):
    def __init__(self, parent, title: str, small_bold) -> None:
        super().__init__(parent, bg=C.SURFACE, highlightthickness=1, highlightbackground=C.BORDER)
        self.title_label = tk.Label(self, text=title, bg=C.SURFACE, fg=C.TEXT, font=small_bold)
        self.title_label.pack(anchor="w", padx=14, pady=(12, 6))
        self.body = tk.Frame(self, bg=C.SURFACE)
        self.body.pack(fill="x", padx=14, pady=(0, 14))


class Avatar(tk.Canvas):
    def __init__(self, parent, text: str, color: str, size: int = 38, bg: str = C.SURFACE, font=None) -> None:
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self.create_oval(1, 1, size - 1, size - 1, fill=color, outline="")
        self.create_text(size // 2, size // 2, text=(text[:1] or "?").upper(), fill="#ffffff", font=font)


class ProgressBar(tk.Canvas):
    def __init__(self, parent) -> None:
        super().__init__(parent, height=6, bg=C.BORDER, highlightthickness=0, bd=0)
        self._fraction = 0.0
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, fraction: float) -> None:
        self._fraction = max(0.0, min(1.0, fraction))
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        self.create_rectangle(0, 0, int(width * self._fraction), 6, fill=C.PRIMARY, outline="")


class ScrollFrame(tk.Frame):
    """A vertically scrollable frame (used for the peer list)."""

    def __init__(self, parent, bg: str = C.SURFACE) -> None:
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style="Std.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        for widget in (self.canvas, self.inner):
            self.bind_wheel(widget)

    def _on_inner(self, _event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needed = self.inner.winfo_reqheight() > self.canvas.winfo_height()
        if needed and not self.scrollbar.winfo_ismapped():
            self.scrollbar.pack(side="right", fill="y")
        elif not needed and self.scrollbar.winfo_ismapped():
            self.scrollbar.pack_forget()

    def bind_wheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._wheel, add="+")
        widget.bind("<Button-4>", lambda _e: self._scroll(-1), add="+")
        widget.bind("<Button-5>", lambda _e: self._scroll(1), add="+")

    def _wheel(self, event) -> None:
        self._scroll(-1 if event.delta > 0 else 1)

    def _scroll(self, direction: int) -> None:
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(direction, "units")


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------
class App:
    """Main window.  Public methods (start_peer, connect_clicked, ...) are
    also what the automated GUI smoke test drives."""

    def __init__(self, root: tk.Tk, downloads_dir: str = DOWNLOADS_DIR,
                 default_name: Optional[str] = None, default_port: int = 5000) -> None:
        self.root = root
        self.downloads_dir = downloads_dir
        root.title("PeerLink - P2P Network  |  CSE 433")
        root.geometry("1120x720")
        root.minsize(600, 600)
        root.configure(bg=C.BG)

        base = pick_font(root, ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Noto Sans", "DejaVu Sans", "Arial"), "TkDefaultFont")
        mono = pick_font(root, ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New"), "TkFixedFont")
        self.f_body = (base, 10)
        self.f_bold = (base, 10, "bold")
        self.f_small = (base, 9)
        self.f_small_bold = (base, 9, "bold")
        self.f_title = (base, 16, "bold")
        self.f_head = (base, 12, "bold")
        self.f_mono = (mono, 9)
        self.f_mono_bold = (mono, 9, "bold")

        self._setup_ttk_style()

        # --- state -----------------------------------------------------
        self.node: Optional[PeerNode] = None
        self._generation = 0
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.peers: Dict[str, dict] = {}
        self.peer_order: List[str] = []
        self.history: Dict[str, List[dict]] = {}
        self.unread: Dict[str, int] = {}
        self.selected: Optional[str] = None
        self.view = "chat"
        self._closing = False
        self._transfer: Optional[dict] = None
        self._toast_job: Optional[str] = None
        self._color_counter = 0
        self.local_ips = utils.get_local_ips()
        self.local_ip = self.local_ips[0]

        # --- responsive state --------------------------------------------
        self.compact = False                 # single-panel mode
        self.panel = "chat"                  # which panel shows in compact mode
        self._bubble_wrap = BUBBLE_WRAP
        self._resize_job: Optional[str] = None

        default_name = default_name or (os.environ.get("USERNAME") or os.environ.get("USER") or "Peer")[:24]

        self._build_header()
        self._build_statusbar()
        self.body = tk.Frame(root, bg=C.BG)
        self.body.pack(fill="both", expand=True, padx=12, pady=12)
        self._build_sidebar(self.body, default_name, default_port)
        self._build_main(self.body)
        self._arrange()

        self._refresh_peer_list()
        self._refresh_peer_header()
        self._render_chat()
        self._show_view("chat")
        self._set_online(False)
        self._log("INFO", "PeerLink ready. This computer's addresses: " + ", ".join(self.local_ips)
                  + ". Choose a name and port, then press Start Peer.")

        root.bind_all("<Control-o>", lambda _e: self.choose_file_and_send())
        root.bind("<Configure>", self._on_root_configure)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_job = root.after(POLL_MS, self._poll_events)
        root.after(120, self._layout)

    # ==================================================================
    # Construction
    # ==================================================================
    def _setup_ttk_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Std.Vertical.TScrollbar", background=C.BORDER, troughcolor=C.SURFACE,
                        bordercolor=C.SURFACE, lightcolor=C.BORDER, darkcolor=C.BORDER,
                        arrowcolor=C.MUTED, relief="flat", gripcount=0)
        style.map("Std.Vertical.TScrollbar", background=[("active", C.DIM)])
        style.configure("Chat.Vertical.TScrollbar", background=C.BORDER, troughcolor=C.CHAT_BG,
                        bordercolor=C.CHAT_BG, lightcolor=C.BORDER, darkcolor=C.BORDER,
                        arrowcolor=C.MUTED, relief="flat", gripcount=0)
        style.map("Chat.Vertical.TScrollbar", background=[("active", C.DIM)])

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=C.SURFACE)
        header.pack(fill="x")
        inner = tk.Frame(header, bg=C.SURFACE)
        inner.pack(fill="x", padx=16, pady=10)

        logo = tk.Canvas(inner, width=50, height=34, bg=C.SURFACE, highlightthickness=0)
        logo.create_oval(3, 4, 29, 30, outline=C.PRIMARY, width=4)
        logo.create_oval(20, 4, 46, 30, outline=C.LOGO_2, width=4)
        logo.create_arc(20, 4, 46, 30, start=100, extent=160, style="arc", outline=C.PRIMARY, width=4)
        logo.pack(side="left")
        titles = tk.Frame(inner, bg=C.SURFACE)
        titles.pack(side="left", padx=(10, 0))
        tk.Label(titles, text="PeerLink", font=self.f_title, bg=C.SURFACE, fg=C.TEXT).pack(anchor="w")
        self.tagline = tk.Label(titles, text="Serverless P2P chat & file sharing  -  every peer is a server and a client",
                                font=self.f_small, bg=C.SURFACE, fg=C.MUTED)
        self.tagline.pack(anchor="w")

        right = tk.Frame(inner, bg=C.SURFACE)
        right.pack(side="right")
        self.pill = tk.Frame(right, bg=C.SURFACE_2, highlightthickness=1, highlightbackground=C.BORDER)
        self.pill.pack(side="right")
        self.pill_dot = tk.Canvas(self.pill, width=12, height=12, bg=C.SURFACE_2, highlightthickness=0)
        self.pill_dot.pack(side="left", padx=(12, 6), pady=8)
        self.pill_label = tk.Label(self.pill, text="Offline", font=self.f_small_bold, bg=C.SURFACE_2, fg=C.MUTED)
        self.pill_label.pack(side="left", padx=(0, 14))
        tk.Frame(self.root, bg=C.BORDER, height=1).pack(fill="x")

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=C.SURFACE, highlightthickness=1, highlightbackground=C.BORDER)
        bar.pack(side="bottom", fill="x")
        self.status_dot = tk.Canvas(bar, width=10, height=10, bg=C.SURFACE, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(14, 6), pady=7)
        self.status_label = tk.Label(bar, text="Ready", font=self.f_small, bg=C.SURFACE, fg=C.MUTED, anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.status_path = tk.Label(bar, text="Downloads: " + self.downloads_dir, font=self.f_small,
                                    bg=C.SURFACE, fg=C.DIM)
        self.status_path.pack(side="right", padx=14)
        self._toast("Ready", "info")

    def _build_sidebar(self, parent: tk.Frame, default_name: str, default_port: int) -> None:
        side = tk.Frame(parent, bg=C.BG, width=330)
        side.pack_propagate(False)
        self.side = side                      # packed by _arrange()

        # ---- My Peer -------------------------------------------------
        card = Card(side, "My peer", self.f_bold)
        card.pack(fill="x")
        row = tk.Frame(card.body, bg=C.SURFACE)
        row.pack(fill="x")
        self.name_field = Field(row, "Peer name", default_name, self.f_body, self.f_small, width=14)
        self.name_field.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.port_field = Field(row, "Listen port", str(default_port), self.f_body, self.f_small, width=7)
        self.port_field.pack(side="left")
        buttons = tk.Frame(card.body, bg=C.SURFACE)
        buttons.pack(fill="x", pady=(12, 0))
        self.start_btn = FlatButton(buttons, "Start Peer", self.start_peer, "success", self.f_bold)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.stop_btn = FlatButton(buttons, "Stop", self.stop_peer, "danger", self.f_bold)
        self.stop_btn.pack(side="left", fill="x", expand=True)
        self.stop_btn.set_enabled(False)
        self.identity_label = tk.Label(card.body, text="Not running", font=self.f_small, bg=C.SURFACE,
                                       fg=C.MUTED, anchor="w", justify="left", cursor="arrow", wraplength=290)
        self.identity_label.pack(fill="x", pady=(10, 0))
        self.identity_label.bind("<Button-1>", self._copy_address)

        # ---- Connect -------------------------------------------------
        card = Card(side, "Connect to another peer", self.f_bold)
        card.pack(fill="x", pady=(10, 0))
        row = tk.Frame(card.body, bg=C.SURFACE)
        row.pack(fill="x")
        self.ip_field = Field(row, "Remote IP", "127.0.0.1", self.f_body, self.f_small, width=14)
        self.ip_field.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.rport_field = Field(row, "Remote port", "5001", self.f_body, self.f_small, width=7)
        self.rport_field.pack(side="left")
        self.connect_btn = FlatButton(card.body, "Connect", self.connect_clicked, "primary", self.f_bold)
        self.connect_btn.pack(fill="x", pady=(12, 0))
        self.connect_btn.set_enabled(False)
        for field in (self.ip_field, self.rport_field):
            field.entry.bind("<Return>", lambda _e: self.connect_clicked())

        # ---- Peer list -----------------------------------------------
        card = tk.Frame(side, bg=C.SURFACE, highlightthickness=1, highlightbackground=C.BORDER)
        card.pack(fill="both", expand=True, pady=(10, 0))
        top = tk.Frame(card, bg=C.SURFACE)
        top.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(top, text="Connected peers", bg=C.SURFACE, fg=C.TEXT, font=self.f_bold).pack(side="left")
        self.count_label = tk.Label(top, text="0", bg=C.PRIMARY_SOFT, fg=C.PRIMARY, font=self.f_small_bold, padx=8, pady=1)
        self.count_label.pack(side="right")
        self.peer_scroll = ScrollFrame(card)
        self.peer_scroll.pack(fill="both", expand=True, padx=6, pady=(0, 8))

    def _build_main(self, parent: tk.Frame) -> None:
        main = tk.Frame(parent, bg=C.SURFACE, highlightthickness=1, highlightbackground=C.BORDER)
        self.main = main                      # packed by _arrange()

        # ---- selected-peer header -----------------------------------
        self.peer_header = tk.Frame(main, bg=C.SURFACE)
        self.peer_header.pack(fill="x", padx=16, pady=(12, 0))
        self.back_btn = FlatButton(self.peer_header, "< Peers", lambda: self._goto("peers"), "ghost",
                                   self.f_small_bold, padx=10, pady=6)          # shown in compact mode only
        self.header_avatar_slot = tk.Frame(self.peer_header, bg=C.SURFACE)
        self.header_avatar_slot.pack(side="left")
        text_col = tk.Frame(self.peer_header, bg=C.SURFACE)
        text_col.pack(side="left", padx=(10, 0), fill="x", expand=True)
        self.header_title = tk.Label(text_col, text="", font=self.f_head, bg=C.SURFACE, fg=C.TEXT, anchor="w")
        self.header_title.pack(anchor="w")
        self.header_sub = tk.Label(text_col, text="", font=self.f_small, bg=C.SURFACE, fg=C.MUTED, anchor="w",
                                   justify="left", wraplength=420)
        self.header_sub.pack(anchor="w")
        actions = tk.Frame(self.peer_header, bg=C.SURFACE)
        actions.pack(side="right")
        self.open_btn = FlatButton(actions, "Open Downloads", self.open_downloads, "ghost", self.f_small_bold, padx=12, pady=6)
        self.open_btn.pack(side="left", padx=(0, 6))
        self.disconnect_btn = FlatButton(actions, "Disconnect", self.disconnect_selected, "danger", self.f_small_bold, padx=12, pady=6)
        self.disconnect_btn.pack(side="left")

        # ---- tabs ----------------------------------------------------
        tabs = tk.Frame(main, bg=C.SURFACE)
        tabs.pack(fill="x", padx=16, pady=(10, 0))
        self.tab_widgets: Dict[str, tuple] = {}
        for key, label in (("chat", "Conversation"), ("log", "Event Log")):
            holder = tk.Frame(tabs, bg=C.SURFACE, cursor="hand2")
            holder.pack(side="left", padx=(0, 22))
            text = tk.Label(holder, text=label, font=self.f_bold, bg=C.SURFACE, fg=C.MUTED, cursor="hand2")
            text.pack()
            underline = tk.Frame(holder, bg=C.SURFACE, height=2)
            underline.pack(fill="x", pady=(5, 0))
            for widget in (holder, text, underline):
                widget.bind("<Button-1>", lambda _e, k=key: self._show_view(k))
            self.tab_widgets[key] = (text, underline)
        tk.Frame(main, bg=C.BORDER, height=1).pack(fill="x")

        # ---- composer (packed first at the bottom) -------------------
        composer = tk.Frame(main, bg=C.SURFACE)
        composer.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        self.xfer_frame = tk.Frame(composer, bg=C.SURFACE)
        self.xfer_label = tk.Label(self.xfer_frame, text="", font=self.f_small, bg=C.SURFACE, fg=C.PRIMARY, anchor="w")
        self.xfer_label.pack(fill="x")
        self.xfer_bar = ProgressBar(self.xfer_frame)
        self.xfer_bar.pack(fill="x", pady=(4, 8))
        row = tk.Frame(composer, bg=C.SURFACE)
        row.pack(fill="x")
        self.composer_row = row
        self.text_entry = tk.Entry(
            row, font=(self.f_body[0], 11), bg=C.SURFACE, fg=C.TEXT, insertbackground=C.TEXT,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=C.BORDER, highlightcolor=C.PRIMARY)
        self.text_entry.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 8))
        self.text_entry.bind("<Return>", lambda _e: self.send_text_clicked())
        self.send_btn = FlatButton(row, "Send", self.send_text_clicked, "primary", self.f_bold, padx=22, pady=9)
        self.send_btn.pack(side="left", padx=(0, 8))
        self.attach_btn = FlatButton(row, "Choose File & Send", self.choose_file_and_send, "success", self.f_bold, padx=16, pady=9)
        self.attach_btn.pack(side="left")
        self.hint_label = tk.Label(composer, text="Text, image, audio, video, PDF, ZIP - any file is sent as raw bytes  (Ctrl+O)",
                                   font=self.f_small, bg=C.SURFACE, fg=C.DIM)
        self.hint_label.pack(anchor="w", pady=(6, 0))

        # ---- content area -------------------------------------------
        self.content = tk.Frame(main, bg=C.SURFACE)
        self.content.pack(fill="both", expand=True, padx=16, pady=10)

        self.chat_frame = tk.Frame(self.content, bg=C.CHAT_BG)
        self.chat_text = tk.Text(self.chat_frame, bg=C.CHAT_BG, fg=C.TEXT, bd=0, highlightthickness=0, wrap="word",
                                 padx=14, pady=10, cursor="arrow", state="disabled", font=self.f_body,
                                 spacing1=5, spacing3=5, takefocus=0, selectbackground=C.BORDER)
        chat_scroll = ttk.Scrollbar(self.chat_frame, orient="vertical", command=self.chat_text.yview,
                                    style="Chat.Vertical.TScrollbar")
        self.chat_text.configure(yscrollcommand=chat_scroll.set)
        chat_scroll.pack(side="right", fill="y")
        self.chat_text.pack(side="left", fill="both", expand=True)
        self.chat_text.tag_configure("left", justify="left", lmargin1=4, lmargin2=4)
        self.chat_text.tag_configure("right", justify="right", rmargin=4)
        self.chat_text.tag_configure("center", justify="center", foreground=C.DIM, font=self.f_small, spacing1=8, spacing3=8)
        self.chat_text.tag_configure("empty_title", justify="center", foreground=C.MUTED, font=self.f_head, spacing1=90)
        self.chat_text.tag_configure("empty_sub", justify="center", foreground=C.DIM, font=self.f_body, spacing1=6)

        self.log_frame = tk.Frame(self.content, bg=C.SURFACE_2)
        self.log_text = tk.Text(self.log_frame, bg=C.SURFACE_2, fg=C.TEXT, bd=0, highlightthickness=0, wrap="word",
                                padx=12, pady=8, state="disabled", font=self.f_mono, spacing1=2, spacing3=2,
                                selectbackground=C.BORDER, takefocus=0)
        log_scroll = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log_text.yview,
                                   style="Std.Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("time", foreground=C.DIM)
        for level, colour in (("INFO", C.MUTED), ("SYSTEM", C.PRIMARY), ("CONNECT", C.SUCCESS),
                              ("TEXT", C.TEXT), ("FILE", C.WARN), ("ERROR", C.DANGER)):
            self.log_text.tag_configure("lvl_" + level, foreground=colour, font=self.f_mono_bold)
        self.log_text.tag_configure("msg", foreground=C.TEXT)
        self.log_text.tag_configure("msg_error", foreground=C.DANGER)

    # ==================================================================
    # Responsive layout
    # ==================================================================
    def _on_root_configure(self, event) -> None:
        if event.widget is not self.root or self._closing:
            return
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(60, self._layout)

    def _layout(self) -> None:
        """Adapt the window to its current size (debounced)."""
        self._resize_job = None
        if self._closing:
            return
        width = self.root.winfo_width()
        if width <= 1:
            return
        compact = width < COMPACT_W
        side_w = max(270, min(360, int(width * 0.29)))
        self.side.configure(width=side_w)

        if compact != self.compact:
            self.compact = compact
            if compact:
                self.panel = "chat" if self.selected else "peers"
            self._arrange()

        wrap = max(220, min(560, width - 90)) if compact else max(220, side_w - 70)
        self.identity_label.configure(wraplength=wrap)

        set_visible(self.tagline, width >= WIDE_TEXT_W, anchor="w")
        set_visible(self.status_path, width >= PATH_TEXT_W, side="right", padx=14)
        set_visible(self.hint_label, not compact, anchor="w", pady=(6, 0))
        self.open_btn.set_text("Folder" if compact else "Open Downloads")
        self.attach_btn.set_text("File" if compact else "Choose File & Send")

        main_w = self.main.winfo_width()
        if main_w > 1:
            self.header_sub.configure(wraplength=max(160, main_w - 330))
        self._fit_bubbles()

    def _arrange(self) -> None:
        """Place sidebar / conversation according to the current mode."""
        self.side.pack_forget()
        self.main.pack_forget()
        if not self.compact:
            self.side.pack(side="left", fill="y", padx=(0, 12))
            self.main.pack(side="left", fill="both", expand=True)
            self.back_btn.pack_forget()
        elif self.panel == "peers":
            self.side.pack(fill="both", expand=True)
        else:
            self.main.pack(fill="both", expand=True)
            self.back_btn.pack(side="left", padx=(0, 10), before=self.header_avatar_slot)
        self.root.after(80, self._fit_bubbles)

    def _goto(self, panel: str) -> None:
        """Switch panel (only has a visible effect in compact mode)."""
        self.panel = panel
        if self.compact:
            self._arrange()

    def _fit_bubbles(self) -> None:
        """Re-wrap chat bubbles to ~2/3 of the conversation width."""
        if self._closing:
            return
        width = self.chat_text.winfo_width()
        if width < 120:
            return
        wrap = max(220, min(720, int(width * 0.66)))
        if abs(wrap - self._bubble_wrap) >= 24:
            self._bubble_wrap = wrap
            if self.selected:
                self._render_chat()

    # ==================================================================
    # Small UI helpers
    # ==================================================================
    def _toast(self, message: str, level: str = "info") -> None:
        colour = {"info": C.MUTED, "ok": C.SUCCESS, "warn": C.WARN, "error": C.DANGER}[level]
        dot = {"info": C.DIM, "ok": C.SUCCESS_DOT, "warn": C.WARN_DOT, "error": C.DANGER}[level]
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 9, 9, fill=dot, outline="")
        self.status_label.configure(text=message, fg=colour)
        if self._toast_job is not None:
            self.root.after_cancel(self._toast_job)
        if level != "info":
            self._toast_job = self.root.after(9000, lambda: self._toast(self._idle_status(), "info"))
        else:
            self._toast_job = None

    def _idle_status(self) -> str:
        if self.node and self.node.is_running:
            return f"Online as {self.node.name} - {len(self.peers)} peer(s) connected"
        return "Ready"

    def _set_online(self, online: bool) -> None:
        self.pill_dot.delete("all")
        self.pill_dot.create_oval(1, 1, 11, 11, fill=C.SUCCESS_DOT if online else C.DIM, outline="")
        if online and self.node:
            self.pill_label.configure(text=f"Online  -  {self.node.name}  [{self.node.peer_id}]", fg=C.SUCCESS)
        else:
            self.pill_label.configure(text="Offline", fg=C.MUTED)

    def _show_view(self, view: str) -> None:
        self.view = view
        for key, (text, underline) in self.tab_widgets.items():
            active = key == view
            text.configure(fg=C.TEXT if active else C.MUTED)
            underline.configure(bg=C.PRIMARY if active else C.SURFACE)
        self.chat_frame.pack_forget()
        self.log_frame.pack_forget()
        (self.chat_frame if view == "chat" else self.log_frame).pack(fill="both", expand=True)
        if view == "chat":
            self.text_entry.focus_set()
            self.root.after(40, self._fit_bubbles)

    def _copy_address(self, _event=None) -> None:
        if self.node and self.node.is_running:
            self.root.clipboard_clear()
            self.root.clipboard_append(f"{self.local_ip}:{self.node.port}")
            self._toast(f"Copied {self.local_ip}:{self.node.port} - share it with another peer", "ok")

    def _forward_wheel(self, widget: tk.Misc, target: tk.Text) -> None:
        """Mouse-wheel over a bubble should scroll the chat, not be swallowed."""
        widget.bind("<MouseWheel>", lambda e: target.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        widget.bind("<Button-4>", lambda _e: target.yview_scroll(-1, "units"))
        widget.bind("<Button-5>", lambda _e: target.yview_scroll(1, "units"))
        for child in widget.winfo_children():
            self._forward_wheel(child, target)

    # ==================================================================
    # Event log
    # ==================================================================
    def _log(self, level: str, message: str) -> None:
        text = self.log_text
        text.configure(state="normal")
        text.insert("end", time.strftime("%H:%M:%S  "), "time")
        text.insert("end", f"[{level}] ", "lvl_" + level)
        text.insert("end", message + "\n", "msg_error" if level == "ERROR" else "msg")
        lines = int(text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        text.configure(state="disabled")
        text.see("end")

    # ==================================================================
    # Actions (button handlers)
    # ==================================================================
    def start_peer(self) -> None:
        if self.node and self.node.is_running:
            return
        try:
            name = utils.validate_name(self.name_field.get())
            port = utils.validate_port(self.port_field.get(), minimum=1024)
        except utils.ValidationError as exc:
            self._fail(str(exc))
            return

        self._generation += 1
        generation = self._generation
        node = PeerNode(name, port, self.downloads_dir,
                        on_event=lambda event, **data: self.events.put((generation, event, data)))
        try:
            node.start()
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1), 10048, 10013):
                message = f"Port {port} is already in use. Choose a different port."
            else:
                message = f"Cannot start peer on port {port}: {exc.strerror or exc}"
            self._fail(message)
            return

        self.node = node
        self.peers.clear()
        self.peer_order.clear()
        self.history.clear()
        self.unread.clear()
        self.selected = None
        self.name_field.set_enabled(False)
        self.port_field.set_enabled(False)
        self.start_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.connect_btn.set_enabled(True)
        also = ("\nOther addresses: " + ", ".join(self.local_ips[1:4])) if len(self.local_ips) > 1 else ""
        self.identity_label.configure(
            text=f"ID  {node.peer_id}   -   listening on {self.local_ip}:{node.port}{also}\n"
                 "Click to copy  -  share the Wi-Fi/LAN address", fg=C.PRIMARY, cursor="hand2")
        self._set_online(True)
        self._refresh_peer_list()
        self._refresh_peer_header()
        self._render_chat()
        self._log("SYSTEM", f"Peer started: {node.name} [{node.peer_id}] on port {node.port}")
        self._toast(f"Peer started on port {node.port}", "ok")

    def stop_peer(self) -> None:
        node, self.node = self.node, None
        if node is None:
            return
        self._generation += 1                    # ignore anything the old node still emits
        node.stop()
        self.peers.clear()
        self.peer_order.clear()
        self.selected = None
        self._clear_transfer()
        self.name_field.set_enabled(True)
        self.port_field.set_enabled(True)
        self.start_btn.set_enabled(True)
        self.stop_btn.set_enabled(False)
        self.connect_btn.set_enabled(False)
        self.connect_btn.set_text("Connect")
        self.identity_label.configure(text="Not running", fg=C.MUTED, cursor="arrow")
        self._set_online(False)
        self._refresh_peer_list()
        self._refresh_peer_header()
        self._render_chat()
        self._goto("peers")
        self._log("SYSTEM", "Peer stopped. All connections closed.")
        self._toast("Peer stopped", "info")

    def connect_clicked(self) -> None:
        if not (self.node and self.node.is_running):
            self._fail("Start your peer before connecting.")
            return
        try:
            host = utils.validate_host(self.ip_field.get())
            port = utils.validate_port(self.rport_field.get())
        except utils.ValidationError as exc:
            self._fail(str(exc))
            return
        self.connect_btn.set_enabled(False)
        self.connect_btn.set_text("Connecting...")
        self.node.connect_to_peer(host, port)

    def send_text_clicked(self) -> None:
        if not (self.node and self.node.is_running):
            self._fail("Start your peer first.")
            return
        if not self.selected:
            self._fail("Select a connected peer first.")
            return
        text = self.text_entry.get().strip()
        if not text:
            return
        if len(text) > protocol.MAX_TEXT_LENGTH:
            self._fail(f"Message too long (max {protocol.MAX_TEXT_LENGTH} characters).")
            return
        if self.node.send_text(self.selected, text):
            self.text_entry.delete(0, "end")

    def choose_file_and_send(self) -> None:
        if not (self.node and self.node.is_running):
            self._fail("Start your peer first.")
            return
        if not self.selected:
            self._fail("Select a connected peer first.")
            return
        path = filedialog.askopenfilename(parent=self.root, title="Choose a file to send")
        if path:
            self.send_file_path(path)

    def send_file_path(self, path: str) -> None:
        if not (self.node and self.node.is_running):
            self._fail("Start your peer first.")
            return
        if not self.selected:
            self._fail("Select a connected peer first.")
            return
        if not os.path.isfile(path):
            self._fail(f"File does not exist: {path}")
            return
        self.node.send_file(self.selected, path)

    def disconnect_selected(self) -> None:
        if self.node and self.selected:
            self.node.disconnect_peer(self.selected)

    def open_downloads(self) -> None:
        os.makedirs(self.downloads_dir, exist_ok=True)
        if not utils.open_folder(self.downloads_dir):
            self._toast("Could not open the downloads folder: " + self.downloads_dir, "warn")

    def _fail(self, message: str) -> None:
        self._log("ERROR", message)
        self._toast(message, "error")

    def on_close(self) -> None:
        self._closing = True
        for job in (getattr(self, "_poll_job", None), self._toast_job, self._resize_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        if self.node:
            self.node.stop()
        self.root.destroy()

    # ==================================================================
    # Event pump (Tk thread)
    # ==================================================================
    def _poll_events(self) -> None:
        if self._closing:
            return
        try:
            for _ in range(200):
                generation, event, data = self.events.get_nowait()
                if generation == self._generation:
                    self._handle_event(event, data)
        except queue.Empty:
            pass
        self._poll_job = self.root.after(POLL_MS, self._poll_events)

    def _handle_event(self, event: str, data: dict) -> None:
        handler = getattr(self, "_on_" + event, None)
        if handler:
            handler(**data)

    def _on_info(self, message: str, **_):
        self._log("INFO", message)

    def _on_error(self, message: str, **_):
        self._fail(message)

    def _on_peer_connected(self, peer_id, name, ip, port, direction="", replaced=False, **_):
        if peer_id in self.peers:              # same peer, connection swapped (both sides clicked Connect)
            self.peers[peer_id].update(name=name, ip=ip, port=port)
            self._connect_finished()
            self._refresh_peer_list()
            self._refresh_peer_header()
            return
        self.peers[peer_id] = {"name": name, "ip": ip, "port": port,
                               "color": C.AVATARS[self._color_counter % len(C.AVATARS)]}
        self._color_counter += 1
        if peer_id not in self.peer_order:
            self.peer_order.append(peer_id)
        self._connect_finished()
        self._add_history(peer_id, {"kind": "system", "text": f"Connected to {name} ({ip}:{port})", "time": utils.format_time()})
        self._log("CONNECT", f"Connected to {name} [{peer_id}] at {ip}:{port}"
                  + ("  (they connected to you)" if direction == "incoming" else ""))
        self._toast(f"Connected to {name}", "ok")
        if self.selected is None:
            self.select_peer(peer_id)
        else:
            self._refresh_peer_list()

    def _on_peer_disconnected(self, peer_id, name, reason="", **_):
        self.peers.pop(peer_id, None)
        if peer_id in self.peer_order:
            self.peer_order.remove(peer_id)
        self._add_history(peer_id, {"kind": "system", "text": f"{name} disconnected", "time": utils.format_time()})
        self._log("CONNECT", f"{name} [{peer_id}] disconnected ({reason})")
        self._toast(f"{name} disconnected", "warn")
        if self._transfer and self._transfer.get("peer_id") == peer_id:
            self._clear_transfer()
        if self.selected == peer_id:
            self.selected = self.peer_order[0] if self.peer_order else None
            if self.selected:
                self.unread.pop(self.selected, None)
            else:
                self._goto("peers")
            self._refresh_peer_header()
            self._render_chat()
        self._refresh_peer_list()

    def _on_connect_failed(self, message: str, **_):
        self._connect_finished()
        self._fail(message)

    def _connect_finished(self) -> None:
        if self.node and self.node.is_running:
            self.connect_btn.set_text("Connect")
            self.connect_btn.set_enabled(True)

    def _on_text_received(self, peer_id, name, text, timestamp=None, **_):
        self._log("TEXT", f"{name} -> You: {text}")
        self._add_history(peer_id, {"kind": "text", "mine": False, "sender": name, "text": text,
                                    "time": utils.format_time(timestamp)}, incoming=True)

    def _on_text_sent(self, peer_id, name, text, timestamp=None, **_):
        self._log("TEXT", f"You -> {name}: {text}")
        self._add_history(peer_id, {"kind": "text", "mine": True, "sender": "You", "text": text,
                                    "time": utils.format_time(timestamp)})

    def _on_file_progress(self, direction, peer_id, name, filename, done, total, **_):
        now = time.monotonic()
        key = (direction, peer_id, filename)
        if not self._transfer or self._transfer["key"] != key:
            self._transfer = {"key": key, "peer_id": peer_id, "start": now}
            self.xfer_frame.pack(fill="x", before=self.composer_row)
        elapsed = max(now - self._transfer["start"], 0.001)
        speed = done / elapsed
        fraction = (done / total) if total else 1.0
        verb = "Sending" if direction == "out" else "Receiving"
        arrow = "to" if direction == "out" else "from"
        self.xfer_label.configure(
            text=f"{verb} {filename} {arrow} {name}   -   {fraction * 100:.0f}%   "
                 f"({utils.format_size(done)} / {utils.format_size(total)})   {utils.format_size(speed)}/s")
        self.xfer_bar.set(fraction)

    def _clear_transfer(self) -> None:
        self._transfer = None
        self.xfer_frame.pack_forget()

    def _on_file_received(self, peer_id, name, filename, path, filesize, timestamp=None, **_):
        self._clear_transfer()
        self._log("FILE", f"{name} -> You: File received: {filename} ({utils.format_size(filesize)}) saved to {path}")
        self._add_history(peer_id, {"kind": "file", "mine": False, "sender": name, "filename": filename,
                                    "size": filesize, "path": path, "time": utils.format_time(timestamp)}, incoming=True)
        self._toast(f"File received: {filename}", "ok")

    def _on_file_sent(self, peer_id, name, filename, path, filesize, timestamp=None, **_):
        self._clear_transfer()
        self._log("FILE", f"You -> {name}: File sent: {filename} ({utils.format_size(filesize)})")
        self._add_history(peer_id, {"kind": "file", "mine": True, "sender": "You", "filename": filename,
                                    "size": filesize, "path": path, "time": utils.format_time(timestamp)})
        self._toast(f"File sent: {filename}", "ok")

    # ==================================================================
    # Conversations
    # ==================================================================
    def _add_history(self, peer_id: str, entry: dict, incoming: bool = False) -> None:
        self.history.setdefault(peer_id, []).append(entry)
        if peer_id == self.selected:
            self._append_entry(entry)
        elif incoming:
            self.unread[peer_id] = self.unread.get(peer_id, 0) + 1
            self._refresh_peer_list()

    def select_peer(self, peer_id: str) -> None:
        if peer_id not in self.peers:
            return
        self.selected = peer_id
        self.unread.pop(peer_id, None)
        self._refresh_peer_list()
        self._refresh_peer_header()
        self._render_chat()
        self._show_view("chat")
        self._goto("chat")

    def _refresh_peer_header(self) -> None:
        for child in self.header_avatar_slot.winfo_children():
            child.destroy()
        info = self.peers.get(self.selected) if self.selected else None
        if info:
            Avatar(self.header_avatar_slot, info["name"], info["color"], 42, C.SURFACE, self.f_head).pack()
            self.header_title.configure(text=info["name"])
            self.header_sub.configure(text=f"ID {self.selected}   -   {info['ip']}:{info['port']}   -   direct TCP connection")
            self.disconnect_btn.pack(side="left")
            self.disconnect_btn.set_enabled(True)
        else:
            self.header_title.configure(text="No peer selected")
            self.header_sub.configure(text="Connect to a peer and pick it from the list")
            self.disconnect_btn.set_enabled(False)

    def _refresh_peer_list(self) -> None:
        inner = self.peer_scroll.inner
        for child in inner.winfo_children():
            child.destroy()
        self.count_label.configure(text=str(len(self.peer_order)))
        if not self.peer_order:
            tk.Label(inner, text="No peers connected yet.\n\nStart your peer, then connect\nusing an IP address and port.",
                     font=self.f_small, bg=C.SURFACE, fg=C.DIM, justify="center").pack(pady=26, fill="x")
            return
        for peer_id in self.peer_order:
            self._build_peer_row(inner, peer_id)

    def _build_peer_row(self, parent: tk.Frame, peer_id: str) -> None:
        info = self.peers[peer_id]
        selected = peer_id == self.selected
        bg = C.PRIMARY_SOFT if selected else C.SURFACE
        row = tk.Frame(parent, bg=bg, cursor="hand2")
        row.pack(fill="x", pady=2)
        tk.Frame(row, bg=C.PRIMARY if selected else bg, width=3).pack(side="left", fill="y")
        Avatar(row, info["name"], info["color"], 34, bg, self.f_bold).pack(side="left", padx=(10, 10), pady=8)
        col = tk.Frame(row, bg=bg)
        col.pack(side="left", fill="x", expand=True)
        tk.Label(col, text=info["name"], font=self.f_bold, bg=bg, fg=C.TEXT, anchor="w").pack(anchor="w")
        tk.Label(col, text=f"{peer_id}  -  {info['ip']}:{info['port']}", font=self.f_small, bg=bg, fg=C.MUTED, anchor="w").pack(anchor="w")
        unread = self.unread.get(peer_id, 0)
        if unread:
            tk.Label(row, text=str(unread), font=self.f_small_bold, bg=C.BADGE, fg="#ffffff", padx=7).pack(side="right", padx=10)

        def bind_all(widget: tk.Misc) -> None:
            widget.bind("<Button-1>", lambda _e, pid=peer_id: self.select_peer(pid))
            self.peer_scroll.bind_wheel(widget)
            for child in widget.winfo_children():
                bind_all(child)
        bind_all(row)

    # ------------------------------------------------------------------
    # Chat rendering
    # ------------------------------------------------------------------
    def _render_chat(self) -> None:
        text = self.chat_text
        for child in text.winfo_children():
            child.destroy()
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.configure(state="disabled")
        if not self.selected:
            text.configure(state="normal")
            title = "Start a peer, then connect to someone" if not (self.node and self.node.is_running) else "Select a peer to start chatting"
            text.insert("end", title + "\n", "empty_title")
            text.insert("end", "Messages and files go straight to the other computer - there is no server in between.\n", "empty_sub")
            text.configure(state="disabled")
            return
        for entry in self.history.get(self.selected, [])[-MAX_CHAT_RENDER:]:
            self._append_entry(entry)
        self._scroll_chat_to_end()

    def _append_entry(self, entry: dict) -> None:
        text = self.chat_text
        text.configure(state="normal")
        if entry["kind"] == "system":
            text.insert("end", f"{entry['text']}  -  {entry['time']}\n", "center")
        else:
            mine = entry["mine"]
            tag = "right" if mine else "left"
            start = text.index("end-1c")
            holder = self._make_bubble(entry)
            text.window_create("end", window=holder)
            text.insert("end", "\n")
            text.tag_add(tag, start, "end-1c")
            self._forward_wheel(holder, text)
        text.configure(state="disabled")
        self._scroll_chat_to_end()

    def _scroll_chat_to_end(self) -> None:
        self.chat_text.update_idletasks()
        self.chat_text.yview_moveto(1.0)
        self.root.after(30, lambda: self.chat_text.yview_moveto(1.0))

    def _make_bubble(self, entry: dict) -> tk.Frame:
        mine = entry["mine"]
        wrap = self._bubble_wrap
        holder = tk.Frame(self.chat_text, bg=C.CHAT_BG)
        side = "e" if mine else "w"
        who = "You" if mine else entry["sender"]
        tk.Label(holder, text=f"{who}  -  {entry['time']}", font=self.f_small, bg=C.CHAT_BG, fg=C.DIM).pack(anchor=side, padx=3)

        if entry["kind"] == "text":
            bubble = tk.Label(holder, text=entry["text"], wraplength=wrap, justify="left", anchor="w",
                              bg=C.BUBBLE_ME if mine else C.BUBBLE_THEM, fg="#ffffff" if mine else C.TEXT,
                              font=(self.f_body[0], 11), padx=14, pady=9)
            bubble.pack(anchor=side, pady=(2, 0))
        else:
            bg = C.SURFACE
            card = tk.Frame(holder, bg=bg, highlightthickness=1, highlightbackground=C.PRIMARY if mine else C.BORDER)
            card.pack(anchor=side, pady=(2, 0))
            label, tile_bg, tile_fg = file_badge(entry["filename"])
            tk.Label(card, text=label, font=self.f_mono_bold, bg=tile_bg, fg=tile_fg, width=6).pack(side="left", fill="y")
            body = tk.Frame(card, bg=bg)
            body.pack(side="left", padx=14, pady=10)
            tk.Label(body, text=entry["filename"], font=self.f_bold, bg=bg, fg=C.TEXT, anchor="w",
                     wraplength=max(140, wrap - 90), justify="left").pack(anchor="w")
            state = "Sent" if mine else "Received - saved to downloads"
            tk.Label(body, text=f"{utils.format_size(entry['size'])}   -   {state}", font=self.f_small,
                     bg=bg, fg=C.MUTED).pack(anchor="w", pady=(2, 0))
            if not mine and entry.get("path"):
                link = tk.Label(body, text="Show in folder", font=self.f_small_bold, bg=bg, fg=C.PRIMARY, cursor="hand2")
                link.pack(anchor="w", pady=(6, 0))
                link.bind("<Button-1>", lambda _e, p=entry["path"]: self._reveal(p))
        return holder

    def _reveal(self, path: str) -> None:
        if not utils.open_folder(path, select_file=True):
            self._toast("Could not open the folder: " + os.path.dirname(path), "warn")
