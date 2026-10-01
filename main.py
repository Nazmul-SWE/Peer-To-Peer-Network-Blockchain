"""
main.py - entry point of PeerLink

    python main.py
    python main.py --name Alice --port 5000 --start      (handy for demos)
"""

import argparse
import sys


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="PeerLink - peer-to-peer chat and file sharing over TCP")
    parser.add_argument("--name", help="peer name to pre-fill (default: your OS user name)")
    parser.add_argument("--port", type=int, default=5000, help="listening port to pre-fill (default: 5000)")
    parser.add_argument("--start", action="store_true", help="start the peer immediately")
    parser.add_argument("--downloads", help="folder for received files (default: ./downloads)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    if sys.version_info < (3, 9):
        print("PeerLink needs Python 3.9 or newer.")
        return 1
    try:
        import tkinter as tk
    except ImportError:
        print("Tkinter is missing. On Debian/Ubuntu install it with:  sudo apt install python3-tk")
        return 1

    from gui import App, DOWNLOADS_DIR

    if sys.platform.startswith("win"):           # crisp text on high-DPI Windows screens
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    args = parse_args(argv)
    root = tk.Tk()
    app = App(root, downloads_dir=args.downloads or DOWNLOADS_DIR,
              default_name=args.name, default_port=args.port)
    if args.start:
        root.after(100, app.start_peer)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
